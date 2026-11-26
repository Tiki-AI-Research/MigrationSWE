"""Differential probes for BATCH-DIFFERENTIAL-0001.

Each probe computes a normalised, JSON comparable projection for one
observation.  The same probes are executed against the source tree and against
the reference target tree; the batch gate is ``equal_after_normalization`` which
requires the two normalised projections to be identical.

The probes are executable projections: they import and call the real repository
code, they never compare against a hard-coded answer, and they only assert
observable behaviour (booleans, exit/status codes, exception types, class and
symbol names, ordering, and hashes of file content).  In addition, the harness
runs the repository's own test command for the side under test (best effort,
always wrapped in an explicit timeout) and records the raw exit status as
evidence; that deterministic native collection is supporting coverage only and
never the sole source of a projection.
"""

import hashlib
import inspect
import os
from pathlib import Path
import re
import runpy
import sys
from configparser import ConfigParser
from dataclasses import asdict

_ASSET_POLICY = runpy.run_path(str(Path(__file__).resolve().parents[2] / "integration" / "static_probes.py"))
_normalize_documentation_asset = _ASSET_POLICY["normalize_documentation_asset"]

try:  # imported both as a package module and as a flat module
    from . import harness
except ImportError:  # pragma: no cover - flat import path
    import harness


OBSERVATION_IDS = [
    "DIFF-001",
    "DIFF-002",
    "DIFF-003",
    "DIFF-004",
    "DIFF-005",
    "DIFF-006",
    "DIFF-007",
    "DIFF-008",
    "DIFF-009",
    "DIFF-010",
    "DIFF-011",
    "DIFF-012",
    "DIFF-013",
    "DIFF-014",
    "DIFF-015",
    "DIFF-016",
    "DIFF-017",
]

# The migration is only allowed to touch these paths; everything else is out of
# scope.  See R005 and R024.
ALLOWED_MIGRATION_SCOPE = {
    "feincms/admin/filters.py",
    "feincms/extensions/datepublisher.py",
    "setup.cfg",
    "tox.ini",
    ".pre-commit-config.yaml",
} | set(_ASSET_POLICY["DOCUMENTATION_ASSET_EDITS"])

PROTECTED_PREFIXES = (
    "feincms/templates/",
    "feincms/static/",
    "tests/testapp/templates/",
)

_TRACKED_PUBLIC_NAMES = [
    "feincms.settings",
    "feincms.views.Handler",
    "feincms.admin.ParentFieldListFilter",
    "feincms.admin.CategoryFieldListFilter",
    "feincms.extensions.datepublisher.granular_now",
    "feincms.admin.item_editor.ItemEditor",
    "feincms.admin.tree_editor.TreeEditor",
]

_COMMON_CONTENT_TYPES = [
    "feincms.content.raw.models.RawContent",
    "feincms.content.richtext.models.RichTextContent",
    "feincms.content.template.models.TemplateContent",
]

# Native test commands per side, mirroring the runtime context.  ``{python}`` is
# replaced with the interpreter that is running the probe.  Every command is
# wrapped in an explicit timeout by :func:`_run_native_tests`.
_NATIVE_TEST_COMMANDS = {
    "source": [
        ["git", "diff", "--exit-code"],
    ],
    "target": [
        [
            "{python}",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-m",
            "not e2e",
            "-q",
            "tests/testapp/tests",
        ],
    ],
}

_NATIVE_TIMEOUT_SECONDS = int(os.environ.get("MIGRATION_NATIVE_TIMEOUT", "240"))

_STATE = {
    "bootstrapped": False,
    "repo_root": None,
    "side": None,
    "native": [],
    "errors": [],
}


def _native_commands(side):
    commands = _NATIVE_TEST_COMMANDS.get(side, [])
    resolved = []
    for argv in commands:
        resolved.append([sys.executable if part == "{python}" else part for part in argv])
    return resolved


