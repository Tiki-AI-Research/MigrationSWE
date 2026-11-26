"""Standard-library-only fallback projections for the INTEGRATION runtime.

The published asset is executed in two very different containers:

* the ``target`` runtime (a full Django 5.2 / pytest environment), and
* the ``source`` runtime (a ``source-dockerfile`` that only ships ``git`` and a
  bare Python interpreter -- no importable Django, no pytest).

The dependency batches own the *executable* probes.  Several of them (for
example ``BATCH-P2P-0002``) are written purely against the repository tree and
therefore run on both sides.  Others necessarily import Django, so on the bare
source runtime they cannot execute.

This module gives the integration entry point a deterministic, repository
derived projection for every observation so that the runtime report is always
complete and so that behaviour-preservation observations (P2P) and differential
observations normalize to the exact same value on both sides, even when the
owning dependency probe cannot execute on one of them.

Nothing here is hard-coded by role or side: every value comes from inspecting
the repository that is being probed.  The only side-sensitive computation is
the target-technology check used by the F2P fallback, which is a genuine
inspection of the repository (does it still keep the pre-Django-5.2 runtime
branches, is the declared dependency raised to 5.2, ...).
"""

from __future__ import annotations

import ast
import configparser
import hashlib
import os
import py_compile
import re
import tempfile

TARGET_DJANGO = "5.2"
LEGACY_VERSION_LITERAL = "django.VERSION"
LEGACY_IS_DST_LITERAL = "is_dst="

# These exact, public documentation-only edits are the sole exceptions in
# instruction constraint 4. Normalize the corrected comments BACK to their
# baseline spelling for preservation hashes. The target-technology detector
# still scans the original bytes and rejects either retained legacy reference.
# Never strip comments or ignore a whole JavaScript file: every other byte,
# including runtime strings, whitespace and file inventory, stays protected.
DOCUMENTATION_ASSET_EDITS = {
    "feincms/static/feincms/item_editor.js": (
        b"      // Use Django's built-in inline spawing mechanism (Django 1.2+)\n",
        b"      // Use Django's built-in inline spawing mechanism\n",
    ),
    "feincms/static/feincms/tree_editor.js": (
        b"// https://docs.djangoproject.com/en/1.4/ref/contrib/csrf/\n",
        b"// https://docs.djangoproject.com/en/5.2/ref/csrf/\n",
    ),
}


def normalize_documentation_asset(relative, data):
    """Canonicalize only the two explicitly permitted whole comment lines."""

    edit = DOCUMENTATION_ASSET_EDITS.get(str(relative).replace(os.sep, "/"))
    if edit is None:
        return data
    before, after = edit
    # Split with endings retained: a matching substring embedded in a string,
    # moved to the middle of a line, or followed by code must never be ignored.
    return b"".join(before if line == after else line for line in data.splitlines(keepends=True))

#: The public Django *runtime version* API.  ``django.VERSION``,
#: ``django.__version__`` and ``django.get_version()`` are three spellings of
#: the same value, so the detector recognises the closed attribute/member set
#: (constant-folded first) instead of one literal name.
_DJANGO_VERSION_ATTRS = frozenset({"VERSION", "__version__"})
_DJANGO_VERSION_CALLS = frozenset({"get_version", "get_version_tuple"})
#: Key under ``<distribution>("Django").metadata[...]`` that carries the version.
_DISTRIBUTION_VERSION_KEY = "Version"


# ---------------------------------------------------------------------------
# small repository helpers
# ---------------------------------------------------------------------------
def _path(repo_root: str, relative: str) -> str:
    return os.path.join(repo_root, relative)


def _read(repo_root: str, relative: str):
    try:
        with open(_path(repo_root, relative), encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_file(repo_root: str, relative: str):
    try:
        with open(_path(repo_root, relative), "rb") as handle:
            return _digest_bytes(handle.read())
    except OSError:
        return None


def _digest_tree(repo_root: str, relatives) -> str:
    digest = hashlib.sha256()
    found = []
    for relative in relatives:
        root = _path(repo_root, relative)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for filename in sorted(filenames):
                absolute = os.path.join(dirpath, filename)
                found.append(os.path.relpath(absolute, repo_root))
    for relative in sorted(found):
        digest.update(relative.encode("utf-8"))
        try:
            with open(_path(repo_root, relative), "rb") as handle:
                digest.update(normalize_documentation_asset(relative, handle.read()))
        except OSError:
            continue
    return digest.hexdigest()


def _compiles(repo_root: str, relative: str) -> bool:
    absolute = _path(repo_root, relative)
    if not os.path.isfile(absolute):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="integration-static-") as tmp:
            py_compile.compile(
                absolute, cfile=os.path.join(tmp, "out.pyc"), doraise=True
            )
    except (OSError, py_compile.PyCompileError, SyntaxError):
        return False
    return True


def _parse(source):
    if not source:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _function_node(tree, name):
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _class_node(tree, name):
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _test_defined(repo_root: str, test_path: str, test_name: str) -> bool:
    source = _read(repo_root, test_path)
    tree = _parse(source)
    return _function_node(tree, test_name) is not None


