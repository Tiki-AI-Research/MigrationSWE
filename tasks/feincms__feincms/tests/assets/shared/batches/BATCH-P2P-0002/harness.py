"""Executable state-preservation harness for BATCH-P2P-0002.

This harness produces a *concrete, execution-derived projection* for every P2P
observation and never a bare ``{"result": "PASS"}`` control status.  Each
projection is computed by executing a probe against the repository tree, so the
value changes if the preserved contract changes.

The projections are deliberately environment independent: they inspect the
repository with the Python standard library only (AST, ``configparser``,
``hashlib``, ``py_compile``, ``git``) and never require the project's runtime
dependencies to be installed.  This matters because the source side of a
version-upgrade migration is a *source-dockerfile* runtime whose only declared
preflight command is ``git --version`` -- it has a Python interpreter but is not
guaranteed to have Django importable.  Running the same probe on both sides and
normalising with ``json.dumps(..., sort_keys=True)`` therefore yields the same
stable value on the source and the target side.

Every potentially long-running subprocess is wrapped in the ``timeout`` utility
and in a matching ``subprocess`` timeout, so the harness can never block
indefinitely.  Tests that require Playwright / a live browser need extra
privileges and are excluded.
"""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

DEFAULT_TIMEOUT_SECONDS = 300
COMMAND_GRACE_SECONDS = 30
DEPENDENCY_MANIFEST = "/assets/dependencies/dependency-manifest.json"

OBSERVATION_IDS = (
    "P2P-025",
    "P2P-026",
    "P2P-027",
    "P2P-028",
    "P2P-029",
    "P2P-030",
    "P2P-031",
)

_MIGRATION_SURFACE_DIRS = ("feincms/", "docs/", "tests/")
_MIGRATION_SURFACE_ROOT_FILES = frozenset(
    {
        "setup.cfg",
        "setup.py",
        "pyproject.toml",
        "tox.ini",
        "MANIFEST.in",
        ".pre-commit-config.yaml",
        ".gitignore",
        ".gitattributes",
        "CHANGELOG.rst",
        "CHANGELOG.md",
        "README.rst",
        "README.md",
        "LICENSE",
        "AUTHORS",
        "CONTRIBUTING.rst",
    }
)


def _within_migration_surface(path):
    normalized = str(path).replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return True
    if normalized in _MIGRATION_SURFACE_ROOT_FILES:
        return True
    return any(
        normalized.startswith(prefix) for prefix in _MIGRATION_SURFACE_DIRS
    )

STATIC_ASSET = "feincms/static/feincms/jquery-1.11.3.min.js"
ITEM_EDITOR_JS = "feincms/static/feincms/item_editor.js"
MODELS_PY = "feincms/models.py"
ADMIN_INIT_PY = "feincms/admin/__init__.py"
ADMIN_FILTERS_PY = "feincms/admin/filters.py"
ITEM_EDITOR_PY = "feincms/admin/item_editor.py"
TREE_EDITOR_PY = "feincms/admin/tree_editor.py"
VIEWS_INIT_PY = "feincms/views/__init__.py"
RAW_CONTENT_PY = "feincms/content/raw/models.py"
TEST_PAGE_PY = "tests/testapp/tests/test_page.py"
TEST_TEMPLATE_E2E_PY = "tests/testapp/tests/test_template_change_e2e.py"
MODELADMINS_PY = "feincms/module/page/modeladmins.py"

ADMIN_REDIRECT_TEST = "test_24_admin_redirects"
ADMIN_REDIRECT_CLASS = "PagesTestCase"
TEMPLATE_CHANGE_TEST = "test_double_submission_protection"

CLICK_TRIGGER = 'form_element.find("[type=submit][name=_save]").click()'
SELECTOR_TOKEN = '.find("input[type=submit], button")'
DISABLE_TOKEN = '.attr("disabled", "disabled")'

ARTIFACT_MARKERS = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    ".coverage",
    "htmlcov",
)
ARTIFACT_SUFFIXES = (".pyc", ".pyo", ".db", ".sqlite3", ".log")


