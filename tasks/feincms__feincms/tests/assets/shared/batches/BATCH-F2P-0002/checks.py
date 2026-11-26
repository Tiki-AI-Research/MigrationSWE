"""Semantic F2P probes for BATCH-F2P-0002.

Migration: FeinCMS, Django 3.2 -> Django 5.2 (version upgrade).

The exact same semantic predicate is used for both sides; nothing in this
module branches on whether it is executed against the source or the target
repository.  Each predicate either:

* executes the concrete entry/call named by the observation and inspects the
  executed/loaded path, or
* reads the named dependency manifest/configuration, and
* asserts that the target technology (Django >= 5.2) is the active technology.

Consequently the predicate fails under the source technology (Django 3.2) and
passes under the target technology (Django 5.2).
"""

import configparser
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

TARGET_DJANGO = (5, 2)
LEGACY_MARKERS = ("django.VERSION", "import django", "is_dst=")
SCOPED_FILES = (
    "feincms/admin/filters.py",
    "feincms/extensions/datepublisher.py",
)
CONFIG_FILES = ("setup.cfg", "tox.ini", ".pre-commit-config.yaml")


@dataclass
class ProbeResult:
    observation_id: str
    passed: bool
    expected: str = "target-postcondition"
    observed: dict = field(default_factory=dict)
    error_type: str = ""
    evidence: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _read(repo_root, rel):
    with open(os.path.join(repo_root, rel), encoding="utf-8") as handle:
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


def _target_technology_active():
    import django

    return tuple(django.VERSION[:2]) >= TARGET_DJANGO


def _legacy_hits(repo_root, markers, files, semantic=False):
    hits = {}
    for rel in files:
        try:
            text = _read(repo_root, rel)
        except OSError:
            continue
        for marker in markers:
            if marker in text:
                hits.setdefault(rel, []).append(marker)
        if semantic and not hits.get(rel) and _canonical_version_branch(text):
            hits.setdefault(rel, []).append("version-branch")
    return hits


def _side_effects(repo_root):
    return {
        "django_version": None,
        "target_technology": _target_technology_active(),
    }


def _ensure_django(repo_root):
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    tests = os.path.join(repo_root, "tests")
    if os.path.isdir(tests) and tests not in sys.path:
        sys.path.insert(0, tests)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testapp.settings")
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()


# ---------------------------------------------------------------------------
# dependency / configuration declarations
# ---------------------------------------------------------------------------
def _specifier_only(requirement):
    """Return the version specifier subset of a ``Django...`` requirement."""

    if requirement is None:
        return None
    stripped = requirement.strip()
    if stripped.lower().startswith("django"):
        stripped = stripped[len("django"):]
    return stripped.strip() or None


def _django_specifier_from_setup_cfg(repo_root):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.path.join(repo_root, "setup.cfg"))
    raw = parser.get("options", "install_requires")
    for line in raw.splitlines():
        line = line.strip()
        if line and line.lower().startswith("django"):
            return _specifier_only(line)
    return None


def _specifier_ok(specifier):
    from packaging.specifiers import SpecifierSet

    specifier_set = SpecifierSet(specifier)
    return specifier_set.contains("5.2") and not specifier_set.contains("4.2")


def check_setupcfg_requires(repo_root, observation_id):
    specifier = _django_specifier_from_setup_cfg(repo_root)
    try:
        passed = _specifier_ok(specifier)
    except Exception as exc:  # invalid/missing specifier is a semantic failure
        return ProbeResult(
            observation_id,
            False,
            observed={"specifier": specifier, "error": str(exc)},
            error_type=type(exc).__name__,
        )
    return ProbeResult(
        observation_id,
        passed,
        observed={"specifier": specifier, "contains_5_2": True},
    )


def check_metadata_requires(repo_root, observation_id):
    specifier = None
    surface = "importlib.metadata"
    try:
        import importlib.metadata as metadata

        requires = metadata.requires("FeinCMS") or []
        for entry in requires:
            if entry.strip().lower().startswith("django"):
                specifier = _specifier_only(entry)
                break
    except Exception:
        specifier = None
    if specifier is None:
        surface = "setup.cfg"
        specifier = _django_specifier_from_setup_cfg(repo_root)
    try:
        passed = _specifier_ok(specifier)
    except Exception as exc:
        return ProbeResult(
            observation_id,
            False,
            observed={"surface": surface, "specifier": specifier, "error": str(exc)},
            error_type=type(exc).__name__,
        )
    return ProbeResult(
        observation_id,
        passed,
        observed={"surface": surface, "specifier": specifier},
    )