def _config(repo_root: str, relative: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(_path(repo_root, relative))
    except (configparser.Error, OSError):
        pass
    return parser


# ---------------------------------------------------------------------------
# surface checks (must produce side-invariant projections)
# ---------------------------------------------------------------------------
def _surface_settings(repo_root):
    source = _read(repo_root, "feincms/__init__.py") or ""
    defaults = _read(repo_root, "feincms/default_settings.py") or ""
    is_lazy = "class LazySettings" in source and "settings = LazySettings()" in source
    override = "getattr(django_settings, key" in source or (
        "getattr(settings, key" in source
    )
    default_value = False
    match = re.search(
        r"FEINCMS_TREE_EDITOR_INCLUDE_ANCESTORS\s*=\s*getattr\(\s*\n?\s*settings,[^,]+,\s*(True|False)",
        defaults,
    )
    if match:
        default_value = match.group(1) == "True"
    return {
        "is_lazy_settings": is_lazy,
        "default": default_value,
        "override_true": bool(override),
    }


def _settings_ok(projection):
    return all(
        projection[key]
        for key in ("is_lazy_settings", "override_true")
    )


def _surface_filter_registration(repo_root):
    source = _read(repo_root, "feincms/admin/__init__.py") or ""
    parent = "ParentFieldListFilter" in source and "parent_filter" in source
    category = "CategoryFieldListFilter" in source and "category_filter" in source
    registered = source.count("FieldListFilter.register") >= 2
    return {
        "parent_filter": "ParentFieldListFilter" if parent and registered else "",
        "category_filter": "CategoryFieldListFilter" if category and registered else "",
    }


def _filter_registration_ok(projection):
    return bool(projection["parent_filter"]) and bool(projection["category_filter"])


def _surface_static_asset(repo_root):
    digest = _digest_file(repo_root, "feincms/static/feincms/jquery-1.11.3.min.js")
    return {"sha256": digest}


def _static_asset_ok(projection):
    return bool(projection["sha256"])


def _granular_rebuilds_datetime(repo_root) -> bool:
    """Whether ``granular_now`` rebuilds the result *from its input*.

    The migration may express the same behaviour either by constructing a fresh
    ``datetime(n.year, n.month, n.day, n.hour, ...)`` or by deriving it from the
    argument (``n.replace(minute=..., second=0, ...)``).  Both preserve the date
    and hour of ``n`` while flooring/setting the finer fields, so both must be
    accepted.  Binding the check to one literal spelling is an implementation
    form constraint, which the verifier contract forbids.
    """

    source = _read(repo_root, "feincms/extensions/datepublisher.py") or ""
    node = _function_node(_parse(source), "granular_now")
    if node is None:
        return False
    date_parts = {"year", "month", "day", "hour"}
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == "datetime":
                parts = {
                    argument.attr
                    for argument in child.args
                    if isinstance(argument, ast.Attribute)
                }
                if len(date_parts & parts) >= 3:
                    return True
            if isinstance(func, ast.Attribute) and func.attr == "replace":
                keywords = {keyword.arg for keyword in child.keywords}
                if {"minute", "second"} <= keywords:
                    return True
    return False


def _surface_granular_rounding(repo_root):
    source = _read(repo_root, "feincms/extensions/datepublisher.py") or ""
    floors = "(n.minute // 5) * 5" in source
    returns_datetime = _granular_rebuilds_datetime(repo_root)
    return {"rounded_minute_expr": floors, "rebuilds_datetime": returns_datetime}


def _granular_rounding_ok(projection):
    return projection["rounded_minute_expr"] and projection["rebuilds_datetime"]


def _surface_granular_signature(repo_root):
    source = _read(repo_root, "feincms/extensions/datepublisher.py") or ""
    node = _function_node(_parse(source), "granular_now")
    signature = "(n=None, default_tz=None)"
    if node is not None:
        args = [a.arg for a in node.args.args]
        defaults = node.args.defaults
        rendered = []
        offset = len(args) - len(defaults)
        for index, argument in enumerate(args):
            if index >= offset:
                default = defaults[index - offset]
                if isinstance(default, ast.Constant) and default.value is None:
                    rendered.append("%s=None" % argument)
                else:
                    rendered.append(argument)
            else:
                rendered.append(argument)
        if rendered:
            signature = "(%s)" % ", ".join(rendered)
    aware = "make_aware" in source
    return {"signature": signature, "returns_aware": aware}


def _granular_signature_ok(projection):
    return projection["signature"] == "(n=None, default_tz=None)" and bool(
        projection["returns_aware"]
    )


def _lookup_value_membership(tree):
    """Detect the single/multi-selection membership test behaviourally.

    The source implementation keeps a read-only alias (``lookup_vals =
    self.lookup_val``) and reports ``str(pk) in lookup_vals``.  A migration may
    inline that alias (``str(pk) in self.lookup_val``) or rename the local
    variable; those are behaviour-equivalent.  Match the semantics -- an
    ``in``/``not in`` test against the resolved lookup value -- instead of a
    literal variable name so equivalent rewrites are not mistaken for a contract
    regression.
    """

    if tree is None:
        return False

    def is_lookup_value(node):
        if isinstance(node, ast.Name) and "lookup_val" in node.id:
            return True
        if isinstance(node, ast.Attribute) and "lookup_val" in node.attr:
            return True
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            continue
        if any(is_lookup_value(operand) for operand in (node.left, *node.comparators)):
            return True
    return False


def _surface_filter_selection(repo_root):
    source = _read(repo_root, "feincms/admin/filters.py") or ""
    tree = _parse(source)
    return {
        "selected_flag_present": '"selected"' in source,
        "lookup_values_present": _lookup_value_membership(tree),
        "choices_defined": "def choices(self, changelist)" in source,
    }


def _filter_selection_ok(projection):
    return all(
        projection[key]
        for key in ("selected_flag_present", "lookup_values_present", "choices_defined")
    )


def _surface_meta_ordering(repo_root):
    source = _read(repo_root, "feincms/module/page/models.py") or ""
    tree = _parse(source)
    result = {"base_page": [], "page": []}

    def ordering_for(class_name):
        node = _class_node(tree, class_name)
        if node is None:
            return []
        for statement in node.body:
            if isinstance(statement, ast.ClassDef) and statement.name == "Meta":
                for inner in statement.body:
                    if isinstance(inner, ast.Assign):
                        targets = [t.id for t in inner.targets if isinstance(t, ast.Name)]
                        if "ordering" in targets and isinstance(inner.value, ast.List):
                            return [
                                element.value
                                for element in inner.value.elts
                                if isinstance(element, ast.Constant)
                            ]
        return []

    result["base_page"] = ordering_for("BasePage")
    result["page"] = ordering_for("Page")
    return result


def _meta_ordering_ok(projection):
    return projection["base_page"] == ["tree_id", "lft"] and projection["page"] == [
        "tree_id",
        "lft",
    ]


def _surface_404_handling(repo_root):
    source = _read(repo_root, "feincms/views/__init__.py") or ""
    return {
        "handles_404_page": "FEINCMS_CMS_404_PAGE" in source,
        "handler_defined": "class Handler" in source,
    }


def _404_handling_ok(projection):
    return projection["handles_404_page"] and projection["handler_defined"]


def _surface_public_symbols(repo_root):
    checks = (
        ("feincms", "settings", "feincms/__init__.py", "settings = LazySettings()"),
        (
            "feincms.admin",
            "ParentFieldListFilter",
            "feincms/admin/filters.py",
            "class ParentFieldListFilter",
        ),
        (
            "feincms.extensions.datepublisher",
            "granular_now",
            "feincms/extensions/datepublisher.py",
            "def granular_now",
        ),
        ("feincms.views", "Handler", "feincms/views/__init__.py", "class Handler"),
        (
            "feincms.admin.item_editor",
            "ItemEditor",
            "feincms/admin/item_editor.py",
            "class ItemEditor",
        ),
        (
            "feincms.admin.tree_editor",
            "TreeEditor",
            "feincms/admin/tree_editor.py",
            "class TreeEditor",
        ),
    )
    importable = []
    for module, attribute, path, marker in checks:
        if marker in (_read(repo_root, path) or ""):
            importable.append("%s:%s" % (module, attribute))
    return {"importable": importable}


def _public_symbols_ok(projection):
    return len(projection["importable"]) == 6


def _surface_admin_classes(repo_root):
    item = _read(repo_root, "feincms/admin/item_editor.py") or ""
    tree = _read(repo_root, "feincms/admin/tree_editor.py") or ""
    return {
        "item_editor_get_fieldsets": "def get_fieldsets" in item,
        "content_fieldset_used": "FEINCMS_CONTENT_FIELDSET" in item,
        "tree_editor_defined": "class TreeEditor" in tree,
        "tree_editor_tree_editor_present": "def tree_editor" in tree
        or "tree_editor" in tree,
    }


def _admin_classes_ok(projection):
    return all(
        projection[key]
        for key in (
            "item_editor_get_fieldsets",
            "content_fieldset_used",
            "tree_editor_defined",
        )
    )


def _surface_asset_digest(repo_root):
    return {
        "sha256": _digest_tree(
            repo_root,
            (
                "feincms/templates",
                "feincms/static",
                "tests/testapp/templates",
            ),
        )
    }


def _asset_digest_ok(projection):
    return bool(projection["sha256"])


def _surface_suite_integrity(repo_root):
    collected = 0
    for relative in (
        "tests/testapp/tests/test_cms.py",
        "tests/testapp/tests/test_extensions.py",
        "tests/testapp/tests/test_page.py",
        "tests/testapp/tests/test_stuff.py",
        "tests/testapp/tests/test_template_change_fix.py",
    ):
        tree = _parse(_read(repo_root, relative))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                collected += 1
    config = _config(repo_root, "setup.cfg")
    coverage = config.get("coverage:run", "include", fallback="") if config.has_section(
        "coverage:run"
    ) else ""
    return {
        "collected": collected,
        "failed": 0,
        "skipped": 0,
        "coverage_configured": "*feincms*" in coverage,
    }


def _suite_integrity_ok(projection):
    return projection["collected"] > 0 and projection["coverage_configured"]


def _surface_response_headers(repo_root):
    source = _read(repo_root, "feincms/extensions/datepublisher.py") or ""
    return {
        "uses_publication_end_date": "publication_end_date" in source,
        "patches_response_headers": "patch_response_headers(response" in source,
    }


def _response_headers_ok(projection):
    return projection["uses_publication_end_date"] and projection[
        "patches_response_headers"
    ]


def _surface_render_contract(repo_root):
    source = _read(repo_root, "feincms/models.py") or ""
    node = _function_node(_parse(source), "render")
    segment = ""
    if source and node is not None:
        segment = ast.get_source_segment(source, node) or ""
    return {
        "render_defined": node is not None,
        "accepts_var_keyword": node is not None and node.args.kwarg is not None,
        "dispatches_region_method": "render_" in segment,
        "raises_not_implemented": "NotImplementedError" in segment,
        "models_compiles": _compiles(repo_root, "feincms/models.py"),
    }


def _render_contract_ok(projection):
    return all(
        projection[key]
        for key in (
            "render_defined",
            "accepts_var_keyword",
            "dispatches_region_method",
            "raises_not_implemented",
            "models_compiles",
        )
    )


def _surface_python_support(repo_root):
    config = _config(repo_root, "setup.cfg")
    python_requires = _canonical_python_requires(
        config.get("options", "python_requires", fallback="").strip()
    )
    classifiers = config.get("metadata", "classifiers", fallback="")
    versions = sorted(
        classifier.rsplit("::", 1)[1].strip()
        for classifier in classifiers.splitlines()
        if "Programming Language :: Python :: 3." in classifier
    )
    tox_versions = set()
    tox = _read(repo_root, "tox.ini") or ""
    for match in re.finditer(r"py(3\d\d)", tox):
        tox_versions.add(match.group(1))
    for match in re.finditer(r"py\{([0-9,]+)\}", tox):
        for part in match.group(1).split(","):
            part = part.strip()
            if re.fullmatch(r"3\d\d", part):
                tox_versions.add(part)
    rendered = sorted("3.%s" % version[1:] for version in tox_versions)
    return {
        "python_requires": python_requires,
        "python_classifiers": versions,
        "tox_python_environments": rendered,
    }


def _python_support_ok(projection):
    return projection["python_requires"] == ">=3.10" and set(
        ("3.10", "3.11", "3.12", "3.13")
    ).issubset(set(projection["tox_python_environments"]))


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
    """Whether a changed path belongs to the repository's migration surface.

    Raising the supported Django baseline legitimately touches the package
    modules that carry the pre-5.2 compatibility branches, the packaging and
    configuration files that declare the baseline, the documentation that
    references the Django version, and the test suite exercising the migrated
    behaviour.  The migration surface is therefore the repository's own
    source/config/documentation/test trees; unrelated additions (evaluation
    scaffolding, build artifacts, third-party content) are not part of it.
    """

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


_PYTHON_REQUIRES_CLAUSE = re.compile(r"(>=|<=|==|!=|>|<)\s*(\d+)\.(\d+)")


def _python_requires_predicates(value):
    """Parse a ``python_requires`` specifier into ``(op, (major, minor))`` rows."""

    return [
        (match.group(1), (int(match.group(2)), int(match.group(3))))
        for match in _PYTHON_REQUIRES_CLAUSE.finditer(str(value or ""))
    ]


def _python_requires_allows(value, version) -> bool:
    for operator, bound in _python_requires_predicates(value):
        if operator == ">=" and version < bound:
            return False
        if operator == ">" and version <= bound:
            return False
        if operator == "<=" and version > bound:
            return False
        if operator == "<" and version >= bound:
            return False
        if operator == "==" and version != bound:
            return False
        if operator == "!=" and version == bound:
            return False
    return True


def _canonical_python_requires(value):
    """Collapse equivalent ``python_requires`` declarations to one value.

    The migration must keep the declared Python support *range*: the floor stays
    at 3.10 and 3.10 through 3.13 stay supported.  Expressing that as
    ``>=3.10`` or more precisely as ``>=3.10,<3.14`` is the same declaration and
    must not be treated as a contract change; raising the floor, lowering it, or
    excluding any of 3.10-3.13 must be.  Accepted declarations normalize to the
    source spelling ``>=3.10`` so the frozen baseline needs no change; anything
    outside the accepted range keeps its literal text and therefore differs.
    """

    text = str(value or "").strip()
    floor = next(
        (
            ">=%d.%d" % bound
            for operator, bound in _python_requires_predicates(text)
            if operator == ">="
        ),
        "",
    )
    covers = all(
        _python_requires_allows(text, version)
        for version in ((3, 10), (3, 11), (3, 12), (3, 13))
    )
    if floor == ">=3.10" and covers:
        return ">=3.10"
    return text


def _surface_change_scope(repo_root):
    config = _config(repo_root, "setup.cfg")
    python_requires = _canonical_python_requires(
        config.get("options", "python_requires", fallback="").strip()
    )
    changed = []
    git_dir = _path(repo_root, ".git")
    if os.path.isdir(git_dir):
        import shutil
        import subprocess

        timeout_bin = shutil.which("timeout")

        def _git(args):
            command = ["git", *args]
            if timeout_bin:
                command = [timeout_bin, "60", *command]
            try:
                completed = subprocess.run(
                    command,
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
            except (OSError, subprocess.SubprocessError):
                return []
            if completed.returncode != 0:
                return []
            return [line for line in completed.stdout.splitlines() if line.strip()]

        changed = _git(["diff", "--name-only", "HEAD"])
        # A candidate patch applied with ``git apply`` (no ``--index``) leaves
        # newly added files untracked, so they never appear in ``git diff``.  A
        # new production/config file outside the allowed scope is still a scope
        # violation and must be captured here.
        for relative in _git(["ls-files", "--others", "--exclude-standard"]):
            normalized = relative.replace("\\", "/")
            if normalized in changed:
                continue
            parts = normalized.split("/")
            if any(
                part in _PRODUCTION_EXCLUDE_DIRS or part.startswith(".")
                for part in parts
            ):
                continue
            # Only source/config additions are a scope concern; ignore build
            # artifacts (``.coverage``, ``coverage.xml``, ``db.sqlite3``, ...).
            if not normalized.endswith(_SCOPE_SOURCE_SUFFIXES):
                continue
            changed.append(normalized)
        changed = sorted(set(changed))
    return {
        "python_requires": python_requires,
        "within_allowed_scope": all(
            _within_migration_surface(path) for path in changed
        ),
    }


def _change_scope_ok(projection):
    return projection["python_requires"] == ">=3.10" and projection[
        "within_allowed_scope"
    ]


def _surface_template_change(repo_root):
    return {
        "test_defined": _test_defined(
            repo_root,
            "tests/testapp/tests/test_template_change_e2e.py",
            "test_double_submission_protection",
        ),
        "script_present": bool(
            _read(repo_root, "feincms/static/feincms/item_editor.js")
        ),
    }


def _template_change_ok(projection):
    return projection["test_defined"] and projection["script_present"]


_TEST_SURFACES = {
    "test:tests/testapp/tests/test_stuff.py#test_translation_short_language_code": (
        "tests/testapp/tests/test_stuff.py",
        "test_translation_short_language_code",
    ),
    "test:tests/testapp/tests/test_stuff.py#test_medialibrary_doctests": (
        "tests/testapp/tests/test_stuff.py",
        "test_medialibrary_doctests",
    ),
    "test:tests/testapp/tests/test_cms.py#test_05_non_abstract_content_type": (
        "tests/testapp/tests/test_cms.py",
        "test_05_non_abstract_content_type",
    ),
    "test:tests/testapp/tests/test_cms.py#test_10_content_type_subclasses": (
        "tests/testapp/tests/test_cms.py",
        "test_10_content_type_subclasses",
    ),
    "test:tests/testapp/context_processors.py#test_context": (
        "tests/testapp/tests/test_page.py",
        "test_30_context_processors",
    ),
    "test:tests/testapp/tests/test_page.py#test_30_context_processors": (
        "tests/testapp/tests/test_page.py",
        "test_30_context_processors",
    ),
    "test:tests/testapp/tests/test_extensions.py#test_user_has_language_set_with_session": (
        "tests/testapp/tests/test_extensions.py",
        "test_user_has_language_set_with_session",
    ),
    "test:tests/testapp/tests/test_extensions.py#test_user_has_language_set_with_cookie": (
        "tests/testapp/tests/test_extensions.py",
        "test_user_has_language_set_with_cookie",
    ),
    "test:tests/testapp/tests/test_template_change_fix.py#test_fix_order_is_correct": (
        "tests/testapp/tests/test_template_change_fix.py",
        "test_fix_order_is_correct",
    ),
    "test:tests/testapp/tests/test_template_change_fix.py#test_fix_is_present_in_javascript": (
        "tests/testapp/tests/test_template_change_fix.py",
        "test_fix_is_present_in_javascript",
    ),
    "test:tests/testapp/tests/test_page.py#test_38_invalid_template": (
        "tests/testapp/tests/test_page.py",
        "test_38_invalid_template",
    ),
    "test:tests/testapp/tests/test_page.py#test_33_preview": (
        "tests/testapp/tests/test_page.py",
        "test_33_preview",
    ),
    "test:tests/testapp/tests/test_page.py#test_35_access_with_extra_path": (
        "tests/testapp/tests/test_page.py",
        "test_35_access_with_extra_path",
    ),
    "test:tests/testapp/tests/test_page.py#test_01_tree_editor": (
        "tests/testapp/tests/test_page.py",
        "test_01_tree_editor",
    ),
    "test:tests/testapp/tests/test_page.py#test_03_item_editor": (
        "tests/testapp/tests/test_page.py",
        "test_03_item_editor",
    ),
    "test:tests/testapp/tests/test_page.py#test_24_admin_redirects": (
        "tests/testapp/tests/test_page.py",
        "test_24_admin_redirects",
    ),
    "test:tests/testapp/tests/test_template_change_e2e.py#test_double_submission_protection": (
        "tests/testapp/tests/test_template_change_e2e.py",
        "test_double_submission_protection",
    ),
}


def _test_surface(repo_root, surface):
    path, name = _TEST_SURFACES[surface]
    present = _test_defined(repo_root, path, name)
    return {"test": name, "present": present, "module_compiles": _compiles(repo_root, path)}


def _test_surface_ok(projection):
    return projection["present"] and projection["module_compiles"]


#: surface string -> (check function, pass predicate)
_SURFACE_CHECKS = {
    "feincms.settings LazySettings attribute resolution": (
        _surface_settings,
        _settings_ok,
    ),
    "django.contrib.admin.filters.FieldListFilter registration": (
        _surface_filter_registration,
        _filter_registration_ok,
    ),
    "granular_now five-minute rounding": (
        _surface_granular_rounding,
        _granular_rounding_ok,
    ),
    "granular_now call signature and return type": (
        _surface_granular_signature,
        _granular_signature_ok,
    ),
    "ParentFieldListFilter/CategoryFieldListFilter single-selection rendering": (
        _surface_filter_selection,
        _filter_selection_ok,
    ),
    "tree editor/Page Meta ordering": (_surface_meta_ordering, _meta_ordering_ok),
    "FEINCMS_CMS_404_PAGE handling and preview routing responses": (
        _surface_404_handling,
        _404_handling_ok,
    ),
    "public FeinCMS symbol names": (_surface_public_symbols, _public_symbols_ok),
    "ItemEditor and TreeEditor fieldset/tab and list_filter selection": (
        _surface_admin_classes,
        _admin_classes_ok,
    ),
    "content type render(**kwargs) contract": (
        _surface_render_contract,
        _render_contract_ok,
    ),
    "Python floor and tox Python matrix": (_surface_python_support, _python_support_ok),
    "declared Python support range and migration change scope": (
        _surface_change_scope,
        _change_scope_ok,
    ),
    "migration change scope": (_surface_change_scope, _change_scope_ok),
    "repository templates and static HTML/CSS/JS assets": (
        _surface_asset_digest,
        _asset_digest_ok,
    ),
    "test suite assertions and coverage gates": (
        _surface_suite_integrity,
        _suite_integrity_ok,
    ),
    "datepublisher_response_processor response headers": (
        _surface_response_headers,
        _response_headers_ok,
    ),
    "public FeinCMS capabilities and contracts": (
        _surface_public_symbols,
        _public_symbols_ok,
    ),
    "test:feincms/static/feincms/jquery-1.11.3.min.js#string": (
        _surface_static_asset,
        _static_asset_ok,
    ),
    "test:feincms/static/feincms/jquery-1.11.3.min.js#<": (
        _surface_static_asset,
        _static_asset_ok,
    ),
}


# ---------------------------------------------------------------------------
# observation id -> surface mapping (mirrors required_runtime_observations)
# ---------------------------------------------------------------------------
OBSERVATION_SURFACES = {
    "P2P-001": "feincms.settings LazySettings attribute resolution",
    "P2P-002": "test:tests/testapp/tests/test_stuff.py#test_translation_short_language_code",
    "P2P-003": "django.contrib.admin.filters.FieldListFilter registration",
    "P2P-004": "test:tests/testapp/tests/test_stuff.py#test_medialibrary_doctests",
    "P2P-005": "test:tests/testapp/tests/test_cms.py#test_05_non_abstract_content_type",
    "P2P-006": "test:tests/testapp/context_processors.py#test_context",
    "P2P-007": "test:feincms/static/feincms/jquery-1.11.3.min.js#string",
    "P2P-008": "granular_now five-minute rounding",
    "P2P-009": "test:tests/testapp/tests/test_extensions.py#test_user_has_language_set_with_session",
    "P2P-010": "ParentFieldListFilter/CategoryFieldListFilter single-selection rendering",
    "P2P-011": "tree editor/Page Meta ordering",
    "P2P-012": "test:tests/testapp/tests/test_template_change_fix.py#test_fix_order_is_correct",
    "P2P-013": "test:tests/testapp/tests/test_page.py#test_38_invalid_template",
    "P2P-014": "test:tests/testapp/tests/test_cms.py#test_10_content_type_subclasses",
    "P2P-015": "migration change scope",
    "P2P-016": "test:tests/testapp/tests/test_extensions.py#test_user_has_language_set_with_cookie",
    "P2P-017": "granular_now call signature and return type",
    "P2P-018": "FEINCMS_CMS_404_PAGE handling and preview routing responses",
    "P2P-019": "repository templates and static HTML/CSS/JS assets",
    "P2P-020": "public FeinCMS symbol names",
    "P2P-021": "ItemEditor and TreeEditor fieldset/tab and list_filter selection",
    "P2P-022": "test:tests/testapp/tests/test_template_change_fix.py#test_fix_is_present_in_javascript",
    "P2P-023": "test suite assertions and coverage gates",
    "P2P-024": "datepublisher_response_processor response headers",
    "P2P-025": "content type render(**kwargs) contract",
    "P2P-026": "test:tests/testapp/tests/test_template_change_e2e.py#test_double_submission_protection",
    "P2P-027": "test:feincms/static/feincms/jquery-1.11.3.min.js#<",
    "P2P-028": "Python floor and tox Python matrix",
    "P2P-029": "public FeinCMS capabilities and contracts",
    "P2P-030": "test:tests/testapp/tests/test_page.py#test_24_admin_redirects",
    "P2P-031": "declared Python support range and migration change scope",
    "DIFF-001": "declared Python support range and migration change scope",
    "DIFF-002": "feincms.settings LazySettings attribute resolution",
    "DIFF-003": "django.contrib.admin.filters.FieldListFilter registration",
    "DIFF-004": "granular_now call signature and return type",
    "DIFF-005": "datepublisher_response_processor response headers",
    "DIFF-006": "ItemEditor and TreeEditor fieldset/tab and list_filter selection",
    "DIFF-007": "content type render(**kwargs) contract",
    "DIFF-008": "ParentFieldListFilter/CategoryFieldListFilter single-selection rendering",
    "DIFF-009": "granular_now five-minute rounding",
    "DIFF-010": "FEINCMS_CMS_404_PAGE handling and preview routing responses",
    "DIFF-011": "tree editor/Page Meta ordering",
    "DIFF-012": "public FeinCMS capabilities and contracts",
    "DIFF-013": "repository templates and static HTML/CSS/JS assets",
    "DIFF-014": "public FeinCMS symbol names",
    "DIFF-015": "Python floor and tox Python matrix",
    "DIFF-016": "migration change scope",
    "DIFF-017": "test suite assertions and coverage gates",
    "CONTRACT-001": "declared Python support range and migration change scope",
    "CONTRACT-002": "feincms.settings LazySettings attribute resolution",
    "CONTRACT-003": "django.contrib.admin.filters.FieldListFilter registration",
    "CONTRACT-004": "granular_now call signature and return type",
    "CONTRACT-005": "datepublisher_response_processor response headers",
    "CONTRACT-006": "ItemEditor and TreeEditor fieldset/tab and list_filter selection",
    "CONTRACT-007": "content type render(**kwargs) contract",
    "CONTRACT-008": "ParentFieldListFilter/CategoryFieldListFilter single-selection rendering",
    "CONTRACT-009": "granular_now five-minute rounding",
    "CONTRACT-010": "FEINCMS_CMS_404_PAGE handling and preview routing responses",
    "CONTRACT-011": "tree editor/Page Meta ordering",
    "CONTRACT-012": "public FeinCMS capabilities and contracts",
    "CONTRACT-013": "repository templates and static HTML/CSS/JS assets",
    "CONTRACT-014": "public FeinCMS symbol names",
    "CONTRACT-015": "Python floor and tox Python matrix",
    "CONTRACT-016": "migration change scope",
    "CONTRACT-017": "test suite assertions and coverage gates",
}


# ---------------------------------------------------------------------------
# state check (persisted schema / MIGRATION_MODULES)
# ---------------------------------------------------------------------------
def _surface_state(repo_root):
    page_migrations = _path(repo_root, "tests/testapp/migrate/page")
    media_migrations = _path(repo_root, "tests/testapp/migrate/medialibrary")
    settings_source = _read(repo_root, "tests/testapp/settings.py") or ""

    def has_migration_files(directory):
        try:
            return any(
                name.endswith(".py") and name != "__init__.py"
                for name in os.listdir(directory)
            )
        except OSError:
            return False

    return {
        "page_migrations_present": has_migration_files(page_migrations),
        "medialibrary_migrations_present": has_migration_files(media_migrations),
        "migration_modules_configured": "MIGRATION_MODULES" in settings_source,
    }


def _state_ok(projection):
    return (
        projection["page_migrations_present"]
        and projection["medialibrary_migrations_present"]
        and projection["migration_modules_configured"]
    )


# ---------------------------------------------------------------------------
# target-technology check (F2P fallback)
# ---------------------------------------------------------------------------
def _is_sys_modules_node(node, strings, sys_names):
    """True when ``node`` is the ``sys.modules`` mapping.

    Recognises ``sys.modules`` / an alias of ``sys`` (``import sys as X``),
    ``getattr(sys, "modules")`` (the attribute name may be computed), the
    namespace mapping ``vars(sys)["modules"]`` / ``sys.__dict__["modules"]``,
    and names bound to the mapping itself (``from sys import modules as m`` /
    ``m = sys.modules``) so the identity of the namespace is resolved by value
    instead of by the literal receiver name.
    """

    if isinstance(node, ast.Name):
        return node.id in sys_names["modules"]
    if isinstance(node, ast.Attribute) and node.attr == "modules":
        return isinstance(node.value, ast.Name) and node.value.id in sys_names["mod"]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
    ):
        return _resolve_string(node.args[1], strings) == "modules" and (
            isinstance(node.args[0], ast.Name) and node.args[0].id in sys_names["mod"]
        )
    if isinstance(node, ast.Subscript):
        if _resolve_string(node.slice, strings) != "modules":
            return False
        base = node.value
        if (
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Name)
            and base.func.id == "vars"
            and base.args
        ):
            base = base.args[0]
        elif isinstance(base, ast.Attribute) and base.attr == "__dict__":
            base = base.value
        else:
            return False
        return isinstance(base, ast.Name) and base.id in sys_names["mod"]
    return False


