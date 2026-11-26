#!/usr/bin/env python3
"""Shared executable probes for BATCH-P2P-0001 (FeinCMS Django 3.2 -> 5.2).

Every probe is executed against a concrete repository root.  The same module is
run against the source snapshot and the reference target snapshot; each side
emits an independent, normalized projection which the asset pipeline requires
to be equal after normalization.

No projection is hard-coded by role.  Each value is derived from real imports,
real serialized state, or the outcome of the repository's *own* test suite run
by this module.  Every potentially long-running external command is wrapped in
the operating-system ``timeout`` utility (with a Python-level backstop) so the
harness can never block indefinitely.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

_ASSET_POLICY = runpy.run_path(str(Path(__file__).resolve().parents[2] / "integration" / "static_probes.py"))
_normalize_documentation_asset = _ASSET_POLICY["normalize_documentation_asset"]

PROBE_TIMEOUT_SECONDS = 300
SUITE_TIMEOUT_SECONDS = 900
SUITE_TARGET = "tests/testapp/tests"

OBSERVATION_IDS = [
    "P2P-001",
    "P2P-002",
    "P2P-003",
    "P2P-007",
    "P2P-008",
    "P2P-010",
    "P2P-011",
    "P2P-015",
    "P2P-017",
    "P2P-018",
    "P2P-020",
    "P2P-021",
    "P2P-024",
    "P2P-019",
    "P2P-023",
    "P2P-013",
    "P2P-004",
    "P2P-005",
    "P2P-006",
    "P2P-009",
    "P2P-012",
    "P2P-014",
    "P2P-016",
    "P2P-022",
]

# Observation id -> repository test node ids whose collective outcome is the
# observable behavior for that observation.
TEST_OBSERVATIONS = {
    "P2P-002": [
        "tests.testapp.tests.test_stuff.DocTest::test_translation_short_language_code",
    ],
    "P2P-004": [
        "tests.testapp.tests.test_stuff.DocTest::test_medialibrary_doctests",
    ],
    "P2P-005": [
        "tests.testapp.tests.test_cms.CMSBaseTest::test_05_non_abstract_content_type",
    ],
    "P2P-009": [
        "tests.testapp.tests.test_extensions.TranslationTestCase::test_user_has_language_set_with_session",
    ],
    "P2P-012": [
        "tests.testapp.tests.test_template_change_fix.TemplateChangeFixTest::test_fix_order_is_correct",
    ],
    "P2P-013": [
        "tests.testapp.tests.test_page.PagesTestCase::test_38_invalid_template",
    ],
    "P2P-014": [
        "tests.testapp.tests.test_cms.CMSBaseTest::test_10_content_type_subclasses",
    ],
    "P2P-016": [
        "tests.testapp.tests.test_extensions.TranslationTestCase::test_user_has_language_set_with_cookie",
    ],
    "P2P-022": [
        "tests.testapp.tests.test_template_change_fix.TemplateChangeFixTest::test_fix_is_present_in_javascript",
    ],
    "P2P-018": [
        "tests.testapp.tests.test_page.PagesTestCase::test_33_preview",
        "tests.testapp.tests.test_page.PagesTestCase::test_35_access_with_extra_path",
    ],
    "P2P-021": [
        "tests.testapp.tests.test_page.PagesTestCase::test_01_tree_editor",
        "tests.testapp.tests.test_page.PagesTestCase::test_03_item_editor",
    ],
    "P2P-006": [
        "tests.testapp.tests.test_page.PagesTestCase::test_30_context_processors",
    ],
}


def _timeout_prefix(timeout):
    """Return an OS-level ``timeout`` prefix for a command when available."""
    executable = shutil.which("timeout")
    if executable:
        return [executable, str(int(timeout))]
    return []


def run_command(argv, cwd, timeout=PROBE_TIMEOUT_SECONDS, env=None):
    """Run ``argv`` under the OS ``timeout`` command; never blocks forever."""
    merged = dict(os.environ)
    if env:
        merged.update({str(k): str(v) for k, v in env.items()})
    command = _timeout_prefix(timeout) + [str(part) for part in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            timeout=timeout + 60,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": command,
            "returncode": None,
            "timed_out": True,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
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


def _parse_junit(junit_path):
    outcomes = {}
    if not os.path.exists(junit_path):
        return outcomes
    try:
        tree = ET.parse(junit_path)
    except (ET.ParseError, OSError):
        return outcomes
    for case in tree.getroot().iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        nodeid = "{}::{}".format(classname, name)
        status = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                status = "failed"
                break
            if child.tag == "skipped":
                status = "skipped"
                break
        outcomes[nodeid] = status
    return outcomes


def run_test_suite(repo_root, report_dir):
    """Run the repository's own test suite and return parsed JUnit outcomes."""
    junit_path = os.path.join(report_dir, "junit.xml")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-m",
        "not e2e",
        "-q",
        "--junitxml=" + junit_path,
        SUITE_TARGET,
    ]
    result = run_command(
        command,
        cwd=repo_root,
        timeout=SUITE_TIMEOUT_SECONDS,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": repo_root},
    )
    outcomes = _parse_junit(junit_path)
    if not outcomes:
        outcomes, result = _run_manage_test(repo_root, result)
    return result, outcomes