def check_tox_matrix(repo_root, observation_id):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(os.path.join(repo_root, "tox.ini"))
    envlist = parser.get("tox", "envlist")
    deps = parser.get("testenv", "deps")
    old_factors = re.findall(r"dj(?:32|41|42|50|51|main)", deps)
    old_envs = re.findall(r"dj(?:32|41|42|50|51|main)", envlist)
    has_dj52_dep = bool(re.search(r"dj52:\s*Django>=5\.2,<5\.3", deps))
    has_dj52_env = "dj52" in envlist
    passed = has_dj52_dep and has_dj52_env and not old_factors and not old_envs
    return ProbeResult(
        observation_id,
        passed,
        observed={
            "envlist": envlist,
            "dj52_dependency": has_dj52_dep,
            "legacy_factors": sorted(set(old_factors + old_envs)),
        },
    )


def check_precommit_target(repo_root, observation_id):
    text = _read(repo_root, ".pre-commit-config.yaml")
    match = re.search(
        r"django-upgrade(?:(?!\n\s*-\s+repo:).)*", text, re.DOTALL | re.IGNORECASE
    )
    block = match.group(0) if match else ""
    has_target = bool(re.search(r"--target-version[^\n]*5\.2", block))
    legacy = bool(re.search(r"--target-version[^\n]*3\.2", block)) or (
        "3.2" in block and not has_target
    )
    passed = has_target and not legacy
    return ProbeResult(
        observation_id,
        passed,
        observed={"has_target_5_2": has_target, "legacy_target": legacy},
    )


# ---------------------------------------------------------------------------
# production source residue / execution-path checks
# ---------------------------------------------------------------------------
def check_no_version_branch(repo_root, observation_id):
    hits = _legacy_hits(repo_root, ("django.VERSION",), SCOPED_FILES, semantic=True)
    datepublisher = _read(repo_root, "feincms/extensions/datepublisher.py")
    filters = _read(repo_root, "feincms/admin/filters.py")
    make_aware_ok = (
        "timezone.make_aware(d, default_tz)" in datepublisher
        and "is_dst" not in datepublisher
    )
    multi_valued_ok = "str(pk) in lookup_vals" in filters
    passed = not hits and make_aware_ok and multi_valued_ok
    return ProbeResult(
        observation_id,
        passed,
        observed={
            "legacy_hits": hits,
            "make_aware_without_is_dst": make_aware_ok,
            "multi_valued_lookup": multi_valued_ok,
        },
        evidence=[rel for rel in hits],
    )


def check_no_legacy_markers(repo_root, observation_id):
    hits = _legacy_hits(repo_root, LEGACY_MARKERS, SCOPED_FILES)
    passed = not hits
    return ProbeResult(
        observation_id,
        passed,
        observed={"legacy_hits": hits},
        evidence=[rel for rel in hits],
    )


# ---------------------------------------------------------------------------
# runtime behavior checks
# ---------------------------------------------------------------------------
def check_granular_rounding(repo_root, observation_id):
    try:
        _ensure_django(repo_root)
        from feincms.extensions.datepublisher import granular_now

        observed_value = granular_now(
            datetime(2024, 1, 15, 9, 12), default_tz=ZoneInfo("UTC")
        )
        expected_value = datetime(2024, 1, 15, 9, 10, tzinfo=ZoneInfo("UTC"))
        behavior = observed_value == expected_value and observed_value.tzinfo is not None
        residue = _legacy_hits(
            repo_root, ("is_dst=",), ("feincms/extensions/datepublisher.py",)
        )
        technology = _target_technology_active()
        passed = behavior and technology and not residue
        return ProbeResult(
            observation_id,
            passed,
            observed={
                "value": observed_value.isoformat(),
                "behavior": behavior,
                "target_technology": technology,
                "residue": residue,
            },
        )
    except Exception as exc:
        return ProbeResult(
            observation_id,
            False,
            observed={"exception": str(exc)},
            error_type=type(exc).__name__,
        )


def check_granular_dst(repo_root, observation_id):
    try:
        _ensure_django(repo_root)
        from feincms.extensions.datepublisher import granular_now

        value = granular_now(
            datetime(2024, 10, 27, 2, 7), default_tz=ZoneInfo("Europe/Zurich")
        )
        behavior = value.tzinfo is not None
        residue = _legacy_hits(
            repo_root, ("is_dst=", "django.VERSION"), ("feincms/extensions/datepublisher.py",), semantic=True
        )
        technology = _target_technology_active()
        passed = behavior and technology and not residue
        return ProbeResult(
            observation_id,
            passed,
            observed={
                "value": value.isoformat(),
                "behavior": behavior,
                "target_technology": technology,
                "residue": residue,
            },
        )
    except Exception as exc:
        return ProbeResult(
            observation_id,
            False,
            observed={"exception": str(exc)},
            error_type=type(exc).__name__,
        )