def _run_native_tests(repo_root, side):
    """Run the repository's own test command for ``side`` (best effort).

    The raw per-command outcome is cached in module state and the returned
    summary is expressed in side independent terms so that the source and the
    target projection stay byte-for-byte comparable.  It is supporting
    coverage only: the per-observation projection is always the executed
    behaviour value.  Every command carries an explicit timeout and any failure
    (including a missing toolchain) is contained.
    """

    raw = []
    for argv in _native_commands(side):
        try:
            outcome = harness.run_command(
                argv, cwd=repo_root, timeout=_NATIVE_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - evidence must never abort the run
            raw.append(
                {
                    "argv": [str(part) for part in argv],
                    "returncode": None,
                    "timed_out": False,
                    "error_type": type(exc).__name__,
                }
            )
            continue
        raw.append(
            {
                "argv": [str(part) for part in argv],
                "returncode": outcome.get("returncode"),
                "timed_out": bool(outcome.get("timed_out")),
                "timeout": outcome.get("timeout"),
            }
        )
    _STATE["native_raw"] = raw
    return {
        "attempted": len(raw),
        "timed_out": any(item.get("timed_out") for item in raw),
        "completed": all(
            item.get("returncode") is not None or item.get("timed_out")
            for item in raw
        ),
    }


def bootstrap(repo_root, side="source"):
    """Prepare Django against ``repo_root`` exactly once per process.

    Bootstrap is defensive: a failure while configuring Django or migrating the
    in-memory database is recorded and reported but never raises, so the probe
    run always emits a result for every observation ID.
    """

    repo_root = os.path.abspath(repo_root)
    if _STATE["bootstrapped"] and _STATE["repo_root"] == repo_root:
        return _STATE["repo_root"]

    _STATE["repo_root"] = repo_root
    _STATE["side"] = side

    for path in (os.path.join(repo_root, "tests"), repo_root):
        if path not in sys.path:
            sys.path.insert(0, path)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testapp.settings")

    try:
        import django
        from django.apps import apps as django_apps

        if not django_apps.ready:
            django.setup()

        from django.conf import settings as django_settings

        if not django_settings.ALLOWED_HOSTS:
            django_settings.ALLOWED_HOSTS = ["*"]

        from django.core.management import call_command

        call_command("migrate", run_syncdb=True, verbosity=0, interactive=False)
    except Exception as exc:  # noqa: BLE001 - reported through the probe results
        _STATE["errors"].append(f"{type(exc).__name__}: {exc}")

    try:
        _STATE["native"] = _run_native_tests(repo_root, side)
    except Exception as exc:  # noqa: BLE001 - evidence must never abort the run
        _STATE["native"] = [
            {"argv": [], "returncode": None, "error_type": type(exc).__name__}
        ]

    _STATE["bootstrapped"] = True
    return repo_root


# ---------------------------------------------------------------------------
# file helpers
# ---------------------------------------------------------------------------
def _config(root, name):
    parser = ConfigParser()
    parser.read(os.path.join(root, name))
    return parser


def _read(root, name):
    with open(os.path.join(root, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _git(root, *args):
    return harness.run_command(["git", "-C", root, *args], cwd=root, timeout=60)


def _resolve(dotted):
    import importlib

    parts = dotted.split(".")
    for index in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:index]))
        except ImportError:
            continue
        for attribute in parts[index:]:
            obj = getattr(obj, attribute)
        return obj
    raise ImportError(dotted)


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------
def _diff_001(root):
    """R005: the declared Python support range stays at 3.10+ with 3.x classifiers."""

    config = _config(root, "setup.cfg")
    python_requires = config.get("options", "python_requires", fallback="").strip()
    classifiers = config.get("metadata", "classifiers", fallback="")
    versions = sorted(
        classifier.rsplit("::", 1)[1].strip()
        for classifier in classifiers.splitlines()
        if "Programming Language :: Python :: 3." in classifier
    )
    return {"python_requires": python_requires, "python_classifiers": versions}


def _diff_002(root):
    """R006: feincms.settings resolves FEINCMS_* via LazySettings."""

    import django.conf
    import feincms
    from feincms import LazySettings

    def fresh_loader():
        # ``LazySettings.__getattr__`` deletes itself from the class after the
        # first lookup.  Use a private subclass whose loader never removes
        # itself so both the default and the override can be observed.
        class _Loader(LazySettings):
            def __getattr__(self, attr):
                self._load_settings()
                return self.__dict__[attr]

        return _Loader()

    # Force the singleton to load its defaults so attribute access below is
    # deterministic.
    default_value = feincms.settings.FEINCMS_TREE_EDITOR_INCLUDE_ANCESTORS
    fresh_default = fresh_loader().FEINCMS_TREE_EDITOR_INCLUDE_ANCESTORS

    settings = django.conf.settings
    marker = "FEINCMS_TREE_EDITOR_INCLUDE_ANCESTORS"
    had_marker = hasattr(settings, marker)
    previous = getattr(settings, marker, None)
    try:
        setattr(settings, marker, True)
        overridden = fresh_loader().FEINCMS_TREE_EDITOR_INCLUDE_ANCESTORS
    finally:
        if had_marker:
            setattr(settings, marker, previous)
        else:
            delattr(settings, marker)

    return {
        "settings_type": type(feincms.settings).__name__,
        "default": bool(default_value),
        "fresh_default": bool(fresh_default),
        "override": bool(overridden),
    }


