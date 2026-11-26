"""F2P target predicate checks for BATCH-F2P-0003.

The functions in this module implement one semantic predicate per owned
observation.  The exact same predicate is evaluated against the source tree
and against the target tree; no branch inspects "which side" it is running on.

The predicates assert that the frozen target technology (Django 5.2) is the
technology actually in effect on the executed/loaded path and that no runtime
compatibility branch for Django versions below 5.2 survives in the migration
scope.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:  # pragma: no cover - python 3.10 keeps this import optional
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


OBSERVATION_IDS = (
    "F2P-049",
    "F2P-051",
    "F2P-054",
    "F2P-055",
    "F2P-056",
    "F2P-057",
    "F2P-052",
    "F2P-050",
    "F2P-053",
    "F2P-058",
)

# Legacy technology residue that the Django 5.2 migration must remove from the
# production scope.  These are literal substrings, not human readable prose.
SCOPE_FILES = (
    "feincms/admin/filters.py",
    "feincms/extensions/datepublisher.py",
)
VERSION_BRANCH_TOKENS = ("django.VERSION", "import django")
DATE_PUBLISHER_FILE = "feincms/extensions/datepublisher.py"
IS_DST_TOKEN = "is_dst="

TARGET_DJANGO = (5, 2)
CHECK_TIMEOUT_SECONDS = 120
PYTEST_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# helpers


def _result(passed, observed=None, expected=None, evidence=None, error_type=""):
    return {
        "passed": bool(passed),
        "observed": observed,
        "expected": expected,
        "error_type": error_type,
        "evidence": list(evidence or []),
    }


def _read_text(repo_root, relative_path):
    path = os.path.join(repo_root, relative_path)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _load_canonical_detector():
    """Load the single canonical Django-version-branch detector.

    The semantic detector lives exactly once, in
    ``shared/integration/static_probes.py``; this probe reuses it (in addition
    to its own literal tokens) so every consumer of the "no retained version
    branch" property agrees on one implementation instead of drifting copies.
    """

    try:
        import importlib.util

        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "..",
            "integration",
            "static_probes.py",
        )
        spec = importlib.util.spec_from_file_location("batch_static_probes", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # pragma: no cover - never break the probe on import
        return None


_CANONICAL_PROBES = _load_canonical_detector()


def _canonical_version_branch(source):
    if _CANONICAL_PROBES is None:
        return False
    try:
        return bool(_CANONICAL_PROBES._version_branch(source))
    except Exception:  # pragma: no cover - never break the probe on a scan
        return False


def _scan(repo_root, relative_paths, tokens, semantic=False):
    """Return {relative_path: [tokens found]} for tokens present in files.

    When ``semantic`` is set the canonical version-branch detector is OR-ed in,
    so ``__version__`` / ``importlib.metadata.version("Django")`` spellings count
    as residue exactly like the literal ``django.VERSION``.
    """

    found = {}
    for relative_path in relative_paths:
        try:
            text = _read_text(repo_root, relative_path)
        except OSError:
            continue
        hits = [token for token in tokens if token in text]
        if semantic and "version-branch" not in hits and _canonical_version_branch(
            text
        ):
            hits.append("version-branch")
        if hits:
            found[relative_path] = hits
    return found


def _setup_django(repo_root):
    """Import the repository under test with its own Django settings."""
    tests_dir = os.path.join(repo_root, "tests")
    for entry in (tests_dir, repo_root):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testapp.settings")
    import django

    django.setup()
    return django


def _assets_root():
    override = os.environ.get("F2P0003_ASSETS_ROOT")
    if override:
        return Path(override)
    # shared/batches/BATCH-F2P-0003/checks.py -> <assets>
    return Path(__file__).resolve().parents[3]


def _integration_test_path():
    return (
        _assets_root()
        / "fixtures"
        / "batches"
        / "BATCH-F2P-0003"
        / "test_f2p0003.py"
    )


# ---------------------------------------------------------------------------
# predicates


def check_no_version_branch(repo_root):
    """R004/R018: no Django<5.2 compatibility residue in the scope."""
    found = _scan(repo_root, SCOPE_FILES, VERSION_BRANCH_TOKENS, semantic=True)
    datepublisher = _scan(repo_root, (DATE_PUBLISHER_FILE,), (IS_DST_TOKEN,))
    found.update(datepublisher)
    return _result(
        not found,
        observed=found,
        expected={},
        evidence=[f"legacy tokens found in {sorted(found)}"] if found else [],
    )


def _django_version_ok(repo_root):
    django = _setup_django(repo_root)
    return django, tuple(django.VERSION[:2]) == TARGET_DJANGO


def _filter_choices(filter_class, lookup_val, lookup_choices):
    class _Changelist:
        def get_query_string(self, *args, **kwargs):
            return ""

    instance = object.__new__(filter_class)
    instance.lookup_val = lookup_val
    instance.lookup_kwarg = "parent"
    instance.lookup_choices = list(lookup_choices)
    return list(instance.choices(_Changelist()))


def check_filter_single_selection(repo_root):
    """R004/R012: a single selection selects exactly the matching choice."""
    django, version_ok = _django_version_ok(repo_root)
    from feincms.admin.filters import ParentFieldListFilter

    choices = _filter_choices(
        ParentFieldListFilter, ["3"], [(3, "three"), (5, "five"), (7, "seven")]
    )
    selected = {str(choice["display"]): choice["selected"] for choice in choices}
    behavior_ok = (
        selected.get("three") is True
        and selected.get("five") is False
        and selected.get("seven") is False
    )
    residue = _scan(repo_root, SCOPE_FILES, VERSION_BRANCH_TOKENS, semantic=True)
    passed = version_ok and behavior_ok and not residue
    return _result(
        passed,
        observed={"django": django.VERSION, "selected": selected, "residue": residue},
        expected={"three": True, "five": False, "seven": False},
    )


def check_filter_multi_selection(repo_root):
    """R028: every selected value is reported selected; None selects All."""
    django, version_ok = _django_version_ok(repo_root)
    from feincms.admin.filters import CategoryFieldListFilter

    multi = _filter_choices(
        CategoryFieldListFilter, ["3", "5"], [(3, "three"), (5, "five"), (7, "seven")]
    )
    multi_selected = {str(c["display"]): c["selected"] for c in multi}
    empty = _filter_choices(
        CategoryFieldListFilter, None, [(3, "three"), (5, "five"), (7, "seven")]
    )
    all_selected = empty[0]["selected"]
    behavior_ok = (
        multi_selected.get("three") is True
        and multi_selected.get("five") is True
        and multi_selected.get("seven") is False
        and all_selected is True
    )
    residue = _scan(repo_root, SCOPE_FILES, VERSION_BRANCH_TOKENS, semantic=True)
    passed = version_ok and behavior_ok and not residue
    return _result(
        passed,
        observed={
            "django": django.VERSION,
            "multi_selected": multi_selected,
            "all_selected": all_selected,
            "residue": residue,
        },
        expected={
            "three": True,
            "five": True,
            "seven": False,
            "all_selected": True,
        },
    )


def check_granular_now_rounding(repo_root):
    """R027: granular_now rounds to five minutes without is_dst."""
    django, version_ok = _django_version_ok(repo_root)
    from feincms.extensions.datepublisher import granular_now

    utc = ZoneInfo("UTC")
    observed = granular_now(datetime(2024, 1, 15, 9, 12), default_tz=utc)
    expected = datetime(2024, 1, 15, 9, 10, tzinfo=utc)
    behavior_ok = observed == expected and observed.tzinfo is not None
    residue = _scan(repo_root, (DATE_PUBLISHER_FILE,), (IS_DST_TOKEN,))
    passed = version_ok and behavior_ok and not residue
    return _result(
        passed,
        observed={
            "django": django.VERSION,
            "value": observed.isoformat(),
        },
        expected={"value": expected.isoformat()},
    )


def check_granular_now_dst(repo_root):
    """R015: a DST-ambiguous local time does not raise and stays aware."""
    django, version_ok = _django_version_ok(repo_root)
    from feincms.extensions.datepublisher import granular_now

    zurich = ZoneInfo("Europe/Zurich")
    try:
        observed = granular_now(datetime(2024, 10, 27, 2, 7), default_tz=zurich)
        raised = None
    except Exception as exc:  # noqa: BLE001 - report the failure mode
        observed = None
        raised = type(exc).__name__
    behavior_ok = raised is None and getattr(observed, "tzinfo", None) is not None
    residue = _scan(
        repo_root,
        (DATE_PUBLISHER_FILE,),
        (IS_DST_TOKEN,) + VERSION_BRANCH_TOKENS,
        semantic=True,
    )
    passed = version_ok and behavior_ok and not residue
    return _result(
        passed,
        observed={
            "django": django.VERSION,
            "value": observed.isoformat() if observed else None,
            "raised": raised,
            "residue": residue,
        },
        expected={"aware": True, "raised": None},
    )


def check_scope_token_free(repo_root):
    """R018: production scope has zero legacy token occurrences."""
    found = _scan(repo_root, SCOPE_FILES, (IS_DST_TOKEN,) + VERSION_BRANCH_TOKENS, semantic=True)
    return _result(
        not found,
        observed=found,
        expected={},
        evidence=[f"legacy tokens found in {sorted(found)}"] if found else [],
    )


def check_response_processor_expiry(repo_root):
    """R029: Expires header derived from publication_end_date."""
    django, version_ok = _django_version_ok(repo_root)
    from django.http import HttpResponse
    from django.utils import timezone
    from django.utils.http import parse_http_date

    from feincms.extensions.datepublisher import datepublisher_response_processor

    class _Page:
        publication_end_date = None

    page = _Page()
    page.publication_end_date = timezone.now() + timedelta(seconds=3600)
    before = timezone.now()
    response = HttpResponse()
    datepublisher_response_processor(page, None, response)
    header = response.get("Expires")
    headered = parse_http_date(header) if header else None
    delta_ok = (
        headered is not None and abs(headered - before.timestamp() - 3600) <= 120
    )

    page.publication_end_date = None
    untouched = HttpResponse()
    datepublisher_response_processor(page, None, untouched)
    untouched_ok = untouched.get("Expires") is None

    residue = _scan(
        repo_root,
        (DATE_PUBLISHER_FILE,),
        VERSION_BRANCH_TOKENS + (IS_DST_TOKEN,),
        semantic=True,
    )
    passed = version_ok and delta_ok and untouched_ok and not residue
    return _result(
        passed,
        observed={
            "django": django.VERSION,
            "expires": header,
            "delta": None if headered is None else headered - before.timestamp(),
            "untouched_expires": untouched.get("Expires"),
            "residue": residue,
        },
        expected={"delta": 3600, "untouched_expires": None},
    )


def check_handler_404(repo_root):
    """R030: Handler honours FEINCMS_CMS_404_PAGE under Django 5.2 identity.

    The 404 behaviour is a preserved contract, so the discriminating part of
    this entry probe is the executed target technology identity: the Handler
    request path is exercised on the migrated tree with the Django<5.2
    compatibility branch removed from the migration scope.  The identical
    predicate is applied to both sides; on the source tree the retained
    version branch fails the identity check.
    """
    _, version_ok = _django_version_ok(repo_root)
    residue = _scan(repo_root, SCOPE_FILES, VERSION_BRANCH_TOKENS + (IS_DST_TOKEN,))
    test_path = _integration_test_path()
    env = dict(os.environ)
    env["DJANGO_SETTINGS_MODULE"] = "testapp.settings"
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(repo_root, "tests"), repo_root, env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    argv = [
        "timeout",
        str(PYTEST_TIMEOUT_SECONDS),
        sys.executable,
        "-m",
        "pytest",
        "{}::TestHandler404::test_handler_404".format(test_path),
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    try:
        completed = subprocess.run(
            argv,
            cwd=repo_root,
            env=env,
            timeout=PYTEST_TIMEOUT_SECONDS + 30,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return _result(
            False,
            observed={"timed_out": True},
            evidence=[str(exc)],
            error_type="INFRA_ERROR",
        )
    except OSError as exc:
        return _result(
            False,
            observed={"spawn_error": str(exc)},
            evidence=[type(exc).__name__],
            error_type="INFRA_ERROR",
        )
    passed = version_ok and completed.returncode == 0 and not residue
    return _result(
        passed,
        observed={
            "returncode": completed.returncode,
            "tail": completed.stdout[-400:],
            "residue": residue,
        },
        expected={"returncode": 0, "residue": {}},
        evidence=[line for line in completed.stderr.splitlines() if line][-3:],
    )


CHECKS = {
    "F2P-049": check_filter_single_selection,
    "F2P-050": check_filter_single_selection,
    "F2P-051": check_granular_now_rounding,
    "F2P-052": check_handler_404,
    "F2P-053": check_granular_now_rounding,
    "F2P-054": check_granular_now_dst,
    "F2P-055": check_filter_multi_selection,
    "F2P-056": check_scope_token_free,
    "F2P-057": check_scope_token_free,
    "F2P-058": check_response_processor_expiry,
}


def run_check(observation_id, repo_root):
    function = CHECKS.get(observation_id)
    if function is None:
        return _result(
            False,
            observed={"unknown_observation": observation_id},
            error_type="INFRA_ERROR",
        )
    try:
        return function(repo_root)
    except Exception as exc:  # noqa: BLE001 - setup failures are not F2P failures
        return _result(
            False,
            observed={"exception": type(exc).__name__},
            evidence=[f"{type(exc).__name__}: {exc}"],
            error_type="INFRA_ERROR",
        )


def run_all(repo_root):
    return {oid: run_check(oid, repo_root) for oid in OBSERVATION_IDS}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    repo_root = argv[1] if len(argv) > 1 else os.environ.get(
        "MIGRATION_REPO", "/workspace/repo"
    )
    observation_id = argv[0] if argv else None
    if observation_id:
        print(json.dumps(run_check(observation_id, repo_root), sort_keys=True))
    else:
        print(json.dumps(run_all(repo_root), sort_keys=True))


if __name__ == "__main__":
    main()