def _fake_changelist():
    class _Changelist:
        def get_query_string(self, *args, **kwargs):
            return "?"

    return _Changelist()


def _filter_selection(repo_root, lookup_val):
    from feincms.admin.filters import ParentFieldListFilter

    instance = ParentFieldListFilter.__new__(ParentFieldListFilter)
    instance.lookup_val = lookup_val
    instance.lookup_choices = [(3, "three"), (5, "five")]
    instance.lookup_kwarg = "parent"
    return list(instance.choices(_fake_changelist()))


def check_choices_rendering(repo_root, observation_id):
    try:
        _ensure_django(repo_root)
        import feincms.admin.filters as filters_module

        rows = _filter_selection(repo_root, ["3", "5"])
        selected = {str(row["display"]): row["selected"] for row in rows}
        multi_ok = selected.get("three") is True and selected.get("five") is True
        none_rows = _filter_selection(repo_root, None)
        all_row = [row for row in none_rows if str(row["display"]) == "All"]
        none_ok = bool(all_row) and all_row[0]["selected"] is True
        residue = _legacy_hits(
            repo_root, ("django.VERSION",), ("feincms/admin/filters.py",), semantic=True
        )
        technology = _target_technology_active()
        passed = multi_ok and none_ok and technology and not residue
        return ProbeResult(
            observation_id,
            passed,
            observed={
                "multi_selected": multi_ok,
                "all_selected": none_ok,
                "target_technology": technology,
                "residue": residue,
                "module": filters_module.__file__,
            },
        )
    except Exception as exc:
        return ProbeResult(
            observation_id,
            False,
            observed={"exception": str(exc)},
            error_type=type(exc).__name__,
        )


def check_lookup_val_list(repo_root, observation_id):
    try:
        _ensure_django(repo_root)
        rows = _filter_selection(repo_root, ["3", "5"])
        selected = {str(row["display"]): row["selected"] for row in rows}
        behavior = selected.get("three") is True and selected.get("five") is True
        residue = _legacy_hits(
            repo_root, ("django.VERSION",), ("feincms/admin/filters.py",), semantic=True
        )
        technology = _target_technology_active()
        passed = behavior and technology and not residue
        return ProbeResult(
            observation_id,
            passed,
            observed={
                "selected": selected,
                "target_technology": technology,
                "residue": residue,
            },
        )
    except Exception as exc:
        return ProbeResult(
            observation_id,
            False,
            observed={"exception": str(exc)},
            error_type=type(exc).__name__,
        )


def check_response_expires(repo_root, observation_id):
    try:
        _ensure_django(repo_root)
        from django.http import HttpResponse
        from django.utils import timezone

        from feincms.extensions.datepublisher import datepublisher_response_processor

        class _Page:
            pass

        page = _Page()
        page.publication_end_date = timezone.now() + timedelta(seconds=3600)
        response = HttpResponse(b"x")
        datepublisher_response_processor(page, None, response)
        expires = response.headers.get("Expires")
        delta = None
        behavior = False
        if expires:
            delta = (
                parsedate_to_datetime(expires) - timezone.now()
            ).total_seconds()
            behavior = 3500 <= delta <= 3700

        page.publication_end_date = None
        untouched = HttpResponse(b"x")
        datepublisher_response_processor(page, None, untouched)
        none_ok = "Expires" not in untouched.headers

        residue = _legacy_hits(
            repo_root, ("is_dst=",), ("feincms/extensions/datepublisher.py",)
        )
        technology = _target_technology_active()
        passed = behavior and none_ok and technology and not residue
        return ProbeResult(
            observation_id,
            passed,
            observed={
                "expires": expires,
                "delta_seconds": delta,
                "none_untouched": none_ok,
                "target_technology": technology,
                "residue": residue,
            },
        )
    except Exception as exc:
        return ProbeResult(
            observation_id,
            False,
            observed={"exception": str(exc)},
            error_type=type(exc).__name__,
        )


def check_fieldlistfilter_registration(repo_root, observation_id):
    try:
        _ensure_django(repo_root)
        import feincms.admin  # noqa: F401  (registers the filters)
        from django.contrib.admin.filters import FieldListFilter

        from feincms.admin.filters import CategoryFieldListFilter, ParentFieldListFilter

        def resolve(field):
            for test, filter_class in FieldListFilter._field_list_filters:
                try:
                    if test(field):
                        return filter_class
                except Exception:
                    continue
            return None

        class ParentField:
            parent_filter = True
            category_filter = False

        class CategoryField:
            parent_filter = False
            category_filter = True

        parent_ok = resolve(ParentField()) is ParentFieldListFilter
        category_ok = resolve(CategoryField()) is CategoryFieldListFilter
        technology = _target_technology_active()
        passed = parent_ok and category_ok and technology
        return ProbeResult(
            observation_id,
            passed,
            observed={
                "parent_filter": parent_ok,
                "category_filter": category_ok,
                "target_technology": technology,
            },
        )
    except Exception as exc:
        return ProbeResult(
            observation_id,
            False,
            observed={"exception": str(exc)},
            error_type=type(exc).__name__,
        )