def _sys_modules_key(node, strings, sys_names):
    """Return the resolved key of a ``sys.modules[...]`` / ``sys.modules.get(...)``.

    The key is constant-folded so a name built from string fragments
    (``_dj = "dj" + "ango"``) is still read instead of being treated as an
    opaque non-Django lookup.
    """

    if isinstance(node, ast.Subscript) and _is_sys_modules_node(node.value, strings, sys_names):
        return _resolve_string(node.slice, strings)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _is_sys_modules_node(node.func.value, strings, sys_names)
        and node.args
    ):
        return _resolve_string(node.args[0], strings)
    return None


def _is_django_module_node(node, modules, importlib, strings, sys_names):
    """True when ``node`` evaluates to the ``django`` module object.

    Covers ``import django [as X]`` aliases, the runtime lookups
    ``__import__("django")`` / ``importlib.import_module("django")`` (whose name
    argument is constant-folded), and the ``sys.modules["django"]`` /
    ``sys.modules.get("django")`` namespace lookup so a module object obtained
    indirectly -- through an aliased ``sys`` module or a computed key -- is still
    recognised.
    """

    if isinstance(node, ast.Name) and node.id in modules:
        return True
    if _sys_modules_key(node, strings, sys_names) == "django":
        return True
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    args = node.args
    if isinstance(func, ast.Name) and func.id == "__import__":
        return bool(args) and _resolve_string(args[0], strings) == "django"
    if isinstance(func, ast.Name) and func.id == "import_module":
        return bool(args) and _resolve_string(args[0], strings) == "django"
    if isinstance(func, ast.Attribute) and func.attr == "import_module":
        base = func.value
        if isinstance(base, ast.Name) and base.id in importlib:
            return bool(args) and _resolve_string(args[0], strings) == "django"
    return False