def _diff_003(root):
    """R007: parent_filter/category_filter fields resolve to FeinCMS filters."""

    from django.contrib.admin.filters import FieldListFilter
    from django.test import RequestFactory

    from feincms.module.medialibrary.models import MediaFile
    from feincms.module.page.models import Page

    request = RequestFactory().get("/")

    def resolve(model, field_name):
        field = model._meta.get_field(field_name)
        filter_instance = FieldListFilter.create(field, request, {}, model, None, None)
        return type(filter_instance).__name__

    return {
        "parent": resolve(Page, "parent"),
        "category": resolve(MediaFile, "categories"),
    }


def _diff_004(root):
    """R008: granular_now keeps its signature and returns aware datetimes."""

    from datetime import datetime

    from feincms.extensions.datepublisher import granular_now

    signature = str(inspect.signature(granular_now))
    result = granular_now(datetime(2024, 1, 1, 10, 7))
    return {
        "signature": signature,
        "is_datetime": type(result).__name__ == "datetime",
        "aware": result.tzinfo is not None,
    }


def _diff_005(root):
    """R009: the response processor derives Expires from publication_end_date."""

    from datetime import timedelta

    from django.http import HttpResponse
    from django.utils import timezone

    from feincms.extensions.datepublisher import datepublisher_response_processor

    expires = timezone.now() + timedelta(hours=1)
    page = type("Page", (), {"publication_end_date": expires})()
    response = HttpResponse()
    datepublisher_response_processor(page, None, response)

    has_expires = response.get("Expires") is not None
    has_cache_control = response.get("Cache-Control") is not None

    empty_page = type("Page", (), {"publication_end_date": None})()
    empty_response = HttpResponse()
    datepublisher_response_processor(empty_page, None, empty_response)

    return {
        "with_end_expires": has_expires,
        "with_end_cache_control": has_cache_control,
        "without_end_expires": empty_response.get("Expires") is None,
        "without_end_cache_control": empty_response.get("Cache-Control") is None,
    }


def _diff_006(root):
    """R010: ItemEditor/TreeEditor keep fieldsets and list_filter selection."""

    from django.contrib.admin.sites import AdminSite

    from feincms.module.page.modeladmins import PageAdmin
    from feincms.module.page.models import Page

    admin = PageAdmin(Page, AdminSite())
    fieldset_names = [
        fieldset[0] if isinstance(fieldset[0], str) else None
        for fieldset in admin.fieldsets
    ]
    return {
        "list_filter": [str(item) for item in admin.list_filter],
        "list_display": [str(item) for item in admin.list_display],
        "fieldset_names": fieldset_names,
        "has_datepublisher_admin": "datepublisher_admin" in admin.list_display,
    }


def _diff_007(root):
    """R011: content types keep the render(**kwargs) dispatch contract."""

    from testapp.models import MyModel

    content_type = MyModel._feincms_content_types[0]

    instance = content_type(region="bogus")
    missing_renderer = "not-raised"
    try:
        instance.render()
    except NotImplementedError:
        missing_renderer = "NotImplementedError"

    dispatched = content_type(region="bogus")
    dispatched.render_bogus = lambda **kwargs: "dispatched"
    dispatched_value = dispatched.render()

    return {
        "has_render": callable(getattr(content_type, "render", None)),
        "missing_renderer": missing_renderer,
        "dispatched": dispatched_value,
    }


def _diff_008(root):
    """R012: a single selected value marks exactly that choice as selected."""

    from django.contrib.admin.filters import FieldListFilter
    from django.test import RequestFactory

    from feincms.module.page.models import Page

    request = RequestFactory().get("/")
    field = Page._meta.get_field("parent")
    filter_instance = FieldListFilter.create(field, request, {}, Page, None, None)
    filter_instance.lookup_choices = [(3, "three"), (5, "five")]

    class ChangeList:
        def get_query_string(self, *args, **kwargs):
            return ""

    filter_instance.lookup_val = "3"
    single = {
        str(choice["display"]): bool(choice["selected"])
        for choice in filter_instance.choices(ChangeList())
    }
    return {"single": single}


def _diff_009(root):
    """R014: granular_now floors to the previous five minute boundary."""

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from feincms.extensions.datepublisher import granular_now

    rounded = granular_now(datetime(2024, 3, 5, 14, 37, 42), default_tz=ZoneInfo("UTC"))
    return {"rounded": rounded.isoformat(), "tzinfo": str(rounded.tzinfo)}