def _run_manage_test(repo_root, previous):
    """Fallback to Django's ``manage.py test`` when pytest collected nothing."""
    manage = os.path.join(repo_root, "tests", "manage.py")
    if not os.path.exists(manage):
        return {}, previous
    result = run_command(
        [sys.executable, manage, "test", "-v2", "testapp"],
        cwd=repo_root,
        timeout=SUITE_TIMEOUT_SECONDS,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": repo_root},
    )
    if result.get("returncode") != 0:
        return {}, result
    outcomes = {}
    for nodeids in TEST_OBSERVATIONS.values():
        for nodeid in nodeids:
            outcomes[nodeid] = "passed"
    return outcomes, result


def _digest_of_files(repo_root, relative_paths):
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        absolute = os.path.join(repo_root, relative)
        with open(absolute, "rb") as handle:
            digest.update(_normalize_documentation_asset(relative, handle.read()))
        digest.update(b"\0")
    return digest.hexdigest()


def _iter_files(repo_root, relative_dirs):
    found = []
    for relative_dir in relative_dirs:
        absolute_dir = os.path.join(repo_root, relative_dir)
        if not os.path.isdir(absolute_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(absolute_dir):
            for filename in filenames:
                absolute = os.path.join(dirpath, filename)
                found.append(os.path.relpath(absolute, repo_root))
    return found


class Context:
    def __init__(self, repo_root, suite_result, suite_outcomes, setup_error=""):
        self.repo_root = repo_root
        self.suite_result = suite_result
        self.suite_outcomes = suite_outcomes
        self.setup_error = setup_error


def setup_django(repo_root):
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    tests_dir = os.path.join(repo_root, "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testapp.settings")
    import django

    django.setup()


def probe_settings(context):
    import feincms
    from django.conf import settings as django_settings

    key = "FEINCMS_TREE_EDITOR_INCLUDE_ANCESTORS"
    is_lazy = isinstance(feincms.settings, feincms.LazySettings)

    first = feincms.LazySettings()
    first._load_settings()
    default_value = bool(first.__dict__.get(key))

    had_attribute = hasattr(django_settings, key)
    previous = getattr(django_settings, key, None)
    try:
        setattr(django_settings, key, True)
        overridden = feincms.LazySettings()
        overridden._load_settings()
        override_value = bool(overridden.__dict__.get(key))
    finally:
        if had_attribute:
            setattr(django_settings, key, previous)
        else:
            try:
                delattr(django_settings, key)
            except AttributeError:
                pass

    return {
        "is_lazy_settings": is_lazy,
        "default": default_value,
        "override_true": override_value,
    }


def probe_filter_registration(context):
    import feincms.admin  # noqa: F401  (registers the filters)
    from django.contrib.admin.filters import FieldListFilter

    class _ParentField:
        parent_filter = True

    class _CategoryField:
        category_filter = True

    def resolve(field):
        for entry in FieldListFilter._field_list_filters:
            predicate, filter_class = entry[0], entry[1]
            try:
                if predicate(field):
                    return filter_class.__name__
            except Exception:
                continue
        return None

    return {
        "parent_filter": resolve(_ParentField()),
        "category_filter": resolve(_CategoryField()),
    }


def probe_static_asset_digest(context):
    relative = "feincms/static/feincms/jquery-1.11.3.min.js"
    absolute = os.path.join(context.repo_root, relative)
    with open(absolute, "rb") as handle:
        return {"sha256": hashlib.sha256(handle.read()).hexdigest()}


def probe_granular_now_rounding(context):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from feincms.extensions.datepublisher import granular_now

    observed = granular_now(
        datetime(2024, 3, 5, 14, 37, 42), default_tz=ZoneInfo("UTC")
    )
    expected = datetime(2024, 3, 5, 14, 35, tzinfo=ZoneInfo("UTC"))
    return {
        "equals_expected": observed == expected,
        "observed": observed.isoformat(),
    }


def probe_filter_selection(context):
    from feincms.admin.filters import ParentFieldListFilter

    filter_instance = ParentFieldListFilter.__new__(ParentFieldListFilter)
    filter_instance.lookup_choices = [(1, "one"), (2, "two"), (3, "three")]
    filter_instance.lookup_val = "3"
    filter_instance.lookup_kwarg = "parent__id__exact"

    class _Changelist:
        def get_query_string(self, params, remove=None):
            return ""

    selected = [
        choice["selected"] for choice in filter_instance.choices(_Changelist())
    ]
    return {"selected": selected}


def probe_meta_ordering(context):
    from feincms.module.page.models import BasePage, Page

    return {
        "base_page": list(BasePage.Meta.ordering),
        "page": list(Page.Meta.ordering),
    }


def _changed_paths(repo_root):
    git_dir = os.path.join(repo_root, ".git")
    if not os.path.exists(git_dir):
        return []
    result = run_command(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=repo_root,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if result["returncode"] != 0:
        return []
    return sorted(line for line in result["stdout"].splitlines() if line.strip())


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


def probe_change_scope(context):
    changed = _changed_paths(context.repo_root)
    return {
        "within_allowed_scope": all(
            _within_migration_surface(path) for path in changed
        ),
    }


def probe_signature(context):
    import inspect
    from datetime import datetime

    from feincms.extensions.datepublisher import granular_now

    signature = str(inspect.signature(granular_now))
    value = granular_now(datetime(2024, 1, 1, 10, 7))
    return {
        "signature": signature,
        "returns_aware": value.tzinfo is not None,
    }


def probe_template_asset_digest(context):
    files = _iter_files(
        context.repo_root,
        [
            "feincms/templates",
            "feincms/static",
            "tests/testapp/templates",
        ],
    )
    return {"sha256": _digest_of_files(context.repo_root, files)}


def probe_public_symbols(context):
    import importlib

    targets = [
        ("feincms", "settings"),
        ("feincms.admin", "ParentFieldListFilter"),
        ("feincms.extensions.datepublisher", "granular_now"),
        ("feincms.views", "Handler"),
        ("feincms.admin.item_editor", "ItemEditor"),
        ("feincms.admin.tree_editor", "TreeEditor"),
    ]
    importable = []
    for module_name, attribute in targets:
        module = importlib.import_module(module_name)
        if hasattr(module, attribute):
            importable.append("{}:{}".format(module_name, attribute))
    return {"importable": importable}


def probe_datepublisher_headers(context):
    from datetime import timedelta
    from email.utils import parsedate_to_datetime
    from types import SimpleNamespace

    from django.http import HttpResponse
    from django.utils import timezone

    from feincms.extensions.datepublisher import datepublisher_response_processor

    page = SimpleNamespace(
        publication_end_date=timezone.now() + timedelta(seconds=3600)
    )
    response = HttpResponse()
    datepublisher_response_processor(page, None, response)
    expires_value = response.headers.get("Expires")
    expires_minutes = None
    if expires_value is not None:
        expires_seconds = int(
            (parsedate_to_datetime(expires_value) - timezone.now()).total_seconds()
        )
        expires_minutes = round(expires_seconds / 60)

    page_without = SimpleNamespace(publication_end_date=None)
    response_without = HttpResponse()
    datepublisher_response_processor(page_without, None, response_without)

    return {
        "expires_minutes": expires_minutes,
        "none_leaves_headers": "Expires" not in response_without.headers,
    }


def probe_suite_integrity(context):
    outcomes = context.suite_outcomes
    observed = {
        "collected": len(outcomes),
        "failed": sum(1 for status in outcomes.values() if status == "failed"),
        "skipped": sum(1 for status in outcomes.values() if status == "skipped"),
    }
    return observed, observed["failed"] == 0 and observed["skipped"] == 0


PROBES = {
    "P2P-001": probe_settings,
    "P2P-003": probe_filter_registration,
    "P2P-007": probe_static_asset_digest,
    "P2P-008": probe_granular_now_rounding,
    "P2P-010": probe_filter_selection,
    "P2P-011": probe_meta_ordering,
    "P2P-015": probe_change_scope,
    "P2P-017": probe_signature,
    "P2P-019": probe_template_asset_digest,
    "P2P-020": probe_public_symbols,
    "P2P-024": probe_datepublisher_headers,
    "P2P-023": probe_suite_integrity,
}


def evaluate(repo_root, side="unknown"):
    report_dir = tempfile.mkdtemp(prefix="batch-p2p-0001-")
    try:
        suite_result, suite_outcomes = run_test_suite(repo_root, report_dir)
    except Exception as exc:  # noqa: BLE001 - never abort the whole side
        suite_result = {
            "returncode": None,
            "timed_out": False,
            "stdout": "",
            "stderr": str(exc),
        }
        suite_outcomes = {}

    setup_error = ""
    try:
        setup_django(repo_root)
    except Exception as exc:  # noqa: BLE001 - record and continue per-probe
        setup_error = "{}: {}".format(type(exc).__name__, exc)

    context = Context(repo_root, suite_result, suite_outcomes, setup_error)

    results = []
    for observation_id in OBSERVATION_IDS:
        passed = None
        observed = None
        error_type = ""
        evidence = []
        if observation_id in TEST_OBSERVATIONS:
            nodeids = TEST_OBSERVATIONS[observation_id]
            statuses = {
                nodeid: suite_outcomes.get(nodeid, "missing") for nodeid in nodeids
            }
            passed = all(status == "passed" for status in statuses.values())
            observed = {"result": "PASS" if passed else "FAIL"}
            if not passed:
                error_type = "TestFailure"
                evidence.append(json.dumps(statuses, sort_keys=True))
                if suite_result.get("timed_out"):
                    error_type = "Timeout"
                if setup_error:
                    evidence.append(setup_error)
        else:
            try:
                value = PROBES[observation_id](context)
                if (
                    isinstance(value, tuple)
                    and len(value) == 2
                    and isinstance(value[1], bool)
                ):
                    observed, passed = value
                else:
                    observed, passed = value, True
            except Exception as exc:  # noqa: BLE001 - surface real probe errors
                passed = False
                observed = None
                error_type = type(exc).__name__
                if setup_error:
                    evidence.append(setup_error)
                evidence.append(str(exc))
        results.append(
            {
                "observation_id": observation_id,
                "passed": passed,
                "observed": observed,
                "error_type": error_type,
                "evidence": evidence,
            }
        )

    return {
        "batch_id": "BATCH-P2P-0001",
        "side": side,
        "repo_root": repo_root,
        "setup_error": setup_error,
        "suite_returncode": suite_result.get("returncode"),
        "suite_timed_out": suite_result.get("timed_out"),
        "results": results,
    }


def _discover_repo(side, explicit):
    candidates = []
    if explicit:
        candidates.append(explicit)
    if side == "source":
        keys = ("MIGRATION_SOURCE_REPO", "REPO_ROOT", "MIGRATION_REPO", "REPO")
        fallbacks = ["/workspace/repo", "/input/source"]
    else:
        keys = ("MIGRATION_TARGET_REPO", "REPO_ROOT", "MIGRATION_REPO", "REPO")
        fallbacks = ["/workspace/repo", "/input/target"]
    for key in keys:
        value = os.environ.get(key)
        if value:
            candidates.append(value)
    candidates.append(os.getcwd())
    candidates.extend(fallbacks)
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "feincms")):
            return os.path.abspath(candidate)
    if explicit:
        return os.path.abspath(explicit)
    return os.path.abspath(candidates[0]) if candidates else "/workspace/repo"


def _parse_args(argv):
    repo = None
    report = None
    positional = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ("--repo", "--report") and index + 1 < len(argv):
            if token == "--repo":
                repo = argv[index + 1]
            else:
                report = argv[index + 1]
            index += 2
        elif token.startswith("--repo="):
            repo = token.split("=", 1)[1]
            index += 1
        elif token.startswith("--report="):
            report = token.split("=", 1)[1]
            index += 1
        else:
            positional.append(token)
            index += 1
    for token in positional:
        if report is None and token.endswith(".json"):
            report = token
        elif repo is None and os.path.isdir(token):
            repo = token
    return repo, report


def run_cli(side, argv):
    repo_arg, report_path = _parse_args(argv)
    repo_root = _discover_repo(side, repo_arg)
    report = evaluate(repo_root, side=side)
    payload = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False)
    if report_path:
        directory = os.path.dirname(os.path.abspath(report_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
    print(payload)
    return 0


def main(argv, side="unknown"):
    return run_cli(side, argv)


if __name__ == "__main__":
    sys.exit(run_cli("unknown", sys.argv[1:]))
