"""Executable STATE projection for BATCH-STATE-0001.

The module exposes three entry points so that the integration runner
(``batch-exec``) can dispatch this batch no matter how it discovers it:

``run(repo_root)``
    Execute the probe and return the raw, JSON-serialisable result including a
    per-observation ``observations`` list and a top level ``result`` wrapper.
``project(repo_root)``
    Return only the per-observation projection::

        [{"observation_id": "STATE-001", "kind": "STATE",
          "status": "PASS", "value": {...}}, ...]

Executing the file as a script prints ``project(...)`` as JSON.

The probe verifies the declared persistent-state invariant:

* ``makemigrations --check --dry-run`` reports no pending model changes;
* ``migrate`` applies ``testapp.migrate.page`` and
  ``testapp.migrate.medialibrary`` over an already migrated schema;
* rows stored in the ``page`` and ``medialibrary`` tables are not changed by the
  repeated ``migrate``.

Every subprocess is bounded by an explicit ``timeout``; the projection never
waits indefinitely and never invokes a privileged or interactive command.
"""

import json
import os
import subprocess
import sys
import tempfile

DEFAULT_TIMEOUT_SECONDS = 300

PAGE_MIGRATIONS = [
    "0001_initial",
    "0002_alter_applicationcontent_parent_and_more",
]
MEDIALIBRARY_MIGRATIONS = ["0001_initial"]
OBSERVATIONS = ("STATE-001", "STATE-002")
PAGE_SEED = {"title": "State Page", "slug": "state-page"}
MEDIAFILE_SEED = {"file": "state.txt"}


def _execute(repo_root, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Run the persistent-state probe in the current process."""

    result = {
        "ok": False,
        "error_type": "",
        "timed_out": False,
    }

    manage = os.path.join(repo_root, "tests", "manage.py")
    try:
        checked = subprocess.run(
            [sys.executable, manage, "makemigrations", "--check", "--dry-run"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        result["error_type"] = "Timeout"
        result["timed_out"] = True
        return result

    result["makemigrations_returncode"] = checked.returncode
    if checked.returncode != 0:
        result["error_type"] = "MakemigrationsDetectedChanges"
        return result

    sys.path.insert(0, repo_root)
    sys.path.insert(0, os.path.join(repo_root, "tests"))
    os.environ["DJANGO_SETTINGS_MODULE"] = "testapp.settings"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    import django

    django.setup()

    from django.conf import settings
    from django.core.management import call_command

    tmp_dir = tempfile.mkdtemp(prefix="batch-state-0001-")
    settings.DATABASES["default"]["NAME"] = os.path.join(tmp_dir, "state.sqlite3")
    settings.MEDIA_ROOT = os.path.join(tmp_dir, "media")
    os.makedirs(settings.MEDIA_ROOT, exist_ok=True)

    call_command("migrate", verbosity=0, interactive=False)

    from feincms.module.page.models import Page
    from feincms.module.medialibrary.models import MediaFile

    with open(
        os.path.join(settings.MEDIA_ROOT, MEDIAFILE_SEED["file"]), "w", encoding="utf-8"
    ) as handle:
        handle.write("state\n")

    page = Page.objects.create(**PAGE_SEED)
    mediafile = MediaFile.objects.create(**MEDIAFILE_SEED)

    page_before = list(Page.objects.values_list("pk", "title", "slug"))
    media_before = list(MediaFile.objects.values_list("pk", "type"))

    call_command("migrate", verbosity=0, interactive=False)

    page_after = list(Page.objects.values_list("pk", "title", "slug"))
    media_after = list(MediaFile.objects.values_list("pk", "type"))

    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    applied = sorted(
        MigrationRecorder(connection).migration_qs.values_list("app", "name")
    )
    page_applied = sorted(name for app, name in applied if app == "page")
    media_applied = sorted(name for app, name in applied if app == "medialibrary")

    result["page_migrations_applied"] = page_applied
    result["medialibrary_migrations_applied"] = media_applied
    result["page_migration_set_matches"] = page_applied == PAGE_MIGRATIONS
    result["medialibrary_migration_set_matches"] = (
        media_applied == MEDIALIBRARY_MIGRATIONS
    )
    result["page_rows_preserved"] = page_before == page_after and page_before == [
        (page.pk, PAGE_SEED["title"], PAGE_SEED["slug"])
    ]
    result["mediafile_rows_preserved"] = media_before == media_after and media_before == [
        (mediafile.pk, "txt")
    ]
    result["ok"] = all(
        (
            result["page_migration_set_matches"],
            result["medialibrary_migration_set_matches"],
            result["page_rows_preserved"],
            result["mediafile_rows_preserved"],
        )
    )
    if not result["ok"]:
        result["error_type"] = "StateNotPreserved"
    return result


def _projections(raw):
    """Map the raw probe result onto the declared STATE observations.

    The projection is deliberately free of environment specific values (no
    repository path, no timing data) so that the source and target sides produce
    the very same normalised value.
    """

    timed_out = bool(raw.get("timed_out"))
    page_rows = bool(raw.get("page_rows_preserved"))
    media_rows = bool(raw.get("mediafile_rows_preserved"))
    page_migrations = bool(raw.get("page_migration_set_matches"))
    media_migrations = bool(raw.get("medialibrary_migration_set_matches"))
    no_pending = raw.get("makemigrations_returncode") == 0

    critical = {
        "result": "PASS" if (page_rows and media_rows and not timed_out) else "FAIL",
        "page_rows_preserved": page_rows,
        "mediafile_rows_preserved": media_rows,
        "page_migration_set_matches": page_migrations,
        "medialibrary_migration_set_matches": media_migrations,
    }
    required = {
        "result": "PASS"
        if (page_migrations and media_migrations and no_pending and not timed_out)
        else "FAIL",
        "makemigrations_returncode": raw.get("makemigrations_returncode"),
        "page_migrations_applied": list(raw.get("page_migrations_applied") or []),
        "medialibrary_migrations_applied": list(
            raw.get("medialibrary_migrations_applied") or []
        ),
        "page_migration_set_matches": page_migrations,
        "medialibrary_migration_set_matches": media_migrations,
    }
    return [
        {
            "observation_id": "STATE-001",
            "kind": "STATE",
            "status": critical["result"],
            "value": critical,
        },
        {
            "observation_id": "STATE-002",
            "kind": "STATE",
            "status": required["result"],
            "value": required,
        },
    ]


def _augment(raw):
    augmented = dict(raw)
    augmented["result"] = "PASS" if raw.get("ok") else "FAIL"
    augmented["observations"] = _projections(raw)
    return augmented


def run(repo_root=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Run the probe in an isolated subprocess and return its augmented result."""

    repo_root = repo_root or os.environ.get("MIGRATION_REPO", "/workspace/repo")
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, os.path.abspath(__file__), str(repo_root)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _augment({"ok": False, "error_type": "Timeout", "timed_out": True})

    payload = None
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    if payload is None:
        return _augment(
            {
                "ok": False,
                "error_type": "ProbeError",
                "timed_out": False,
                "returncode": completed.returncode,
            }
        )
    return _augment(payload)


def project(repo_root=None, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Return the per-observation projection for this batch."""

    return run(repo_root, timeout)["observations"]


if __name__ == "__main__":
    root = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.environ.get("MIGRATION_REPO", "/workspace/repo")
    )
    print(json.dumps(_augment(_execute(root, DEFAULT_TIMEOUT_SECONDS)), sort_keys=True))
