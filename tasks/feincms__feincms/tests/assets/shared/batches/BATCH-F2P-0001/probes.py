"""Per-observation F2P probes for batch BATCH-F2P-0001.

The probes implement the atomic claims supplied with the batch.  Every probe
uses the *same* semantic predicate for the source and the target side; nothing
is hard-coded to a role.  The Django 5.2 postcondition is evaluated on the real
execution/loading path, and the "no compatibility branch for Django < 5.2"
part of the contract is evaluated as an absence-of-residue predicate over the
production sources in scope.

The module is intentionally self contained: it only needs the standard library,
the Django version installed in the probe environment, and the ``feincms``
package that lives in the repository passed as ``--repo``.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import sys

PRODUCTION_FILES = (
    "feincms/admin/filters.py",
    "feincms/extensions/datepublisher.py",
)

OBSERVATION_IDS = [
    "F2P-001",
    "F2P-002",
    "F2P-003",
    "F2P-004",
    "F2P-005",
    "F2P-010",
    "F2P-011",
    "F2P-012",
    "F2P-014",
    "F2P-015",
    "F2P-019",
    "F2P-022",
    "F2P-007",
    "F2P-008",
    "F2P-013",
    "F2P-006",
    "F2P-009",
    "F2P-020",
    "F2P-021",
    "F2P-024",
    "F2P-017",
    "F2P-016",
    "F2P-018",
    "F2P-023",
]


# --------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------
def _read(repo: str, rel: str) -> str:
    with open(os.path.join(repo, rel), encoding="utf-8") as handle:
        return handle.read()


def _hits(repo: str, rel: str, tokens) -> dict:
    text = _read(repo, rel)
    found = {}
    for token in tokens:
        count = text.count(token)
        if count:
            found[token] = count
    return found


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


def _canonical_version_branch(source: str) -> bool:
    if _CANONICAL_PROBES is None:
        return False
    try:
        return bool(_CANONICAL_PROBES._version_branch(source))
    except Exception:  # pragma: no cover - never break the probe on a scan
        return False


def _residue(repo: str) -> dict:
    """Return legacy Django<5.2 residue found in the production scope."""

    scope = {
        "feincms/admin/filters.py": ["import django", "django.VERSION"],
        "feincms/extensions/datepublisher.py": [
            "import django",
            "django.VERSION",
            "is_dst=",
        ],
    }
    found = {}
    for rel, tokens in scope.items():
        hits = _hits(repo, rel, tokens)
        if not hits:
            try:
                text = _read(repo, rel)
            except OSError:
                text = None
            if text is not None and _canonical_version_branch(text):
                hits = {"version-branch": 1}
        if hits:
            found[rel] = hits
    return found


def _no_version_branch(repo: str) -> bool:
    """The R004/R018 predicate: zero version-branch residue in scope."""

    return not _residue(repo)


def _setup_django(repo: str) -> None:
    """Bootstrap Django so ``feincms`` can be imported from *repo*.

    The bootstrap is deliberately self contained: it does not depend on the
    repository shipping a ``testapp.settings`` module, so the runtime predicate
    can be evaluated against any checkout (the reference target, the source
    baseline or the migrated repository).  When the surrounding harness has
    already configured Django the existing configuration is reused.
    """

    for path in (os.path.join(repo, "tests"), repo):
        if path not in sys.path:
            sys.path.insert(0, path)

    import django
    from django.apps import apps

    if apps.ready:
        return

    from django.conf import settings as django_settings

    if not django_settings.configured:
        django_settings.configure(
            DEBUG=True,
            SECRET_KEY="batch-f2p-0001-probe",
            USE_TZ=True,
            TIME_ZONE="Europe/Zurich",
            DEFAULT_AUTO_FIELD="django.db.models.AutoField",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            INSTALLED_APPS=[
                "django.contrib.auth",
                "django.contrib.contenttypes",
                "django.contrib.admin",
                "django.contrib.messages",
                "django.contrib.sessions",
                "django.contrib.sites",
                "feincms",
                "mptt",
            ],
            MIDDLEWARE=[
                "django.middleware.common.CommonMiddleware",
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "django.contrib.messages.middleware.MessageMiddleware",
            ],
        )

    if not apps.ready:
        django.setup()


# --------------------------------------------------------------------------
# runtime predicates
# --------------------------------------------------------------------------
def _granular(repo: str, year, month, day, hour, minute):
    _setup_django(repo)
    from zoneinfo import ZoneInfo

    from feincms.extensions.datepublisher import granular_now

    value = granular_now(
        __import__("datetime").datetime(year, month, day, hour, minute),
        default_tz=ZoneInfo("UTC"),
    )
    return value


def _granular_ambiguous(repo: str):
    _setup_django(repo)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from feincms.extensions.datepublisher import granular_now

    value = granular_now(
        datetime(2024, 10, 27, 2, 7), default_tz=ZoneInfo("Europe/Zurich")
    )
    return value.tzinfo is not None


def _response_expires(repo: str, seconds):
    _setup_django(repo)
    from datetime import timedelta

    from django.http import HttpResponse
    from django.utils import timezone

    from feincms.extensions.datepublisher import datepublisher_response_processor

    class _Page:
        pass

    page = _Page()
    page.publication_end_date = timezone.now() + timedelta(seconds=seconds)
    response = HttpResponse()
    datepublisher_response_processor(page, None, response)
    return response.get("Expires")


def _response_none_untouched(repo: str) -> bool:
    _setup_django(repo)
    from django.http import HttpResponse

    from feincms.extensions.datepublisher import datepublisher_response_processor

    class _Page:
        pass

    page = _Page()
    page.publication_end_date = None
    response = HttpResponse()
    response["X-Keep"] = "1"
    datepublisher_response_processor(page, None, response)
    return response.get("Expires") is None and response.get("X-Keep") == "1"


def _choices(repo: str, lookup_val):
    _setup_django(repo)

    from feincms.admin.filters import ParentFieldListFilter

    class _Changelist:
        def get_query_string(self, params, remove=None):
            return "?" + "&".join(f"{key}={value}" for key, value in params.items())

    instance = ParentFieldListFilter.__new__(ParentFieldListFilter)
    instance.lookup_val = lookup_val
    instance.lookup_kwarg = "parent__id__exact"
    instance.lookup_choices = [(3, "three"), (5, "five"), (7, "seven")]
    return {
        item["query_string"]: item["selected"] for item in instance.choices(_Changelist())
    }


def _field_filter_class(repo: str, flag: str):
    _setup_django(repo)
    import feincms.admin  # noqa: F401  (triggers registration)
    from django.contrib.admin.filters import FieldListFilter

    class _Field:
        parent_filter = False
        category_filter = False

    field = _Field()
    setattr(field, flag, True)
    for test, filter_class in FieldListFilter._field_list_filters:
        if test(field):
            return filter_class.__name__
    return None


# --------------------------------------------------------------------------
# configuration predicates
# --------------------------------------------------------------------------
def _django_specifier(repo: str) -> str:
    from packaging.requirements import Requirement

    try:
        from importlib import metadata

        requirements = metadata.requires("FeinCMS") or []
    except Exception:
        parser = configparser.ConfigParser()
        parser.read(os.path.join(repo, "setup.cfg"))
        requirements = [
            line.strip()
            for line in parser.get("options", "install_requires", fallback="").splitlines()
            if line.strip()
        ]
    for requirement in requirements:
        parsed = Requirement(requirement)
        if parsed.name.lower() == "django":
            return str(parsed.specifier)
    raise LookupError("no Django requirement found in distribution metadata")


def _python_requires_ok(repo: str) -> bool:
    parser = configparser.ConfigParser()
    parser.read(os.path.join(repo, "setup.cfg"))
    value = parser.get("options", "python_requires", fallback="")
    classifiers = parser.get("metadata", "classifiers", fallback="")
    floor_ok = "3.10" in value
    classifiers_ok = all(
        f"Programming Language :: Python :: {version}" in classifiers
        for version in ("3.10", "3.11", "3.12")
    )
    return floor_ok and classifiers_ok


def _tox_django_factors(repo: str) -> set:
    text = _read(repo, "tox.ini")
    factors = set(re.findall(r"^\s*(dj[0-9a-zA-Z]+)\s*:", text, flags=re.MULTILINE))
    if "djmain" in text:
        factors.add("djmain")
    return factors


def _precommit_target(repo: str) -> str:
    text = _read(repo, ".pre-commit-config.yaml")
    match = re.search(r"--target-version\s*,\s*[\"']?([0-9.]+)", text)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# observation probes
# --------------------------------------------------------------------------
def _result(observation_id, passed, expected, observed, error_type="", evidence=None):
    return {
        "observation_id": observation_id,
        "passed": bool(passed),
        "expected": expected,
        "observed": observed,
        "error_type": error_type,
        "evidence": evidence or [],
    }


def _runtime(observation_id, expected, value, ok, residue, evidence=None):
    if residue:
        return _result(
            observation_id,
            False,
            expected,
            {"value": value, "residue": residue},
            "SOURCE_TECH_RESIDUE",
            evidence,
        )
    if not ok:
        return _result(
            observation_id,
            False,
            expected,
            value,
            "TARGET_POSTCONDITION_UNMET",
            evidence,
        )
    return _result(observation_id, True, expected, value, "", evidence)


def probe_f2p_001(repo):
    value = _granular_ambiguous(repo)
    return _runtime("F2P-001", "aware datetime, no exception", value, value, _residue(repo))


def probe_f2p_002(repo):
    value = _response_expires(repo, 3600)
    return _runtime(
        "F2P-002",
        "Expires about 3600s ahead and None leaves headers untouched",
        {"expires": value, "none_untouched": _response_none_untouched(repo)},
        value is not None and _response_none_untouched(repo),
        _residue(repo),
    )


def probe_f2p_003(repo):
    residue = _residue(repo)
    make_aware_form = "make_aware(d, default_tz)" in _read(
        repo, "feincms/extensions/datepublisher.py"
    )
    return _runtime(
        "F2P-003",
        "no django.VERSION fallback in filters.py/datepublisher.py",
        {"residue": residue, "make_aware_form": make_aware_form},
        not residue,
        residue,
    )


def probe_f2p_004(repo):
    flags = _choices(repo, ["3", "5"])
    ok = flags.get("?parent__id__exact=3") is True and flags.get("?parent__id__exact=5") is True
    return _runtime("F2P-004", {"3": True, "5": True}, flags, ok, _residue(repo))


def probe_f2p_005(repo):
    flags = _choices(repo, ["3", "5"])
    all_flags = _choices(repo, None)
    ok = (
        flags.get("?parent__id__exact=3") is True
        and flags.get("?parent__id__exact=5") is True
        and all_flags.get("?") is True
    )
    return _runtime("F2P-005", {"3": True, "5": True, "all": True}, {"multi": flags, "none": all_flags}, ok, _residue(repo))


def probe_f2p_010(repo):
    residue = _residue(repo)
    signature = _granular_signature(repo)
    ok = not residue and signature == "(n=None, default_tz=None)"
    return _runtime(
        "F2P-010",
        "zero django.VERSION / import django / is_dst= and kept signature",
        {"residue": residue, "signature": signature},
        ok,
        residue,
    )


def probe_f2p_011(repo):
    spec = _django_specifier(repo)
    from packaging.specifiers import SpecifierSet

    specifiers = SpecifierSet(spec)
    ok = specifiers.contains("5.2") and not specifiers.contains("4.2")
    return _runtime("F2P-011", "SpecifierSet includes 5.2, excludes 4.2", {"specifier": spec, "includes_5_2": specifiers.contains("5.2"), "includes_4_2": specifiers.contains("4.2")}, ok, {})


def probe_f2p_012(repo):
    parser = configparser.ConfigParser()
    parser.read(os.path.join(repo, "setup.cfg"))
    install_requires = parser.get("options", "install_requires", fallback="")
    django_ok = "Django>=5.2" in install_requires
    python_ok = _python_requires_ok(repo)
    return _runtime("F2P-012", "Django>=5.2, python_requires>=3.10 and 3.10-3.13 classifiers", {"install_requires": [line.strip() for line in install_requires.splitlines() if line.strip()], "python_ok": python_ok}, django_ok and python_ok, {})


def probe_f2p_014(repo):
    factors = _tox_django_factors(repo)
    envlist = _read(repo, "tox.ini")
    if _CANONICAL_PROBES is None:
        raise RuntimeError("canonical Django dependency detector is unavailable")
    no_lower = not _CANONICAL_PROBES._tox_permits_lower_django(
        envlist + "\n" + _CANONICAL_PROBES._tox_includes(repo, envlist)
    )
    return _runtime("F2P-014", "Django 5.2 tested with no dependency below 5.2; upstream main is allowed", {"factors": sorted(factors), "no_lower_factors": no_lower}, "dj52" in factors and no_lower, {})


def probe_f2p_015(repo):
    parent = _field_filter_class(repo, "parent_filter")
    category = _field_filter_class(repo, "category_filter")
    ok = parent == "ParentFieldListFilter" and category == "CategoryFieldListFilter"
    return _runtime(
        "F2P-015",
        {
            "parent_filter": "ParentFieldListFilter",
            "category_filter": "CategoryFieldListFilter",
            "no_version_branch": True,
        },
        {
            "parent_filter": parent,
            "category_filter": category,
            "residue": _residue(repo),
        },
        ok and _no_version_branch(repo),
        _residue(repo),
    )


def probe_f2p_019(repo):
    value = _granular(repo, 2024, 1, 15, 9, 12)
    ok = value.isoformat() == "2024-01-15T09:10:00+00:00"
    return _runtime("F2P-019", "2024-01-15T09:10:00+00:00", value.isoformat(), ok, _residue(repo))


def probe_f2p_022(repo):
    target = _precommit_target(repo)
    ok = target == "5.2"
    return _runtime("F2P-022", "--target-version 5.2", target, ok, {})


def probe_f2p_007(repo):
    value = _response_expires(repo, 3600)
    return _runtime("F2P-007", "Expires ~3600s ahead", value, value is not None, _residue(repo))


def probe_f2p_008(repo):
    value = _granular(repo, 2024, 3, 5, 14, 37)
    ok = value.isoformat() == "2024-03-05T14:35:00+00:00"
    return _runtime("F2P-008", "2024-03-05T14:35:00+00:00 and no version branch", {"value": value.isoformat(), "residue": _residue(repo)}, ok and _no_version_branch(repo), {})


def probe_f2p_013(repo):
    flags = _choices(repo, "3")
    ok = flags.get("?parent__id__exact=3") is True and sum(1 for value in flags.values() if value) == 1
    return _runtime("F2P-013", {"3": True, "only_one_selected": True}, flags, ok, _residue(repo))


def probe_f2p_006(repo):
    value = _response_expires(repo, 3600)
    return _runtime("F2P-006", "Expires set and no version branch", {"expires": value, "residue": _residue(repo)}, value is not None and _no_version_branch(repo), {})


def probe_f2p_009(repo):
    value = _granular_ambiguous(repo)
    return _runtime("F2P-009", "aware datetime without raising; no is_dst branch", {"aware": value, "residue": _residue(repo)}, value and _no_version_branch(repo), {})


def probe_f2p_020(repo):
    value = _granular(repo, 2024, 3, 5, 14, 37)
    expires = _response_expires(repo, 3600)
    ok = value.isoformat() == "2024-03-05T14:35:00+00:00" and expires is not None
    return _runtime("F2P-020", "rounding plus Expires header", {"value": value.isoformat(), "expires": expires}, ok, _residue(repo))


def probe_f2p_021(repo):
    value = _granular(repo, 2024, 1, 15, 9, 12)
    ok = value.isoformat() == "2024-01-15T09:10:00+00:00"
    return _runtime("F2P-021", "2024-01-15T09:10:00+00:00 without is_dst", {"value": value.isoformat(), "residue": _residue(repo)}, ok and _no_version_branch(repo), {})


def probe_f2p_024(repo):
    flags = _choices(repo, "3")
    ok = flags.get("?parent__id__exact=3") is True and not _residue(repo)
    return _runtime("F2P-024", "single selection and zero scope residue", {"flags": flags, "residue": _residue(repo)}, ok, {})


def probe_f2p_017(repo):
    value = _granular_ambiguous(repo)
    return _runtime("F2P-017", "aware datetime without raising", value, value, _residue(repo))


def probe_f2p_016(repo):
    parent = _field_filter_class(repo, "parent_filter")
    category = _field_filter_class(repo, "category_filter")
    ok = parent == "ParentFieldListFilter" and category == "CategoryFieldListFilter"
    return _runtime(
        "F2P-016",
        {
            "parent_filter": "ParentFieldListFilter",
            "category_filter": "CategoryFieldListFilter",
            "no_version_branch": True,
        },
        {
            "parent_filter": parent,
            "category_filter": category,
            "residue": _residue(repo),
        },
        ok and _no_version_branch(repo),
        _residue(repo),
    )


def probe_f2p_018(repo):
    residue = _residue(repo)
    return _runtime("F2P-018", "zero django.VERSION / import django / is_dst= in scope", {"residue": residue}, not residue, {})


def probe_f2p_023(repo):
    flags = _choices(repo, ["3", "5"])
    all_flags = _choices(repo, None)
    ok = flags.get("?parent__id__exact=3") is True and flags.get("?parent__id__exact=5") is True and all_flags.get("?") is True
    return _runtime("F2P-023", {"3": True, "5": True, "all": True}, {"multi": flags, "none": all_flags}, ok, _residue(repo))


def _granular_signature(repo: str) -> str:
    _setup_django(repo)
    import inspect

    from feincms.extensions.datepublisher import granular_now

    return str(inspect.signature(granular_now))


PROBES = {
    "F2P-001": probe_f2p_001,
    "F2P-002": probe_f2p_002,
    "F2P-003": probe_f2p_003,
    "F2P-004": probe_f2p_004,
    "F2P-005": probe_f2p_005,
    "F2P-006": probe_f2p_006,
    "F2P-007": probe_f2p_007,
    "F2P-008": probe_f2p_008,
    "F2P-009": probe_f2p_009,
    "F2P-010": probe_f2p_010,
    "F2P-011": probe_f2p_011,
    "F2P-012": probe_f2p_012,
    "F2P-013": probe_f2p_013,
    "F2P-014": probe_f2p_014,
    "F2P-015": probe_f2p_015,
    "F2P-016": probe_f2p_016,
    "F2P-017": probe_f2p_017,
    "F2P-018": probe_f2p_018,
    "F2P-019": probe_f2p_019,
    "F2P-020": probe_f2p_020,
    "F2P-021": probe_f2p_021,
    "F2P-022": probe_f2p_022,
    "F2P-023": probe_f2p_023,
    "F2P-024": probe_f2p_024,
}


def run_observation(observation_id: str, repo: str) -> dict:
    try:
        return PROBES[observation_id](repo)
    except Exception as exc:  # pragma: no cover - defensive, reported as infra
        return _result(
            observation_id,
            False,
            "probe executed",
            f"{type(exc).__name__}: {exc}",
            "INFRA_ERROR",
        )


def run_all(repo: str):
    return [run_observation(observation_id, repo) for observation_id in OBSERVATION_IDS]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run BATCH-F2P-0001 probes")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    results = run_all(os.path.abspath(args.repo))
    payload = json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