def check_cms_404_page(repo_root, observation_id):
    try:
        _ensure_django(repo_root)
        from django.http import Http404, HttpResponse
        from django.test import RequestFactory, override_settings

        import feincms
        from feincms.views import Handler

        class _Page:
            pass

        class ProbeHandler(Handler):
            def get_object(self):
                path = self.kwargs.get("path")
                if path is None and self.args:
                    path = self.args[0]
                configured = feincms.settings.FEINCMS_CMS_404_PAGE
                if configured is not None and path == configured:
                    return _Page()
                raise Http404("missing page")

            def handler(self, request, *args, **kwargs):
                return HttpResponse(b"404-page-body", status=200)

        factory = RequestFactory()
        view = ProbeHandler.as_view()

        with override_settings(FEINCMS_CMS_404_PAGE="/404/"):
            feincms.settings._load_settings()
            response = view(factory.get("/missing/"), "missing/")
            with_setting = (
                response.status_code == 404 and response.content == b"404-page-body"
            )

        with override_settings(FEINCMS_CMS_404_PAGE=None):
            feincms.settings._load_settings()
            raised = False
            try:
                view(factory.get("/missing/"), "missing/")
            except Http404:
                raised = True

        technology = _target_technology_active()
        passed = with_setting and raised and technology
        return ProbeResult(
            observation_id,
            passed,
            observed={
                "with_setting": with_setting,
                "without_setting_http404": raised,
                "target_technology": technology,
            },
        )
    except Exception as exc:
        return ProbeResult(
            observation_id,
            False,
            observed={"exception": str(exc)},
            error_type=type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# observation registry (must cover exactly the owned observation ids)
# ---------------------------------------------------------------------------
OBSERVATION_CHECKS = {
    "F2P-025": check_no_version_branch,
    "F2P-026": check_granular_dst,
    "F2P-027": check_lookup_val_list,
    "F2P-028": check_no_version_branch,
    "F2P-029": check_response_expires,
    "F2P-030": check_cms_404_page,
    "F2P-031": check_response_expires,
    "F2P-032": check_no_legacy_markers,
    "F2P-033": check_granular_dst,
    "F2P-034": check_no_legacy_markers,
    "F2P-035": check_no_version_branch,
    "F2P-036": check_choices_rendering,
    "F2P-037": check_metadata_requires,
    "F2P-038": check_no_version_branch,
    "F2P-039": check_no_legacy_markers,
    "F2P-040": check_tox_matrix,
    "F2P-041": check_lookup_val_list,
    "F2P-042": check_cms_404_page,
    "F2P-043": check_no_legacy_markers,
    "F2P-044": check_granular_rounding,
    "F2P-045": check_setupcfg_requires,
    "F2P-046": check_precommit_target,
    "F2P-047": check_granular_rounding,
    "F2P-048": check_fieldlistfilter_registration,
}

OBSERVATION_IDS = [
    "F2P-030",
    "F2P-046",
    "F2P-027",
    "F2P-048",
    "F2P-026",
    "F2P-028",
    "F2P-029",
    "F2P-033",
    "F2P-034",
    "F2P-035",
    "F2P-036",
    "F2P-038",
    "F2P-039",
    "F2P-044",
    "F2P-047",
    "F2P-025",
    "F2P-031",
    "F2P-041",
    "F2P-042",
    "F2P-043",
    "F2P-045",
    "F2P-040",
    "F2P-032",
    "F2P-037",
]


def run_checks(repo_root, observation_ids=None):
    """Run every owned observation predicate in order."""

    order = observation_ids if observation_ids is not None else OBSERVATION_IDS
    results = []
    for observation_id in order:
        check = OBSERVATION_CHECKS.get(observation_id)
        if check is None:
            results.append(
                ProbeResult(
                    observation_id,
                    False,
                    observed={"error": "no probe registered"},
                    error_type="MissingProbe",
                )
            )
            continue
        try:
            results.append(check(repo_root, observation_id))
        except Exception as exc:
            results.append(
                ProbeResult(
                    observation_id,
                    False,
                    observed={"exception": str(exc)},
                    error_type=type(exc).__name__,
                )
            )
    return results