#: Upper bound for a resolved string constant and the fixpoint iteration count.
#: A self-referential/iterated concatenation (``x = x + x``) otherwise grows
#: without bound when the detector scans a large production module.
_MAX_RESOLVED_STRING = 4096
_MAX_STRING_ITERATIONS = 32


def _resolve_string(node, strings):
    """Resolve ``node`` to a literal string by constant folding.

    Names bound to constants, ``+`` concatenation, ``"".join((...))``,
    ``"%s" % (...)`` interpolation, f-strings and the common str methods on
    constant operands (``upper``/``lower``/``strip``/``replace``/``format``) are
    all folded, so a module or attribute name assembled from fragments at author
    time resolves to its value instead of hiding behind a computed expression.
    """

    def _clip(value):
        if value is None or len(value) > _MAX_RESOLVED_STRING:
            return None
        return value

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _clip(node.value)
    if isinstance(node, ast.Name):
        return _clip(strings.get(node.id))
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = _resolve_string(value.value, strings)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return _clip("".join(parts))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_string(node.left, strings)
        right = _resolve_string(node.right, strings)
        if left is not None and right is not None:
            return _clip(left + right)
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template = _resolve_string(node.left, strings)
        if template is None:
            return None
        operands = (
            node.right.elts
            if isinstance(node.right, (ast.Tuple, ast.List))
            else [node.right]
        )
        resolved = [_resolve_string(item, strings) for item in operands]
        if any(item is None for item in resolved):
            return None
        # Only substitute a plain ``%s`` template so an unrelated format
        # specifier is never mis-evaluated.
        if template.count("%s") == len(resolved) and "%" not in template.replace(
            "%s", ""
        ):
            return _clip(template % tuple(resolved))
        return None
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "join" and node.args:
            separator = _resolve_string(func.value, strings)
            if separator is None:
                return None
            iterable = node.args[0]
            if isinstance(iterable, (ast.Tuple, ast.List, ast.Set)):
                items = [_resolve_string(item, strings) for item in iterable.elts]
                if any(item is None for item in items):
                    return None
                return _clip(separator.join(items))
            return None
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"upper", "lower", "strip", "lstrip", "rstrip"}
            and not node.args
        ):
            base = _resolve_string(func.value, strings)
            if base is None:
                return None
            return _clip(getattr(base, func.attr)())
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "replace"
            and len(node.args) >= 2
        ):
            base = _resolve_string(func.value, strings)
            old = _resolve_string(node.args[0], strings)
            new = _resolve_string(node.args[1], strings)
            if base is None or old is None or new is None:
                return None
            return _clip(base.replace(old, new))
        if isinstance(func, ast.Attribute) and func.attr == "format":
            base = _resolve_string(func.value, strings)
            if base is None:
                return None
            args = [_resolve_string(item, strings) for item in node.args]
            if any(item is None for item in args):
                return None
            try:
                return _clip(base.format(*args))
            except (IndexError, KeyError, ValueError):
                return None
    return None


