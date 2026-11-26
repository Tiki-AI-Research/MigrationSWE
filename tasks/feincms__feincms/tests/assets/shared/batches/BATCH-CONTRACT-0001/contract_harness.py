"""Shared public-contract probe harness for BATCH-CONTRACT-0001.

The harness computes one normalized, JSON-serialisable value per CONTRACT
observation.  The same module is executed in the source container (the
baseline repository) and in the target container (the migrated repository);
the CONTRACT gate passes only when both sides produce the exact same
normalized value.

Every potentially long-running external command is wrapped in an explicit
``timeout`` (see :func:`run_command`) so the harness can never block
indefinitely.  No privileged operation is used.
"""

from __future__ import annotations

import configparser
import hashlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from datetime import datetime, timedelta

_ASSET_POLICY = runpy.run_path(str(Path(__file__).resolve().parents[2] / "integration" / "static_probes.py"))
_normalize_documentation_asset = _ASSET_POLICY["normalize_documentation_asset"]

OBSERVATION_IDS = [
    "CONTRACT-001",
    "CONTRACT-002",
    "CONTRACT-003",
    "CONTRACT-004",
    "CONTRACT-005",
    "CONTRACT-006",
    "CONTRACT-007",
    "CONTRACT-008",
    "CONTRACT-009",
    "CONTRACT-010",
    "CONTRACT-011",
    "CONTRACT-012",
    "CONTRACT-013",
    "CONTRACT-014",
    "CONTRACT-015",
    "CONTRACT-016",
    "CONTRACT-017",
]

DEFAULT_TIMEOUT_SECONDS = 300

SCOPED_FILES = [
    "feincms/admin/filters.py",
    "feincms/extensions/datepublisher.py",
    "setup.cfg",
    "tox.ini",
    ".pre-commit-config.yaml",
]

ASSET_TREES = [
    "feincms/templates",
    "feincms/static",
    "tests/testapp/templates",
]