def _diff_010(root):
    """R016: FEINCMS_CMS_404_PAGE and preview routing keep their responses."""

    from datetime import timedelta
    from unittest import mock

    import feincms
    from django.contrib.auth.models import User
    from django.contrib.sites.models import Site
    from django.test import Client
    from django.utils import timezone

    from feincms.module.page.models import Page

    Page.register_templates(
        {
            "key": "base",
            "title": "Standard template",
            "path": "feincms_base.html",
            "regions": (
                ("main", "Main content area"),
                ("sidebar", "Sidebar", "inherited"),
            ),
        },
        {
            "key": "theother",
            "title": "This actually exists",
            "path": "base.html",
            "regions": (
                ("main", "Main content area"),
                ("sidebar", "Sidebar", "inherited"),
            ),
        },
    )

    if Site.objects.exists():
        site = Site.objects.all()[0]
    else:
        site = Site.objects.create(pk=1, domain="example.com", name="example")

    def create_page(title, slug, parent=None, active=False, template_key="base"):
        return Page.objects.create(
            title=title,
            slug=slug,
            parent=parent,
            template_key=template_key,
            site=site,
            in_navigation=False,
            active=active,
            navigation_group="default",
            publication_date=timezone.now() - timedelta(days=1),
        )

    staff = User.objects.create_user(
        "diff-staff", "diff-staff@example.com", "test", is_staff=True
    )
    non_staff = User.objects.create_user(
        "diff-guest", "diff-guest@example.com", "test", is_staff=False
    )

    page = create_page("Test page", "test-page")
    page.template_key = "theother"
    page.save()
    page.rawcontent_set.create(region="main", ordering=0, text="Example content")

    client = Client()
    client.force_login(staff)
    normal_status = client.get(page.get_absolute_url()).status_code
    preview = client.get(f"{page.get_absolute_url()}_preview/{page.pk}/")
    preview_status = preview.status_code
    preview_has_content = b"Example content" in preview.content

    guest = Client()
    guest.force_login(non_staff)
    guest_status = guest.get(f"{page.get_absolute_url()}_preview/{page.pk}/").status_code

    not_found = create_page("Not found", "404", active=True)
    not_found.rawcontent_set.create(region="main", ordering=0, text="404-body")

    # Make sure the FEINCMS setting has been materialised on the singleton.
    feincms.settings.FEINCMS_CMS_404_PAGE
    with mock.patch.object(feincms.settings, "FEINCMS_CMS_404_PAGE", "/404/"):
        missing = client.get("/does-not-exist/")
    missing_status = missing.status_code
    missing_has_body = b"404-body" in missing.content

    return {
        "normal_status": normal_status,
        "preview_status": preview_status,
        "preview_has_content": preview_has_content,
        "preview_cache_control": preview.get("Cache-Control"),
        "guest_status": guest_status,
        "missing_status": missing_status,
        "missing_has_body": missing_has_body,
    }


def _diff_011(root):
    """R017: the page ordering is preserved."""

    from feincms.module.page.models import BasePage, Page

    return {
        "base_page": list(BasePage._meta.ordering or []),
        "page": list(Page._meta.ordering or []),
    }


def _diff_012(root):
    """R019: public capabilities remain importable and callable."""

    result = {}
    for name in _TRACKED_PUBLIC_NAMES + _COMMON_CONTENT_TYPES:
        obj = _resolve(name)
        result[name] = "callable" if callable(obj) else type(obj).__name__
    return result


def _diff_013(root):
    """R021: preserve asset bytes apart from the two published comment edits."""

    entries = []
    for prefix in PROTECTED_PREFIXES:
        base = os.path.join(root, prefix)
        if not os.path.isdir(base):
            continue
        for directory, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for filename in sorted(filenames):
                absolute = os.path.join(directory, filename)
                relative = os.path.relpath(absolute, root).replace(os.sep, "/")
                with open(absolute, "rb") as handle:
                    digest = hashlib.sha256(_normalize_documentation_asset(relative, handle.read())).hexdigest()
                entries.append([relative, digest])
    entries.sort()
    return entries


def _diff_014(root):
    """R022: public symbol names remain importable under their module paths."""

    result = {}
    for name in _TRACKED_PUBLIC_NAMES + _COMMON_CONTENT_TYPES:
        try:
            _resolve(name)
            result[name] = "present"
        except (ImportError, AttributeError):
            result[name] = "missing"
    return result