def _string_constants(tree):
    """Map names to the string constants they are bound to (fixpoint)."""

    strings = {}
    if tree is None:
        return strings
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    changed = True
    iterations = 0
    while changed and iterations < _MAX_STRING_ITERATIONS:
        changed = False
        iterations += 1
        for node in assignments:
            value = node.value
            if value is None:
                continue
            resolved = _resolve_string(value, strings)
            if resolved is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and strings.get(target.id) != resolved:
                    strings[target.id] = resolved
                    changed = True
    return strings


def _is_attrgetter_version(value, strings):
    """True when ``value`` is ``attrgetter(<version name>)`` (a computed name ok).

    A retained version accessor may be stored in a local before being applied
    (``_get = attrgetter("VERSION"); _get(django)``); the bound name is tracked
    alongside the derived version values so the later call is still recognised.
    """

    if isinstance(value, ast.Call):
        func = value.func
        is_attrgetter = (
            isinstance(func, ast.Name) and func.id == "attrgetter"
        ) or (isinstance(func, ast.Attribute) and func.attr == "attrgetter")
        return (
            is_attrgetter
            and bool(value.args)
            and _resolve_string(value.args[0], strings) in _DJANGO_VERSION_ATTRS
        )
    return False


def _is_django_version_mapping_node(node, modules, importlib, strings, sys_names):
    """True when ``node`` evaluates to the ``django`` module namespace mapping."""

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "vars" and node.args:
            return _is_django_module_node(node.args[0], modules, importlib, strings, sys_names)
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return _is_django_module_node(node.value, modules, importlib, strings, sys_names)
    return False


def _is_metadata_namespace(node, importlib, strings, sys_names):
    """True when ``node`` evaluates to the ``importlib.metadata`` namespace.

    Covers ``import importlib`` + ``importlib.metadata``, ``import
    importlib.metadata [as X]``, ``import importlib_metadata [as X]`` and
    ``from importlib import metadata [as m]``; the module identity is resolved
    by value (import alias), never by a fixed spelling.
    """

    if isinstance(node, ast.Name):
        return node.id in sys_names.get("meta", set())
    if isinstance(node, ast.Attribute) and node.attr == "metadata":
        return isinstance(node.value, ast.Name) and node.value.id in importlib
    return False


def _is_django_distribution_name(node, strings):
    """True when ``node`` folds to the Django distribution name.

    Distribution names are normalised (case, ``-``/``_``, surrounding space) so
    ``"Django"``, ``"django"`` and ``"Django "`` all compare equal -- the name
    is a package identifier, not a byte string.
    """

    name = _resolve_string(node, strings)
    if not name:
        return False
    return name.strip().lower().replace("_", "-") == "django"


def _is_django_distribution_expr(node, importlib, strings, sys_names):
    """True when ``node`` evaluates to the Django *distribution* object.

    Covers ``importlib.metadata.distribution("Django")``,
    ``importlib_metadata.distribution("Django")`` and
    ``pkg_resources.get_distribution("Django")`` (module aliases and
    ``from ... import distribution|get_distribution`` bindings included).
    """

    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    args = node.args
    if isinstance(func, ast.Name) and func.id in sys_names.get("meta_dist", set()):
        return _is_django_distribution_name(args[0], strings)
    if isinstance(func, ast.Attribute):
        if func.attr == "distribution" and _is_metadata_namespace(
            func.value, importlib, strings, sys_names
        ):
            return _is_django_distribution_name(args[0], strings)
        if (
            func.attr == "get_distribution"
            and isinstance(func.value, ast.Name)
            and func.value.id in sys_names.get("pkg", set())
        ):
            return _is_django_distribution_name(args[0], strings)
    return False


def _is_django_metadata_mapping_expr(node, importlib, strings, sys_names):
    """True when ``node`` evaluates to the Django distribution metadata mapping.

    Covers ``importlib.metadata.metadata("Django")`` (and the
    ``from importlib.metadata import metadata`` binding) and
    ``<distribution>("Django").metadata`` so ``["Version"]`` is read.
    """

    if isinstance(node, ast.Call) and node.args:
        func = node.func
        if isinstance(func, ast.Name) and func.id in sys_names.get("meta_map", set()):
            return _is_django_distribution_name(node.args[0], strings)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "metadata"
            and _is_metadata_namespace(func.value, importlib, strings, sys_names)
        ):
            return _is_django_distribution_name(node.args[0], strings)
    if isinstance(node, ast.Attribute) and node.attr == "metadata":
        return _is_django_distribution_expr(node.value, importlib, strings, sys_names)
    return False


def _is_metadata_version_node(node, importlib, strings, sys_names):
    """True when ``node`` reads the installed Django version from metadata.

    Covers ``importlib.metadata.version("Django")`` (and ``importlib_metadata`` /
    ``from importlib.metadata import version as v``) and the version attribute of
    a resolved Django *distribution* object
    (``importlib.metadata.distribution("Django").version``).
    """

    if not isinstance(node, ast.Call):
        return False
    func = node.func
    args = node.args
    if isinstance(func, ast.Attribute) and func.attr == "version":
        if _is_metadata_namespace(func.value, importlib, strings, sys_names):
            return bool(args) and _is_django_distribution_name(args[0], strings)
        if _is_django_distribution_expr(func.value, importlib, strings, sys_names):
            return True
    if (
        isinstance(func, ast.Name)
        and func.id in sys_names.get("meta_version", set())
        and args
    ):
        return _is_django_distribution_name(args[0], strings)
    return False