def run_command(
    argv,
    cwd,
    timeout=DEFAULT_TIMEOUT_SECONDS,
    env=None,
):
    """Run ``argv`` with an explicit timeout and never block forever."""

    merged = dict(os.environ)
    if env:
        merged.update({str(key): str(value) for key, value in env.items()})
    command = [str(part) for part in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            timeout=timeout,
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


def _norm(value):
    """Normalize values so that two sides can be compared deterministically."""

    if isinstance(value, dict):
        return {str(key): _norm(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    if isinstance(value, set):
        return sorted(_norm(item) for item in value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(repo_root, relative):
    root = os.path.join(repo_root, relative)
    entries = {}
    if not os.path.isdir(root):
        return entries
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            key = os.path.relpath(full, repo_root).replace(os.sep, "/")
            with open(full, "rb") as handle:
                entries[key] = hashlib.sha256(_normalize_documentation_asset(key, handle.read())).hexdigest()
    return entries


def _bootstrap_django(repo_root):
    for candidate in (os.path.join(repo_root, "tests"), repo_root):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "testapp.settings")
    import django

    django.setup()

    from django.test.utils import setup_test_environment

    setup_test_environment()


def _create_test_database():
    from django.db import connection

    return connection.creation.create_test_db(verbosity=0)


def _destroy_test_database(name):
    from django.db import connection

    connection.creation.destroy_test_db(name, verbosity=0)


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def probe_001(repo_root):
    parser = configparser.ConfigParser()
    parser.read(os.path.join(repo_root, "setup.cfg"))
    python_requires = parser.get("options", "python_requires", fallback="")
    classifiers = parser.get("metadata", "classifiers", fallback="")
    python_classifiers = sorted(
        line.strip()
        for line in classifiers.splitlines()
        if line.strip().startswith("Programming Language :: Python ::")
    )
    present = {
        path: os.path.isfile(os.path.join(repo_root, path)) for path in SCOPED_FILES
    }
    return _norm(
        {
            "python_requires": python_requires.strip(),
            "python_classifiers": python_classifiers,
            "scoped_files_present": present,
        }
    )


def probe_002(repo_root):
    import feincms

    lazy = isinstance(feincms.settings, feincms.LazySettings)
    default_value = bool(feincms.settings.FEINCMS_TREE_EDITOR_INCLUDE_ANCESTORS)
    return _norm({"is_lazy_settings": lazy, "default_value": default_value})


def probe_003(repo_root):
    import feincms.admin  # noqa: F401  (registers the field list filters)
    from django.contrib.admin.filters import FieldListFilter

    class _FakeField:
        pass

    def matches(**attrs):
        names = []
        for test, filter_class in FieldListFilter._field_list_filters:
            field = _FakeField()
            for key, value in attrs.items():
                setattr(field, key, value)
            try:
                if test(field):
                    names.append(filter_class.__name__)
            except Exception:  # pragma: no cover - defensive
                continue
        return sorted(names)

    return _norm(
        {
            "parent_filter": matches(parent_filter=True),
            "category_filter": matches(category_filter=True),
        }
    )


def probe_004(repo_root):
    from feincms.extensions.datepublisher import granular_now

    signature = str(inspect.signature(granular_now))
    result = granular_now(datetime(2024, 1, 1, 10, 7))
    return _norm(
        {
            "signature": signature,
            "is_datetime": isinstance(result, datetime),
            "aware": result.tzinfo is not None,
        }
    )


def probe_005(repo_root):
    from email.utils import parsedate_to_datetime

    from django.http import HttpResponse
    from django.utils import timezone

    from feincms.extensions.datepublisher import datepublisher_response_processor

    class _Page:
        pass

    page = _Page()
    page.publication_end_date = timezone.now() + timedelta(hours=1)
    response = HttpResponse()
    datepublisher_response_processor(page, None, response)
    expires = response.get("Expires")
    delta = None
    if expires:
        delta = int((parsedate_to_datetime(expires) - timezone.now()).total_seconds())
    return _norm(
        {
            "has_expires": expires is not None,
            "one_hour_ahead": delta is not None and 3500 <= delta <= 3610,
        }
    )


def _superuser():
    from django.contrib.auth.models import AnonymousUser

    user = AnonymousUser()
    user.is_superuser = True
    user.is_staff = True
    user.is_active = True
    return user


def probe_006(repo_root):
    from django.contrib import admin
    from django.test import RequestFactory

    from feincms.admin.item_editor import FEINCMS_CONTENT_FIELDSET_NAME
    from feincms.module.page.modeladmins import PageAdmin
    from feincms.module.page.models import Page

    request = RequestFactory().get("/admin/")
    request.user = _superuser()
    editor = PageAdmin(Page, admin.site)
    try:
        fieldsets = editor.get_fieldsets(request, obj=None)
        fieldset_names = [entry[0] for entry in fieldsets]
    except Exception:  # pragma: no cover - defensive
        fieldset_names = []
    list_filter = sorted(str(item) for item in editor.get_list_filter(request))
    return _norm(
        {
            "content_fieldset_present": FEINCMS_CONTENT_FIELDSET_NAME
            in fieldset_names,
            "fieldset_names": sorted(str(name) for name in fieldset_names),
            "list_filter": list_filter,
        }
    )


def probe_007(repo_root):
    from django.db import models

    from testapp.models import ExampleCMSBase

    class _Something(models.Model):
        class Meta:
            abstract = True

        def render_region(self):
            return "Hello"

    existed = hasattr(ExampleCMSBase, "_feincms_content_types")
    preserved = list(getattr(ExampleCMSBase, "_feincms_content_types", []))
    try:
        content_type = ExampleCMSBase.create_content_type(_Something)
        obj = content_type()
        try:
            obj.render()
            no_region_error = None
        except NotImplementedError:
            no_region_error = "NotImplementedError"
        obj.region = "region"
        dispatched = obj.render()
    finally:
        if existed:
            ExampleCMSBase._feincms_content_types = preserved
    return _norm(
        {"no_region_error": no_region_error, "dispatched": dispatched}
    )


def _fake_filter(filter_class, lookup_val, lookup_choices):
    instance = filter_class.__new__(filter_class)
    instance.lookup_val = lookup_val
    instance.lookup_kwarg = "field"
    instance.lookup_choices = lookup_choices

    class _Changelist:
        def get_query_string(self, *args, **kwargs):
            return ""

    return instance, _Changelist()


def probe_008(repo_root):
    from feincms.admin.filters import (
        CategoryFieldListFilter,
        ParentFieldListFilter,
    )

    choices = [(3, "three"), (5, "five")]

    def selected(filter_class, lookup_val):
        instance, changelist = _fake_filter(filter_class, lookup_val, choices)
        result = {}
        for choice in instance.choices(changelist):
            if choice["display"] in ("three", "five"):
                result[choice["display"]] = bool(choice["selected"])
        return result

    return _norm(
        {
            "parent_single": selected(ParentFieldListFilter, "3"),
            "category_single": selected(CategoryFieldListFilter, "3"),
        }
    )


def probe_009(repo_root):
    from zoneinfo import ZoneInfo

    from feincms.extensions.datepublisher import granular_now

    result = granular_now(
        datetime(2024, 3, 5, 14, 37, 42), default_tz=ZoneInfo("UTC")
    )
    expected = datetime(2024, 3, 5, 14, 35, tzinfo=ZoneInfo("UTC"))
    return _norm(
        {
            "result": result,
            "matches_expected": result == expected,
            "minute": result.minute,
            "second": result.second,
        }
    )


def probe_010(repo_root):
    from django.contrib.sites.models import Site
    from django.test import Client
    from django.utils import timezone

    from feincms import settings as feincms_settings
    from feincms.module.page.models import Page

    Page.register_templates(
        {
            "key": "base",
            "title": "Standard template",
            "path": "feincms_base.html",
            "regions": (("main", "Main content area"),),
        }
    )
    site = Site.objects.order_by("pk").first() or Site.objects.create(
        domain="example.com", name="example.com"
    )
    now = timezone.now()
    notfound = Page.objects.create(
        title="notfound",
        slug="404",
        override_url="/404/",
        template_key="base",
        site=site,
        active=True,
        in_navigation=False,
        publication_date=now - timedelta(days=1),
    )
    notfound.rawcontent_set.create(region="main", ordering=0, text="CONTRACT404PAGE")

    old = feincms_settings.FEINCMS_CMS_404_PAGE
    feincms_settings.FEINCMS_CMS_404_PAGE = "/404/"
    client = Client()
    try:
        response = client.get("/definitely-missing-path/")
        marker = "CONTRACT404PAGE" in response.content.decode("utf-8", "replace")
        status = response.status_code
    finally:
        feincms_settings.FEINCMS_CMS_404_PAGE = old

    preview_missing = Page.objects.create(
        title="preview",
        slug="preview",
        template_key="base",
        site=site,
        active=False,
        in_navigation=False,
        publication_date=now - timedelta(days=1),
    )
    anon = Client()
    non_staff_status = anon.get(
        f"{preview_missing.get_absolute_url()}_preview/{preview_missing.pk}/"
    ).status_code
    return _norm(
        {
            "missing_status": status,
            "missing_served_page": marker,
            "preview_non_staff_status": non_staff_status,
        }
    )


def probe_011(repo_root):
    from feincms.module.page.models import Page

    ordering = list(Page._meta.ordering)
    abstract = [
        list(cls._meta.ordering)
        for cls in Page.__mro__
        if getattr(cls, "_meta", None) is not None
        and getattr(cls._meta, "abstract", False)
        and cls.__name__ == "BasePage"
    ]
    return _norm(
        {
            "page_ordering": ordering,
            "base_page_ordering": abstract,
        }
    )


def probe_012(repo_root):
    import importlib

    targets = [
        ("feincms", "settings"),
        ("feincms.views", "Handler"),
        ("feincms.admin", "ParentFieldListFilter"),
        ("feincms.extensions.datepublisher", "granular_now"),
        ("feincms.admin.item_editor", "ItemEditor"),
        ("feincms.admin.tree_editor", "TreeEditor"),
    ]
    importable = {}
    for module_name, attribute in targets:
        try:
            module = importlib.import_module(module_name)
            importable[f"{module_name}.{attribute}"] = hasattr(module, attribute)
        except Exception:
            importable[f"{module_name}.{attribute}"] = False
    return _norm({"importable": importable})


def probe_013(repo_root):
    manifest = {}
    for tree in ASSET_TREES:
        manifest.update(_tree_manifest(repo_root, tree))
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return _norm(
        {
            "asset_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "asset_count": len(manifest),
        }
    )


def probe_014(repo_root):
    return probe_012(repo_root)


def probe_015(repo_root):
    parser = configparser.ConfigParser()
    parser.read(os.path.join(repo_root, "setup.cfg"))
    python_requires = parser.get("options", "python_requires", fallback="").strip()
    tox = _read(os.path.join(repo_root, "tox.ini"))
    py_envs = sorted(
        marker
        for marker in ("py310", "py311", "py312", "py313")
        if marker in tox
    )
    return _norm(
        {
            "python_requires": python_requires,
            "python_envs": py_envs,
        }
    )


def probe_016(repo_root):
    allowed = set(SCOPED_FILES) | set(_ASSET_POLICY["DOCUMENTATION_ASSET_EDITS"])
    diff_files = []
    if os.path.isdir(os.path.join(repo_root, ".git")):
        result = run_command(
            ["git", "diff", "--name-only", "HEAD"], cwd=repo_root
        )
        if result["returncode"] == 0:
            diff_files = [
                line.strip() for line in result["stdout"].splitlines() if line.strip()
            ]
    return _norm(
        {
            "within_scope": set(diff_files) <= allowed,
            "scope": sorted(allowed),
        }
    )


def probe_017(repo_root):
    parser = configparser.ConfigParser()
    parser.read(os.path.join(repo_root, "setup.cfg"))
    include = parser.get("coverage:run", "include", fallback="")
    omit = parser.get("coverage:run", "omit", fallback="")
    include_entries = sorted(line.strip() for line in include.splitlines() if line.strip())
    omit_entries = sorted(line.strip() for line in omit.splitlines() if line.strip())

    tests_root = os.path.join(repo_root, "tests", "testapp", "tests")
    skip_markers = []
    if os.path.isdir(tests_root):
        for dirpath, dirnames, filenames in os.walk(tests_root):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                rel = os.path.relpath(
                    os.path.join(dirpath, filename), repo_root
                ).replace(os.sep, "/")
                text = _read(os.path.join(dirpath, filename))
                for marker in ("skip", "xfail"):
                    if f"@{marker}" in text or f".{marker}(" in text:
                        skip_markers.append(f"{rel}:{marker}")
    return _norm(
        {
            "coverage_include": include_entries,
            "coverage_omit": omit_entries,
            "skip_markers": sorted(skip_markers),
        }
    )


PROBES = {
    "CONTRACT-001": probe_001,
    "CONTRACT-002": probe_002,
    "CONTRACT-003": probe_003,
    "CONTRACT-004": probe_004,
    "CONTRACT-005": probe_005,
    "CONTRACT-006": probe_006,
    "CONTRACT-007": probe_007,
    "CONTRACT-008": probe_008,
    "CONTRACT-009": probe_009,
    "CONTRACT-010": probe_010,
    "CONTRACT-011": probe_011,
    "CONTRACT-012": probe_012,
    "CONTRACT-013": probe_013,
    "CONTRACT-014": probe_014,
    "CONTRACT-015": probe_015,
    "CONTRACT-016": probe_016,
    "CONTRACT-017": probe_017,
}

#: Probes that need a live database.
DB_PROBES = {"CONTRACT-007", "CONTRACT-010"}
#: Probes that need Django to be configured/imported.
DJANGO_PROBES = {
    "CONTRACT-002",
    "CONTRACT-003",
    "CONTRACT-004",
    "CONTRACT-005",
    "CONTRACT-006",
    "CONTRACT-007",
    "CONTRACT-008",
    "CONTRACT-009",
    "CONTRACT-010",
    "CONTRACT-011",
    "CONTRACT-012",
    "CONTRACT-014",
}


def collect(repo_root):
    """Return ``[(observation_id, ok, value, error), ...]`` for all probes."""

    _bootstrap_django(repo_root)
    database_name = _create_test_database()
    results = []
    try:
        for observation_id in OBSERVATION_IDS:
            probe = PROBES[observation_id]
            try:
                value = probe(repo_root)
                results.append((observation_id, True, _norm(value), None))
            except Exception as exc:  # pragma: no cover - reported, not raised
                results.append(
                    (observation_id, False, None, f"{type(exc).__name__}: {exc}")
                )
    finally:
        _destroy_test_database(database_name)
    return results


def write_report(path, results):
    payload = [
        {
            "observation_id": observation_id,
            "passed": ok,
            "observed": value,
            "error_type": error or "",
        }
        for observation_id, ok, value, error in results
    ]
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path