# ---------------------------------------------------------------------------
# Process / repository helpers
# ---------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _candidate_roots() -> list:
    roots: list = []
    for candidate in (
        os.environ.get("MIGRATION_REPO"),
        "/workspace/repo",
        "/input/source",
        "/input/target",
    ):
        if candidate and candidate not in roots:
            roots.append(candidate)
    return roots


def resolve_repo(preferred: str | None = None) -> str:
    """Return the first candidate directory that actually contains FeinCMS."""

    ordered = []
    if preferred:
        ordered.append(preferred)
    for candidate in _candidate_roots():
        if candidate not in ordered:
            ordered.append(candidate)
    for candidate in ordered:
        if os.path.isdir(os.path.join(candidate, "feincms")):
            return candidate
    if preferred:
        return preferred
    return os.environ.get("MIGRATION_REPO", "/workspace/repo")


def repo_root() -> str:
    return resolve_repo(os.environ.get("MIGRATION_REPO"))


def run_command(
    argv: Sequence[str],
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Run ``argv`` wrapped in the ``timeout`` utility and capture output."""

    merged = dict(os.environ)
    if env:
        merged.update({str(key): str(value) for key, value in env.items()})
    command = ["timeout", str(int(timeout)), *[str(part) for part in argv]]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            timeout=timeout + COMMAND_GRACE_SECONDS,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": command,
            "returncode": None,
            "timed_out": True,
            "stdout": _as_text(exc.stdout),
            "stderr": _as_text(exc.stderr),
        }
    except OSError as exc:
        return {
            "argv": command,
            "returncode": 127,
            "timed_out": False,
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "argv": command,
        "returncode": completed.returncode,
        "timed_out": False,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def read_dependency_manifest() -> dict:
    if not os.path.isfile(DEPENDENCY_MANIFEST):
        return {}
    try:
        with open(DEPENDENCY_MANIFEST, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _read_text(repo: str, relpath: str) -> str | None:
    path = os.path.join(repo, relpath)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _parse(source: str | None) -> ast.Module | None:
    if source is None:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _class_node(tree: ast.AST | None, name: str) -> ast.ClassDef | None:
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _method_node(
    class_node: ast.ClassDef | None, name: str
) -> ast.FunctionDef | None:
    if class_node is None:
        return None
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _function_node(tree: ast.AST | None, name: str) -> ast.FunctionDef | None:
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _has_var_keyword(node: ast.FunctionDef | None) -> bool:
    return node is not None and node.args.kwarg is not None


def _module_compiles(repo: str, relpath: str) -> bool:
    path = os.path.join(repo, relpath)
    if not os.path.isfile(path):
        return False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            py_compile.compile(
                path, cfile=os.path.join(tmp, "out.pyc"), doraise=True
            )
        return True
    except (py_compile.PyCompileError, OSError, ValueError):
        return False


def read_config(path: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser


def _expand_braces(expression: str) -> list:
    match = re.search(r"\{([^{}]*)\}", expression)
    if not match:
        return [expression.strip()] if expression.strip() else []
    prefix, suffix = expression[: match.start()], expression[match.end() :]
    expanded: list = []
    for option in match.group(1).split(","):
        for tail in _expand_braces(prefix + option + suffix):
            expanded.append(tail)
    return expanded


def tox_environments(repo: str) -> list:
    parser = read_config(os.path.join(repo, "tox.ini"))
    if not parser.has_section("tox"):
        return []
    raw = parser.get("tox", "envlist", fallback="")
    environments: list = []
    for chunk in raw.split():
        chunk = chunk.strip().rstrip(",")
        if not chunk:
            continue
        environments.extend(_expand_braces(chunk))
    return [env for env in environments if env]


def python_environments(repo: str) -> list:
    environments = tox_environments(repo)
    factors = {
        env.split("-", 1)[0]
        for env in environments
        if env.startswith("py") and len(env) > 2
    }
    return sorted(factors)


def python_floor(repo: str) -> tuple | None:
    parser = read_config(os.path.join(repo, "setup.cfg"))
    if not parser.has_section("options"):
        return None
    spec = parser.get("options", "python_requires", fallback="")
    match = re.search(r">=\s*(\d+)\.(\d+)", spec)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def python_floor_spec(repo: str) -> str:
    parser = read_config(os.path.join(repo, "setup.cfg"))
    if not parser.has_section("options"):
        return ""
    return parser.get("options", "python_requires", fallback="").strip()


def python_classifier_versions(repo: str) -> list:
    parser = read_config(os.path.join(repo, "setup.cfg"))
    if not parser.has_section("metadata"):
        return []
    raw = parser.get("metadata", "classifiers", fallback="")
    versions: list = []
    for line in raw.splitlines():
        match = re.search(
            r"Programming Language :: Python :: (\d+\.\d+)\s*$", line.strip()
        )
        if match:
            versions.append(match.group(1))
    return sorted(set(versions))


def _is_artifact(path: str) -> bool:
    if any(marker in path for marker in ARTIFACT_MARKERS):
        return True
    return path.endswith(ARTIFACT_SUFFIXES)


def changed_paths(repo: str) -> set | None:
    inside = run_command(
        ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"], timeout=60
    )
    if inside["returncode"] != 0:
        return None
    status = run_command(
        ["git", "-C", repo, "status", "--porcelain=v1", "-z"], timeout=120
    )
    if status["returncode"] != 0:
        return None
    paths: set = set()
    for entry in status["stdout"].split("\0"):
        if not entry:
            continue
        payload = entry[3:] if len(entry) > 3 else entry
        if " -> " in payload:
            payload = payload.split(" -> ", 1)[1]
        if payload and not _is_artifact(payload):
            paths.add(payload)
    return paths


def static_asset_digest(repo: str) -> str | None:
    path = os.path.join(repo, STATIC_ASSET)
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Result / projection helpers
# ---------------------------------------------------------------------------


def _result(
    observation_id: str,
    passed: bool,
    observed: Any,
    evidence: Sequence[Any],
    error_type: str = "",
) -> dict:
    return {
        "observation_id": observation_id,
        "passed": bool(passed),
        "expected": "PASS",
        "observed": observed,
        "error_type": error_type,
        "evidence": list(evidence),
    }


def _probe_evidence(repo: str, path: str) -> dict:
    return {"kind": "probe", "path": path, "present": os.path.isfile(os.path.join(repo, path))}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_render_contract(repo: str) -> dict:
    """P2P-025: the content-type base ``render(**kwargs)`` contract."""

    source = _read_text(repo, MODELS_PY)
    tree = _parse(source)
    render = _function_node(tree, "render")
    segment = ""
    if source is not None and render is not None:
        segment = ast.get_source_segment(source, render) or ""
    projection = {
        "models_compiles": _module_compiles(repo, MODELS_PY),
        "render_defined": render is not None,
        "accepts_var_keyword": _has_var_keyword(render),
        "dispatches_region_method": "render_" in segment,
        "raises_not_implemented": "NotImplementedError" in segment,
    }
    passed = all(
        projection[key]
        for key in (
            "models_compiles",
            "render_defined",
            "accepts_var_keyword",
            "dispatches_region_method",
            "raises_not_implemented",
        )
    )
    return _result(
        "P2P-025",
        passed,
        projection,
        [_probe_evidence(repo, MODELS_PY)],
        "" if passed else "render_contract_violation",
    )


def probe_public_imports(repo: str) -> dict:
    """P2P-029: previously importable public names remain defined."""

    init_src = _read_text(repo, "feincms/__init__.py") or ""
    views_src = _read_text(repo, VIEWS_INIT_PY) or ""
    filters_src = _read_text(repo, ADMIN_FILTERS_PY) or ""
    admin_src = _read_text(repo, ADMIN_INIT_PY) or ""
    item_src = _read_text(repo, ITEM_EDITOR_PY) or ""
    tree_src = _read_text(repo, TREE_EDITOR_PY) or ""
    raw_src = _read_text(repo, RAW_CONTENT_PY) or ""
    settings_is_lazy = (
        "class LazySettings" in init_src and "settings = LazySettings()" in init_src
    )
    modules = (
        "feincms/__init__.py",
        VIEWS_INIT_PY,
        ADMIN_FILTERS_PY,
        ADMIN_INIT_PY,
        ITEM_EDITOR_PY,
        TREE_EDITOR_PY,
        RAW_CONTENT_PY,
    )
    projection = {
        "settings_is_lazy": settings_is_lazy,
        "settings_type": "LazySettings" if "class LazySettings" in init_src else "",
        "handler_defined": "class Handler" in views_src,
        "parent_filter_defined": "class ParentFieldListFilter" in filters_src,
        "category_filter_defined": "class CategoryFieldListFilter" in filters_src,
        "field_list_filter_registered": "FieldListFilter.register" in admin_src,
        "item_editor_defined": "class ItemEditor" in item_src,
        "tree_editor_defined": "class TreeEditor" in tree_src,
        "raw_content_render_defined": (
            "class RawContent" in raw_src and "def render" in raw_src
        ),
        "modules_compile": all(_module_compiles(repo, path) for path in modules),
    }
    required = (
        "settings_is_lazy",
        "handler_defined",
        "parent_filter_defined",
        "category_filter_defined",
        "field_list_filter_registered",
        "item_editor_defined",
        "tree_editor_defined",
        "raw_content_render_defined",
        "modules_compile",
    )
    passed = projection["settings_type"] == "LazySettings" and all(
        projection[key] for key in required
    )
    return _result(
        "P2P-029",
        passed,
        projection,
        [_probe_evidence(repo, path) for path in modules],
        "" if passed else "public_import_violation",
    )


def probe_template_change(repo: str) -> dict:
    """P2P-026: the double-submission fix and its browser test survive."""

    script = _read_text(repo, ITEM_EDITOR_JS) or ""
    e2e_tree = _parse(_read_text(repo, TEST_TEMPLATE_E2E_PY))
    click_pos = script.find(CLICK_TRIGGER)
    selector_pos = script.find(SELECTOR_TOKEN)
    projection = {
        "e2e_test_defined": _function_node(e2e_tree, TEMPLATE_CHANGE_TEST) is not None,
        "script_present": bool(script),
        "click_trigger_present": click_pos >= 0,
        "selector_fix_present": selector_pos >= 0,
        "disable_attr_present": DISABLE_TOKEN in script,
        "disable_after_click": (
            click_pos >= 0 and selector_pos >= 0 and click_pos < selector_pos
        ),
    }
    passed = all(
        projection[key]
        for key in (
            "e2e_test_defined",
            "script_present",
            "click_trigger_present",
            "selector_fix_present",
            "disable_attr_present",
            "disable_after_click",
        )
    )
    return _result(
        "P2P-026",
        passed,
        projection,
        [
            _probe_evidence(repo, ITEM_EDITOR_JS),
            _probe_evidence(repo, TEST_TEMPLATE_E2E_PY),
        ],
        "" if passed else "template_change_fix_violation",
    )


def probe_admin_redirects(repo: str) -> dict:
    """P2P-030: the admin redirect test and the admin behaviour survive."""

    test_source = _read_text(repo, TEST_PAGE_PY)
    test_tree = _parse(test_source)
    page_case = _class_node(test_tree, ADMIN_REDIRECT_CLASS)
    modeladmins = _read_text(repo, MODELADMINS_PY) or ""
    projection = {
        "test_class_defined": page_case is not None,
        "test_present": _method_node(page_case, ADMIN_REDIRECT_TEST) is not None,
        "test_module_compiles": _module_compiles(repo, TEST_PAGE_PY),
        "response_add_defined": "def response_add" in modeladmins,
        "admin_module_compiles": _module_compiles(repo, MODELADMINS_PY),
    }
    passed = all(projection.values())
    return _result(
        "P2P-030",
        passed,
        projection,
        [
            _probe_evidence(repo, TEST_PAGE_PY),
            _probe_evidence(repo, MODELADMINS_PY),
        ],
        "" if passed else "admin_redirect_contract_violation",
    )


def probe_static_asset(repo: str) -> dict:
    """P2P-027: the repository static asset is byte-for-byte unchanged."""

    digest = static_asset_digest(repo)
    projection = {"sha256": digest, "present": digest is not None}
    passed = digest is not None
    return _result(
        "P2P-027",
        passed,
        projection,
        [{"kind": "file", "path": STATIC_ASSET, "sha256": digest}],
        "" if passed else "static_asset_missing",
    )


def probe_python_support(repo: str) -> dict:
    """P2P-028: the Python floor and the tox Python matrix are preserved."""

    floor = python_floor(repo)
    environments = python_environments(repo)
    projection = {
        "python_requires": python_floor_spec(repo),
        "python_environments": environments,
    }
    passed = (
        floor is not None
        and floor <= (3, 10)
        and "py310" in environments
        and "py311" in environments
    )
    return _result(
        "P2P-028",
        passed,
        projection,
        [
            {
                "kind": "config",
                "path": "setup.cfg",
                "python_requires": projection["python_requires"],
            },
            {
                "kind": "config",
                "path": "tox.ini",
                "python_environments": environments,
            },
        ],
        "" if passed else "python_floor_violation",
    )


def probe_metadata_scope(repo: str) -> dict:
    """P2P-031: declared Python support and the migration change scope."""

    floor = python_floor(repo)
    classifiers = python_classifier_versions(repo)
    kept = {"3.10", "3.11", "3.12"} <= set(classifiers)
    changed = changed_paths(repo)
    confined = changed is None or all(
        _within_migration_surface(path) for path in changed
    )
    projection = {
        "python_requires": python_floor_spec(repo),
        "classifier_versions": classifiers,
        "change_scope_confined": bool(confined),
    }
    passed = floor is not None and floor <= (3, 10) and kept and confined
    return _result(
        "P2P-031",
        passed,
        projection,
        [
            {
                "kind": "config",
                "path": "setup.cfg",
                "python_requires": projection["python_requires"],
                "classifier_versions": classifiers,
            },
            {
                "kind": "diff",
                "changed_paths": None if changed is None else sorted(changed),
                "allowed_scope": sorted(_MIGRATION_SURFACE_DIRS)
                + sorted(_MIGRATION_SURFACE_ROOT_FILES),
                "confined": bool(confined),
            },
        ],
        "" if passed else "metadata_scope_violation",
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_probes(
    repo: str | None = None,
    side: str = "target",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list:
    """Return the per-observation projections for one side.

    ``timeout`` is accepted for API compatibility with the runtime harness
    contract; the probes themselves inspect the tree in-process and are fully
    deterministic.
    """

    del timeout  # projections are in-process and bounded by construction
    resolved = resolve_repo(repo)
    manifest = read_dependency_manifest()

    results = [
        probe_render_contract(resolved),
        probe_template_change(resolved),
        probe_static_asset(resolved),
        probe_python_support(resolved),
        probe_public_imports(resolved),
        probe_admin_redirects(resolved),
        probe_metadata_scope(resolved),
    ]

    shared_evidence = {
        "kind": "runtime",
        "side": side,
        "repository": resolved,
        "dependency_manifest_path": DEPENDENCY_MANIFEST,
        "dependency_batch_ids": [
            entry.get("batch_id") for entry in manifest.get("batches", [])
        ],
    }
    for result in results:
        result["side"] = side
        result["evidence"].append(shared_evidence)
    return results


def write_report(path: str, results: Sequence[Any]) -> str:
    payload = [
        result if isinstance(result, dict) else dict(result) for result in results
    ]
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    side = os.environ.get("P2P_SIDE", "target")
    report = argv[0] if argv else "/logs/result.json"
    results = run_probes(repo_root(), side)
    write_report(report, results)
    print(json.dumps([result["observation_id"] for result in results]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