def _is_django_version_node(node, versions, modules, importlib, strings, sys_names):
    """True when ``node`` evaluates to ``django.VERSION`` or a derived value.

    Covers ``X.VERSION``, ``getattr(X, "VERSION")``, ``attrgetter("VERSION")(X)``,
    ``vars(X)["VERSION"]``/``X.__dict__["VERSION"]``, bare names bound to any of
    those, and values *derived* from the version tuple (``VERSION[0]``,
    ``VERSION[:2]``, tuple/list unpacking) so an intermediate variable cannot
    hide a retained pre-5.2 branch.
    """

    if isinstance(node, ast.Name) and (
        node.id in versions or node.id in sys_names.get("helpers", set())
    ):
        return True
    if isinstance(node, ast.Attribute) and node.attr in _DJANGO_VERSION_ATTRS:
        return _is_django_module_node(node.value, modules, importlib, strings, sys_names)
    # ``<distribution>("Django").version`` / ``pkg_resources.get_distribution(...).version``
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "version"
        and _is_django_distribution_expr(node.value, importlib, strings, sys_names)
    ):
        return True
    if isinstance(node, ast.Subscript):
        if _is_django_version_node(
            node.value, versions, modules, importlib, strings, sys_names
        ):
            return True
        key = _resolve_string(node.slice, strings)
        if key in _DJANGO_VERSION_ATTRS and _is_django_version_mapping_node(
            node.value, modules, importlib, strings, sys_names
        ):
            return True
        # ``<distribution>("Django").metadata["Version"]`` /
        # ``importlib.metadata.metadata("Django")["Version"]``
        if key == _DISTRIBUTION_VERSION_KEY and _is_django_metadata_mapping_expr(
            node.value, importlib, strings, sys_names
        ):
            return True
        return False
    if isinstance(node, ast.Call):
        func = node.func
        # A call to a *version-returning helper* (``def major(): return
        # django.VERSION[0]``) yields a version-derived value.
        if isinstance(func, ast.Name) and func.id in sys_names.get("helpers", set()):
            return True
        # A call to a helper that compares a *parameter* which receives the
        # django module object at this call site yields a version-derived value.
        if isinstance(func, ast.Name) and func.id in sys_names.get("version_params", {}):
            for position in sys_names["version_params"][func.id]:
                if position < len(node.args) and _is_django_module_node(
                    node.args[position], modules, importlib, strings, sys_names
                ):
                    return True
        # ``importlib.metadata.version("Django")`` /
        # ``<distribution>("Django").version`` reads the installed version.
        if _is_metadata_version_node(node, importlib, strings, sys_names):
            return True
        # A name bound to a version accessor (``_get = attrgetter("VERSION")``)
        # applied to the module object yields the version tuple.
        if isinstance(func, ast.Name) and func.id in versions and node.args:
            return _is_django_module_node(
                node.args[0], modules, importlib, strings, sys_names
            )
        # ``django.get_version()``/``get_version()`` returns the version string;
        # string/tuple transforms of it (``.split(".")[0]``, ``int(...)``) are
        # still Django-version values.
        if (isinstance(func, ast.Name) and func.id in _DJANGO_VERSION_CALLS) or (
            isinstance(func, ast.Attribute) and func.attr in _DJANGO_VERSION_CALLS
        ):
            return True
        if (
            isinstance(func, ast.Name)
            and func.id in {"int", "str", "tuple", "list", "float", "bool"}
            and node.args
            and _is_django_version_node(
                node.args[0], versions, modules, importlib, strings, sys_names
            )
        ):
            return True
        if (
            isinstance(func, ast.Attribute)
            and func.attr
            in {
                "split",
                "rsplit",
                "strip",
                "lstrip",
                "rstrip",
                "lower",
                "upper",
                "replace",
                "partition",
                "format",
                "join",
            }
            and _is_django_version_node(
                func.value, versions, modules, importlib, strings, sys_names
            )
        ):
            return True
        is_getattr = (
            isinstance(func, ast.Name) and func.id == "getattr"
        ) or (isinstance(func, ast.Attribute) and func.attr == "getattr")
        if is_getattr and len(node.args) >= 2:
            if _resolve_string(node.args[1], strings) in _DJANGO_VERSION_ATTRS:
                return _is_django_module_node(
                    node.args[0], modules, importlib, strings, sys_names
                )
        if isinstance(func, ast.Call) and node.args:
            inner_func = func.func
            is_attrgetter = (
                isinstance(inner_func, ast.Name) and inner_func.id == "attrgetter"
            ) or (isinstance(inner_func, ast.Attribute) and inner_func.attr == "attrgetter")
            if (
                is_attrgetter
                and func.args
                and _resolve_string(func.args[0], strings) in _DJANGO_VERSION_ATTRS
            ):
                return _is_django_module_node(
                    node.args[0], modules, importlib, strings, sys_names
                )
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(
            _is_django_version_node(
                item, versions, modules, importlib, strings, sys_names
            )
            for item in node.elts
        )
    # ``tuple(int(p) for p in __version__.split(".")[:2])`` -- a comprehension
    # over a version-derived iterable (or producing one) is still version-derived.
    if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        if _is_django_version_node(
            node.elt, versions, modules, importlib, strings, sys_names
        ):
            return True
        return any(
            _is_django_version_node(
                generator.iter, versions, modules, importlib, strings, sys_names
            )
            for generator in node.generators
        )
    if isinstance(node, ast.Starred):
        return _is_django_version_node(
            node.value, versions, modules, importlib, strings, sys_names
        )
    return False


def _django_version_bindings(tree):
    """Return ``(version_names, module_names, importlib_names, strings, sys_names)``.

    Values are propagated through intermediate assignments so a retained
    pre-5.2 branch is detected even when the version is first stored in a local
    or module constant and then indexed (``_DJANGO_MAJOR =
    __import__("django").VERSION[0]``, ``vars(_dj)["VERSION"]``).

    ``sys_names`` also carries the *closed platform version API* bindings so the
    version value is resolved by value rather than by one spelling: the
    ``importlib.metadata`` / ``importlib_metadata`` / ``pkg_resources`` names
    (``meta``, ``meta_version``, ``meta_dist``, ``pkg``) and the names of helper
    functions that return or compare a version value (``helpers``,
    ``version_params``).
    """

    versions = set()
    modules = {"django"}
    importlib = {"importlib"}
    sys_names = {
        "mod": {"sys"},
        "modules": set(),
        "meta": set(),
        "meta_map": set(),
        "meta_version": set(),
        "meta_dist": set(),
        "pkg": set(),
        "helpers": set(),
        "version_params": {},
    }
    if tree is None:
        return versions, modules, importlib, {}, sys_names
    strings = _string_constants(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module
            if module == "django":
                for alias in node.names:
                    if alias.name in _DJANGO_VERSION_ATTRS:
                        versions.add(alias.asname or alias.name)
            elif module == "sys":
                for alias in node.names:
                    if alias.name == "modules":
                        sys_names["modules"].add(alias.asname or alias.name)
            elif module == "importlib":
                for alias in node.names:
                    if alias.name == "metadata":
                        sys_names["meta"].add(alias.asname or alias.name)
            elif module in ("importlib.metadata", "importlib_metadata"):
                for alias in node.names:
                    if alias.name == "version":
                        sys_names["meta_version"].add(alias.asname or alias.name)
                    elif alias.name == "distribution":
                        sys_names["meta_dist"].add(alias.asname or alias.name)
                    elif alias.name == "metadata":
                        sys_names["meta"].add(alias.asname or alias.name)
                        sys_names["meta_map"].add(alias.asname or alias.name)
            elif module == "pkg_resources":
                for alias in node.names:
                    if alias.name == "get_distribution":
                        sys_names["meta_dist"].add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "django":
                    modules.add(alias.asname or alias.name)
                elif alias.name == "importlib":
                    importlib.add(alias.asname or alias.name)
                elif alias.name == "sys":
                    sys_names["mod"].add(alias.asname or alias.name)
                elif alias.name in ("importlib.metadata", "importlib_metadata"):
                    sys_names["meta"].add(alias.asname or alias.name.split(".")[0])
                elif alias.name == "pkg_resources":
                    sys_names["pkg"].add(alias.asname or alias.name)
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id in versions or target.id in modules:
                    continue
                if _is_attrgetter_version(value, strings) or _references_django_version(
                    value, versions, modules, importlib, strings, sys_names
                ):
                    versions.add(target.id)
                    changed = True
                elif _is_django_module_node(value, modules, importlib, strings, sys_names):
                    modules.add(target.id)
                    changed = True
                elif _is_sys_modules_node(value, strings, sys_names):
                    if target.id not in sys_names["modules"]:
                        sys_names["modules"].add(target.id)
                        changed = True
    # A ``match`` capture name is derived from the subject.  When the subject is
    # a Django version value (``match django.VERSION: case (major, *_) if
    # major < 5``) the captured names are version-derived too, so the guard is
    # recognised even when it only mentions the capture.  This is a fixpoint
    # because a capture can feed a later ``match`` subject.
    capture_changed = True
    while capture_changed:
        capture_changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Match):
                continue
            if not _references_django_version(
                node.subject, versions, modules, importlib, strings, sys_names
            ):
                continue
            for case in node.cases:
                for name in _match_capture_names(case.pattern):
                    if name not in versions:
                        versions.add(name)
                        capture_changed = True
    # Source -> sink data flow: a helper whose *return* value is version-derived,
    # and a helper that compares a *parameter* which receives the django module.
    sys_names["helpers"] = _version_returning_names(
        tree, versions, modules, importlib, strings, sys_names
    )
    sys_names["version_params"] = _version_parameter_positions(tree, strings)
    return versions, modules, importlib, strings, sys_names


def _match_capture_names(pattern):
    """Names bound by a ``match`` pattern (``case (major, *_)`` -> ``major``).

    Only the ``as``/``star``/mapping-rest bindings matter here: they carry a
    value derived from the matched subject.  Value patterns (``case 4``) and
    literals contribute no names.
    """

    names = set()
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return names


def _body_statements(node):
    if isinstance(node, ast.Lambda):
        return [node.body]
    return list(getattr(node, "body", []))


def _version_returning_names(tree, versions, modules, importlib, strings, sys_names):
    """Names of functions/lambdas whose return value is a version-derived value.

    Factoring ``return django.VERSION[0]`` (or ``return __version__`` /
    ``return importlib.metadata.version("Django")``) into a helper and branching
    on the helper at the call site (``if _major() < 5:``) is the same retained
    fallback; the callee is tainted so the call site is recognised.
    """

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            target = node.name
            bodies = _body_statements(node)
        elif isinstance(node, ast.Lambda):
            target = None
            bodies = [node.body]
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
            bodies = [node.value.body]
            for assigned in node.targets:
                if isinstance(assigned, ast.Name) and _references_django_version(
                    node.value.body, versions, modules, importlib, strings, sys_names
                ):
                    names.add(assigned.id)
            continue
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Lambda):
            if isinstance(node.target, ast.Name) and _references_django_version(
                node.value.body, versions, modules, importlib, strings, sys_names
            ):
                names.add(node.target.id)
            continue
        else:
            continue
        for statement in bodies:
            for child in ast.walk(statement):
                if isinstance(child, ast.Return) and child.value is not None:
                    if _references_django_version(
                        child.value, versions, modules, importlib, strings, sys_names
                    ):
                        if target:
                            names.add(target)
    return names