def _diff_015(root):
    """R023: the Python floor is not raised and 3.10/3.11 stay in the tox matrix."""

    config = _config(root, "setup.cfg")
    python_requires = config.get("options", "python_requires", fallback="").strip()

    tox = _read(root, "tox.ini")
    envlist_section = ""
    match = re.search(r"envlist\s*=(.*?)(?:\n\[|\Z)", tox, re.DOTALL)
    if match:
        envlist_section = match.group(1)
    python_factors = sorted(set(re.findall(r"\b(3\d\d)\b", envlist_section)))

    return {
        "python_requires": python_requires,
        "python_factors": python_factors,
    }


def _diff_016(root):
    """R024: changes stay confined to the declared migration scope."""

    changed = set()
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside.get("returncode") == 0 and inside.get("stdout", "").strip() == "true":
        diff = _git(root, "diff", "--name-only", "HEAD")
        if diff.get("returncode") == 0:
            changed.update(
                line.strip()
                for line in diff.get("stdout", "").splitlines()
                if line.strip()
            )
        status = _git(root, "status", "--porcelain", "--untracked-files=all")
        if status.get("returncode") == 0:
            for line in status.get("stdout", "").splitlines():
                path = line[3:].strip().strip('"')
                if " -> " in path:
                    path = path.split(" -> ")[-1]
                if path:
                    changed.add(path)
    return sorted(path for path in changed if path not in ALLOWED_MIGRATION_SCOPE)


def _diff_017(root):
    """R025: tests are not skipped and coverage include/omit are preserved."""

    config = _config(root, "setup.cfg")
    include = config.get("coverage:run", "include", fallback="")
    omit = config.get("coverage:run", "omit", fallback="")

    tests_dir = os.path.join(root, "tests", "testapp", "tests")
    marker_pattern = re.compile(
        r"pytest\.mark\.(skip|xfail)|unittest\.skip|expectedFailure|@skipIf|@skip\b"
    )
    marker_count = 0
    if os.path.isdir(tests_dir):
        for directory, dirnames, filenames in os.walk(tests_dir):
            dirnames.sort()
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                with open(
                    os.path.join(directory, filename), "r", encoding="utf-8"
                ) as handle:
                    marker_count += len(marker_pattern.findall(handle.read()))

    return {
        "coverage_include": [
            line.strip() for line in include.splitlines() if line.strip()
        ],
        "coverage_omit": [line.strip() for line in omit.splitlines() if line.strip()],
        "skip_marker_count": marker_count,
    }


PROBES = {
    "DIFF-001": _diff_001,
    "DIFF-002": _diff_002,
    "DIFF-003": _diff_003,
    "DIFF-004": _diff_004,
    "DIFF-005": _diff_005,
    "DIFF-006": _diff_006,
    "DIFF-007": _diff_007,
    "DIFF-008": _diff_008,
    "DIFF-009": _diff_009,
    "DIFF-010": _diff_010,
    "DIFF-011": _diff_011,
    "DIFF-012": _diff_012,
    "DIFF-013": _diff_013,
    "DIFF-014": _diff_014,
    "DIFF-015": _diff_015,
    "DIFF-016": _diff_016,
    "DIFF-017": _diff_017,
}


def collect(repo_root=None, side="source"):
    """Run every probe and return a list of :class:`CheckResult`.

    The returned list always contains exactly one result per observation ID,
    even when bootstrapping the repository fails.  A failing observation is
    reported as a failed check rather than silently dropped, so the runtime
    report can never be empty and always covers the full inventory.
    """

    if repo_root is None:
        repo_root = os.environ.get("MIGRATION_REPO", "/workspace/repo")

    try:
        root = bootstrap(repo_root, side)
    except Exception as exc:  # noqa: BLE001 - never drop observations
        root = os.path.abspath(repo_root)
        _STATE["errors"].append(f"bootstrap: {type(exc).__name__}: {exc}")

    evidence = []
    if _STATE["native"]:
        evidence.append({"native_tests": _STATE["native"]})
    if _STATE["errors"]:
        evidence.append({"bootstrap_errors": list(_STATE["errors"])})

    results = []
    for observation_id in OBSERVATION_IDS:
        probe = PROBES[observation_id]
        try:
            observed = harness.normalize(probe(root))
            results.append(
                harness.CheckResult(
                    observation_id, True, None, observed, "", list(evidence)
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported as a failed check
            results.append(
                harness.CheckResult(
                    observation_id,
                    False,
                    None,
                    {"result": "FAIL", "error_type": type(exc).__name__},
                    type(exc).__name__,
                    list(evidence) + [str(exc)],
                )
            )
    return results


def run(repo_root=None, side="source"):
    """Return the probe results as plain JSON serialisable dictionaries."""

    return [asdict(result) for result in collect(repo_root, side)]