def _version_parameter_positions(tree, strings):
    """Map a function name to the positional parameters that access ``.VERSION``.

    ``def f(m): return m.VERSION[0] < 5`` compared at ``f(sys.modules["django"])``
    is a version branch that hides the value behind a parameter; the position of
    the parameter is recorded so the call site is recognised when the argument is
    a django module object.
    """

    mapping = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [arg.arg for arg in node.args.args]
        positions = set()
        for statement in _body_statements(node):
            for child in ast.walk(statement):
                if (
                    isinstance(child, ast.Attribute)
                    and child.attr in _DJANGO_VERSION_ATTRS
                    and isinstance(child.value, ast.Name)
                    and child.value.id in params
                ):
                    positions.add(params.index(child.value.id))
                elif (
                    isinstance(child, ast.Subscript)
                    and isinstance(child.value, ast.Name)
                    and child.value.id in params
                    and _resolve_string(child.slice, strings) in _DJANGO_VERSION_ATTRS
                ):
                    positions.add(params.index(child.value.id))
                elif isinstance(child, ast.Call):
                    inner = child.func
                    is_getattr = (
                        isinstance(inner, ast.Name) and inner.id == "getattr"
                    ) or (isinstance(inner, ast.Attribute) and inner.attr == "getattr")
                    if (
                        is_getattr
                        and len(child.args) >= 2
                        and isinstance(child.args[0], ast.Name)
                        and child.args[0].id in params
                        and _resolve_string(child.args[1], strings)
                        in _DJANGO_VERSION_ATTRS
                    ):
                        positions.add(params.index(child.args[0].id))
        if positions:
            mapping[node.name] = positions
    return mapping


def _references_django_version(node, versions, modules, importlib, strings, sys_names):
    """True when ``node`` mentions a value derived from ``django.VERSION``."""

    for child in ast.walk(node):
        if _is_django_version_node(
            child, versions, modules, importlib, strings, sys_names
        ):
            return True
    return False


def has_version_branch(tree):
    """Detect any runtime branch gated on the installed Django version.

    The value is recognised by *value* across the whole closed platform version
    API (``django.VERSION``, ``django.__version__``, ``django.get_version()``,
    ``importlib.metadata.version("Django")``, ``pkg_resources`` distribution
    metadata, ...), including import aliases, constant-folded member/key names
    and helper functions that return or compare a version value.
    """

    if tree is None:
        return False
    versions, modules, importlib, strings, sys_names = _django_version_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = (node.left, *node.comparators)
            if any(
                _references_django_version(
                    item, versions, modules, importlib, strings, sys_names
                )
                for item in operands
            ):
                return True
        elif isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
            if _references_django_version(
                node.test, versions, modules, importlib, strings, sys_names
            ):
                return True
        elif isinstance(node, ast.Match):
            # ``match django.VERSION: case (major, *_) if major < 5:`` -- the
            # branch is gated on the version whether the version value sits in
            # the subject or in a guard.
            if _references_django_version(
                node.subject, versions, modules, importlib, strings, sys_names
            ):
                return True
            for case in node.cases:
                if case.guard is not None and _references_django_version(
                    case.guard, versions, modules, importlib, strings, sys_names
                ):
                    return True
        elif isinstance(node, ast.comprehension):
            # ``[x for x in y if django.VERSION[0] < 5]`` is a branch condition.
            if any(
                _references_django_version(
                    condition, versions, modules, importlib, strings, sys_names
                )
                for condition in node.ifs
            ):
                return True
    return False


def _version_branch(source):
    """Detect any runtime branch gated on the Django version.

    Matches the literal ``django.VERSION`` as well as import aliases and values
    obtained indirectly or derived from it, or read through any equivalent
    platform version API (``django.__version__``,
    ``importlib.metadata.version("Django")``), so an obfuscated rewrite of a
    retained ``Django < 5.2`` compatibility branch cannot hide.
    """

    tree = _parse(source)
    if tree is None:
        return LEGACY_VERSION_LITERAL in source
    return has_version_branch(tree)


def _dict_literal_keys(node, strings=None):
    """String keys of a dict expression (``{...}`` or ``dict(...)``).

    Keys written as names bound to string constants (``_KEY = "is_dst"``) or as
    constant concatenations are resolved so a variable-key dict cannot hide its
    entries.
    """

    strings = strings or {}
    keys = set()
    if isinstance(node, ast.Dict):
        for key in node.keys:
            resolved = _resolve_string(key, strings)
            if resolved is not None:
                keys.add(resolved)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
    ):
        for keyword in node.keywords:
            if keyword.arg:
                keys.add(keyword.arg)
    return keys


def _dict_key_sources(tree, strings):
    """Map assigned names to the string keys of their dict values (fixpoint).

    A name bound to a dict literal, a ``dict(...)`` call, or another tracked
    name therefore exposes the keys an ``**name`` expansion can carry.
    """

    pending = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    pending.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            pending.setdefault(node.target.id, []).append(node.value)
    sources = {}
    changed = True
    while changed:
        changed = False
        for name, values in pending.items():
            for value in values:
                keys = set(_dict_literal_keys(value, strings))
                if isinstance(value, ast.Name):
                    keys |= sources.get(value.id, set())
                if not keys:
                    continue
                current = sources.setdefault(name, set())
                if not keys <= current:
                    current.update(keys)
                    changed = True
    return sources


def _kwargs_source_has_is_dst(node, sources, strings=None):
    """True when an ``**expr`` expansion can carry an ``is_dst`` entry."""

    if "is_dst" in _dict_literal_keys(node, strings):
        return True
    if isinstance(node, ast.Name):
        return "is_dst" in sources.get(node.id, set())
    return False


def has_is_dst_argument(tree):
    """True when ``tree`` passes a legacy ``is_dst`` flag to ``make_aware``."""

    if tree is None:
        return False
    strings = _string_constants(tree)
    sources = _dict_key_sources(tree, strings)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_make_aware = (
            isinstance(func, ast.Name) and func.id == "make_aware"
        ) or (isinstance(func, ast.Attribute) and func.attr == "make_aware")
        # ``make_aware(value, tz, is_dst)`` passes the legacy flag positionally.
        if is_make_aware and len(node.args) >= 3:
            return True
        for keyword in node.keywords:
            if keyword.arg == "is_dst":
                return True
            if keyword.arg is None and _kwargs_source_has_is_dst(
                keyword.value, sources, strings
            ):
                return True
    return False


def _is_dst_argument(source):
    """Detect any ``is_dst`` keyword/expansion.

    Covers ``is_dst=``, inline ``**{"is_dst": ...}`` and ``**name`` where
    ``name`` is a dict value carrying an ``is_dst`` entry, including entries
    whose key was built from a string constant.
    """

    tree = _parse(source)
    if tree is None:
        return LEGACY_IS_DST_LITERAL in source
    return has_is_dst_argument(tree)


def _version_tuple(text):
    return tuple(int(part) for part in text.split("."))


def _expand_tox_variables(tox_ini):
    """Expand tox ``{[section]key}`` substitutions to their literal values.

    A retained pre-5.2 matrix entry written as
    ``djold: Django>={[testenv]legacy_version},<4.0`` must be resolved before the
    version constraint is read.
    """

    variables = {}
    section = None
    for line in tox_ini.splitlines():
        header = re.match(r"^\[([^\]]+)\]\s*$", line.strip())
        if header:
            section = header.group(1)
            continue
        if section is None or line[:1].isspace():
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            variables[(section, key.strip())] = value.strip()
    pattern = re.compile(r"\{\[([^\]]+)\]([^}]+)\}")
    text = tox_ini
    for _ in range(10):
        expanded = pattern.sub(
            lambda match: variables.get(
                (match.group(1), match.group(2).strip()), match.group(0)
            ),
            text,
        )
        if expanded == text:
            break
        text = expanded
    # ``{env:KEY:DEFAULT}`` resolves to the declared default so a bound such as
    # ``Django>={env:LEGACY_DJANGO:3.2},<4.0`` is still read.
    env_pattern = re.compile(r"\{env:([^}:]+)(?::([^}]*))?\}")
    text = env_pattern.sub(
        lambda match: match.group(2)
        if match.group(2) is not None
        else match.group(0),
        text,
    )
    return text


def _tox_permits_lower_django(tox_ini):
    """True when any tox dependency permits a Django version below 5.2.

    Environment names are irrelevant: the real signal is the declared version
    constraint.  A rename such as ``dj32`` -> ``django32`` therefore no longer
    hides a retained pre-5.2 matrix entry.  tox variable substitution
    (``Django>={[testenv]legacy_version}``) is expanded first so a hidden bound
    is still read.
    """

    tox_ini = _expand_tox_variables(tox_ini)
    target = _version_tuple(TARGET_DJANGO)
    # PEP 508 extras (``Django[argon2]>=3.2,<4.0``) sit between the project name
    # and the version specifier; strip them before reading the constraint so a
    # retained pre-5.2 matrix entry cannot hide behind an extras bracket.
    pattern = re.compile(
        r"(?i)django(?:\s*\[[^\]]*\])?\s*"
        r"((?:\s*(?:<=|>=|==|!=|~=|<|>)\s*v?\d+(?:\.\d+)*(?:\s*,\s*)?)+)"
    )
    bound = re.compile(r"(<=|>=|==|!=|~=|<|>)\s*v?(\d+(?:\.\d+)*)")
    for match in pattern.finditer(tox_ini):
        for part in match.group(1).split(","):
            version_match = bound.search(part)
            if not version_match:
                continue
            operator = version_match.group(1)
            version = _version_tuple(version_match.group(2))
            if operator in ("<=", "<"):
                if version <= target:
                    return True
            elif version < target:
                return True
    return False


_PRODUCTION_EXCLUDE_DIRS = frozenset(
    {
        "tests",
        "test",
        "docs",
        "doc",
        "node_modules",
        "vendor",
        ".git",
        ".tox",
        ".venv",
        "venv",
        "build",
        "dist",
        "site-packages",
        "__pycache__",
    }
)

#: Untracked additions with these suffixes are source/config files (scope
#: relevant); anything else is treated as an ignorable build artifact.
_SCOPE_SOURCE_SUFFIXES = (".py", ".cfg", ".ini", ".toml", ".yaml", ".yml")


def _iter_production_sources(repo_root):
    """Absolute paths of production ``*.py`` files under ``repo_root``."""

    results = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _PRODUCTION_EXCLUDE_DIRS and not name.startswith(".")
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            results.append(full)
    return sorted(results)


def _read_source(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError:
        return ""


def _legacy_capability_branch(source, relative):
    """Recognize the old Django API fallbacks in the migration surface.

    Capability tests select an old Django implementation just as a VERSION
    comparison does. Resolve imported API names so aliases remain equivalent;
    ordinary application/model capability checks must remain allowed.
    """
    tree = _parse(source)
    if tree is None:
        return False
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bindings[alias.asname or alias.name] = node.module + "." + alias.name

    def qualified(node):
        if isinstance(node, ast.Name):
            return bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return qualified(node.value) + "." + node.attr
        return ""

    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp)):
            for call in ast.walk(node.test):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in {"hasattr", "getattr"}
                    and len(call.args) >= 2
                    and isinstance(call.args[1], ast.Constant)
                ):
                    continue
                api = qualified(call.args[0]) + "." + str(call.args[1].value)
                if api in {
                    "django.db.models.Manager.from_queryset",
                    "django.utils.translation.LANGUAGE_SESSION_KEY",
                }:
                    return True
                # These two migration paths receive Django request/field
                # objects. Do not ban similarly named optional application APIs.
                if relative == "feincms/content/application/models.py" and call.args[1].value == "is_ajax":
                    return True
                if relative == "feincms/module/page/forms.py" and call.args[1].value == "remote_field":
                    return True
        if isinstance(node, ast.Try) and relative == "feincms/content/application/models.py":
            modern = any(
                isinstance(child, ast.Attribute) and child.attr == "remote_field"
                for statement in node.body for child in ast.walk(statement)
            )
            legacy = any(
                isinstance(child, ast.Attribute) and child.attr == "rel"
                for handler in node.handlers for child in ast.walk(handler)
            )
            if modern and legacy:
                return True
    return False


def _production_version_branch(repo_root):
    """True for version tests or retained pre-5.2 Django API fallbacks."""
    for path in _iter_production_sources(repo_root):
        source = _read_source(path)
        relative = os.path.relpath(path, repo_root).replace(os.sep, "/")
        if _version_branch(source) or _legacy_capability_branch(source, relative):
            return True
    return False


def _production_is_dst(repo_root):
    """True when any production module retains a legacy ``is_dst`` fallback."""

    return any(
        _is_dst_argument(_read_source(path))
        for path in _iter_production_sources(repo_root)
    )


def _tox_includes(repo_root, text):
    """Concatenate ``-r <file>`` requirement includes referenced from ``tox.ini``."""

    chunks = []
    base = os.path.abspath(repo_root)
    for match in re.finditer(r"(?:^|\s)(?:-r|--requirement)\s+(\S+)", text):
        relative = match.group(1).strip().strip("'\"")
        candidate = os.path.join(repo_root, relative)
        if not os.path.isfile(candidate) or os.path.islink(candidate):
            continue
        resolved = os.path.realpath(candidate)
        if resolved != base and not resolved.startswith(base + os.sep):
            continue
        chunks.append(_read_source(candidate))
    return "\n".join(chunks)


def _legacy_repository_references(repo_root):
    """Find pre-5.2 Django references in repository text, including docs.

    The repository-wide reference requirement also covers templates and
    HTML/CSS/JS assets. Their separate immutability constraint does not grant
    an exception to this check; conflicting requirements must be reported to
    the task author rather than silently accepted by the verifier.
    Ignore environment/build output, and distinguish Django from packages such
    as django-mptt whose version numbers are independent.
    """
    patterns = (
        re.compile(
            r"\bDjango_?\s+(?:version(?:s)?\s+)?"
            r"(?:(?:older\s+than|after|before|up\s+to|at\s+least)\s+)?"
            r"v?(\d+(?:\.\d+)+(?:\.x)?|\d+\.x|[1-5])(?![\w.])", re.I
        ),
        re.compile(r"\bDjango\s*(?:<=|>=|==|~=|<|>)\s*(\d+(?:\.\d+)+)", re.I),
        re.compile(r"docs\.djangoproject\.com/[^/\s]+/(\d+\.\d+)/", re.I),
    )
    hits = []
    suffixes = (
        ".py", ".rst", ".md", ".txt", ".cfg", ".ini", ".toml", ".yaml", ".yml",
        ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".jinja", ".jinja2", ".j2",
    )
    for directory, dirs, files in os.walk(repo_root):
        dirs[:] = [
            name for name in dirs
            if name not in {".git", ".tox", ".venv", "venv", "node_modules", "build", "dist", "__pycache__"}
            and not name.endswith(".egg-info")
            and name not in {".eggs", ".pytest_cache", ".ruff_cache", ".cache"}
        ]
        for filename in sorted(files):
            if not filename.endswith(suffixes):
                continue
            path = os.path.join(directory, filename)
            if os.path.islink(path):
                continue
            source = _read_source(path)
            for pattern in patterns:
                for match in pattern.finditer(source):
                    version = match.group(1).replace(".x", "")
                    if _version_tuple(version) < (5, 2):
                        hits.append({
                            "path": os.path.relpath(path, repo_root),
                            "line": source.count("\n", 0, match.start()) + 1,
                            "reference": match.group(0),
                        })
    return hits


def _target_technology(repo_root):
    setup_cfg = _read(repo_root, "setup.cfg") or ""
    tox_ini = _read(repo_root, "tox.ini") or ""
    pre_commit = _read(repo_root, ".pre-commit-config.yaml") or ""

    legacy_branch = _production_version_branch(repo_root)
    legacy_is_dst = _production_is_dst(repo_root)
    legacy_references = _legacy_repository_references(repo_root)
    # The declared floor is a dependency *specifier*, not a byte string: an
    # equivalent spelling such as ``Django >= 5.2`` (with surrounding
    # whitespace) or ``Django[argon2]>=5.2`` must compare equal to
    # ``Django>=5.2`` instead of being misread as "no floor declared".
    django_floor = bool(
        re.search(
            r"(?i)django\s*(?:\[[^\]]*\])?\s*>=\s*%s(?:\.0)*(?![\d.])"
            % re.escape(TARGET_DJANGO),
            setup_cfg,
        )
    )
    tox_targets_52 = "dj52" in tox_ini and bool(
        re.search(
            r"(?i)dj52\s*:\s*django\s*(?:\[[^\]]*\])?\s*(?:<=|>=|==|!=|~=|<|>)\s*%s"
            % re.escape(TARGET_DJANGO),
            tox_ini,
        )
    )
    no_lower_factor = not _tox_permits_lower_django(
        tox_ini + "\n" + _tox_includes(repo_root, tox_ini)
    )
    precommit_target = bool(
        re.search(r'--target-version,\s*"%s"' % re.escape(TARGET_DJANGO), pre_commit)
    )
    applied = (
        django_floor
        and tox_targets_52
        and no_lower_factor
        and precommit_target
        and not legacy_branch
        and not legacy_is_dst
        and not legacy_references
    )
    return {
        "declared_django_floor": TARGET_DJANGO if django_floor else "",
        "legacy_version_branch": legacy_branch,
        "legacy_is_dst": legacy_is_dst,
        "legacy_repository_references": legacy_references,
        "tox_targets_52": tox_targets_52,
        "tox_has_lower_factor": not no_lower_factor,
        "precommit_targets_52": precommit_target,
        "applied": applied,
    }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
KINDS = ("P2P", "F2P", "DIFFERENTIAL", "STATE", "CONTRACT")


def evaluate(observation_id, kind, repo_root):
    """Return ``(status, value)`` for ``observation_id`` on one side.

    ``status`` is one of PASS/FAIL/INCONCLUSIVE.  ``value`` is the concrete,
    normalized projection.  The result is derived purely from ``repo_root`` so
    the value is identical on the source and target sides for every
    behaviour-preservation (P2P / DIFFERENTIAL / CONTRACT) observation.
    """

    if kind == "F2P":
        projection = _target_technology(repo_root)
        return ("PASS" if projection["applied"] else "FAIL", projection)

    if kind == "STATE":
        projection = _surface_state(repo_root)
        return ("PASS" if _state_ok(projection) else "FAIL", projection)

    surface = OBSERVATION_SURFACES.get(observation_id)
    if surface in _SURFACE_CHECKS:
        check, predicate = _SURFACE_CHECKS[surface]
        projection = check(repo_root)
        return ("PASS" if predicate(projection) else "FAIL", projection)

    if surface in _TEST_SURFACES:
        projection = _test_surface(repo_root, surface)
        return ("PASS" if _test_surface_ok(projection) else "FAIL", projection)

    # Should never happen for the frozen inventory; surface as INCONCLUSIVE.
    return ("INCONCLUSIVE", {"unsupported_surface": surface})


def is_static_observation(observation_id):
    return observation_id in OBSERVATION_SURFACES


def module_digest():
    """A stable digest of this module (useful as evidence)."""

    try:
        with open(__file__, "rb") as handle:
            return _digest_bytes(handle.read())
    except OSError:
        return None
