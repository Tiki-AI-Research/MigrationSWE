#!/usr/bin/env python3
"""Self-check for the Migration verifier integrity/contract fixes (r7+).

Run it inside the verifier bundle::

    python3 /tests/verifier/selfcheck_integrity.py

``grader.py`` executes it as a fail-closed gate before scoring, so every control
below is enforced on the real graded path.

It builds throw-away repositories and asserts the tamper audit keeps rejecting
every delete/weaken form while no longer misfiring on legitimate test additions
and equivalent production-code rewrites.  The same assertions guard the three
regressions that were rejected in the r6 harbor review:

* ``tests_not_tampered`` flagged *any* test change (VB-1);
* a collected-test count could only stay exactly equal (VB-2);
* ``rebuilds_datetime`` matched one literal spelling (VB-4);
* ``python_requires`` was compared verbatim (VB-3).

The r9/r10 section guards the retained-Django-version-branch detector: the
version value must be recognised across the whole closed platform version API
(``django.VERSION`` / ``django.__version__`` / ``get_version*`` /
``importlib.metadata.version("Django")`` / distribution metadata / helpers),
from exactly one implementation shared by the L2 atom and the F2P projection.

The r11 section adds the branch *location* family (``match`` subject/guard and
a version comparison stored in a boolean first) and the constant-guard family
for the tamper audit (``if []:`` is dead code like ``if False:``), while keeping
a flow-insensitive name map from misclassifying a live ``while n`` loop.

Exit code 0 means every control behaved as expected.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import runpy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_SELF = Path(__file__).resolve()
_BUNDLE = _SELF.parent.parent
sys.path.insert(0, str(_SELF.parent))

import residue  # noqa: E402
from differential import compare as compare_projection  # noqa: E402
from evidence import _has_version_branch, _test_integrity, build_source_test_baseline  # noqa: E402


def _load_static_probes():
    path = _BUNDLE / "assets" / "shared" / "integration" / "static_probes.py"
    spec = importlib.util.spec_from_file_location("selfcheck_static_probes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STATIC_PROBES = _load_static_probes()
CONFIG = {"policy": {}}

_SAMPLE = '''\
from datetime import datetime
from django.test import TestCase
from feincms.extensions.datepublisher import granular_now


class SampleTest(TestCase):
    def test_rounding(self):
        d = datetime(2016, 1, 3, 1, 13)
        g = granular_now(d)
        self.assertEqual(d.hour, g.hour)
        self.assertEqual(10, g.minute)
'''


def _write(root: Path, name: str, source: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _baseline_for(source: str) -> tuple[dict, str]:
    root = Path(tempfile.mkdtemp(prefix="selfcheck-base-"))
    _write(root, "tests/app/tests/test_sample.py", source)
    return build_source_test_baseline(root), "tests/app/tests/test_sample.py"


def _tamper(source: str, baseline: dict, relative: str) -> str:
    root = Path(tempfile.mkdtemp(prefix="selfcheck-cand-"))
    _write(root, relative, source)
    return str(
        _test_integrity(root, baseline, CONFIG, source="target-test-baseline")[0]["status"]
    )


def _expect(label: str, got: object, want: object, failures: list[str]) -> None:
    passed = got == want
    print(f"{'OK ' if passed else 'BAD'} {label}: got={got!r} want={want!r}")
    if not passed:
        failures.append(label)


def _asset_comment_checks(failures):
    """All scoring routes must accept exactly the two published comment edits."""

    batches = _BUNDLE / "assets" / "shared" / "batches"
    contract = runpy.run_path(str(batches / "BATCH-CONTRACT-0001" / "contract_harness.py"))
    p2p = runpy.run_path(str(batches / "BATCH-P2P-0001" / "probe.py"))
    previous_harness = sys.modules.pop("harness", None)
    sys.path.insert(0, str(batches / "BATCH-DIFFERENTIAL-0001"))
    try:
        differential = runpy.run_path(str(batches / "BATCH-DIFFERENTIAL-0001" / "probes.py"))
    finally:
        sys.path.pop(0)
        sys.modules.pop("harness", None)
        if previous_harness is not None:
            sys.modules["harness"] = previous_harness
    routes = {
        "integration": lambda root: STATIC_PROBES._surface_asset_digest(str(root)),
        "contract": lambda root: contract["probe_013"](str(root)),
        "differential": lambda root: differential["_diff_013"](str(root)),
        "p2p": lambda root: p2p["probe_template_asset_digest"](SimpleNamespace(repo_root=str(root))),
    }
    with tempfile.TemporaryDirectory(prefix="selfcheck-asset-comments-") as tmp:
        root = Path(tmp)
        originals = {}
        for relative, (before, after) in STATIC_PROBES.DOCUMENTATION_ASSET_EDITS.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[relative] = b"const active = true;\n" + before + b"active && run();\n"
            path.write_bytes(originals[relative])
        _write(root, "feincms/templates/unchanged.html", "<div>unchanged</div>\n")
        _write(root, "feincms/static/unchanged.css", "a { color: red }\n")
        baseline = {name: function(root) for name, function in routes.items()}
        _expect("old comment references still detected", len(STATIC_PROBES._legacy_repository_references(str(root))), 2, failures)
        for relative, (before, after) in STATIC_PROBES.DOCUMENTATION_ASSET_EDITS.items():
            (root / relative).write_bytes(originals[relative].replace(before, after))
        corrected = {relative: (root / relative).read_bytes() for relative in originals}
        _expect("corrected comments contain no legacy references", STATIC_PROBES._legacy_repository_references(str(root)), [], failures)
        for name, function in routes.items():
            _expect(f"{name}: documented comment edits accepted", function(root), baseline[name], failures)

        mutations = {
            "executable JavaScript": lambda data: data.replace(b"active = true", b"active = false"),
            "runtime string": lambda data: data + b'const message = "changed";\n',
            "unapproved comment": lambda data: data + b"// unrelated edit\n",
            "code after approved comment": lambda data: data.replace(b"mechanism\n", b"mechanism;\nrun();\n"),
            "template literal wrapping": lambda data: b"const text = `\n" + data + b"`;\n",
        }
        relative = "feincms/static/feincms/item_editor.js"
        for label, mutate in mutations.items():
            (root / relative).write_bytes(mutate(corrected[relative]))
            for name, function in routes.items():
                _expect(f"{name}: reject {label}", function(root) == baseline[name], False, failures)
            (root / relative).write_bytes(corrected[relative])
        for protected in ("feincms/templates/unchanged.html", "feincms/static/unchanged.css"):
            path = root / protected
            content = path.read_bytes()
            path.write_bytes(content + b"unrelated\n")
            for name, function in routes.items():
                _expect(f"{name}: reject {protected} edit", function(root) == baseline[name], False, failures)
            path.write_bytes(content)
        path = root / relative
        path.unlink()
        for name, function in routes.items():
            _expect(f"{name}: reject deleted asset", function(root) == baseline[name], False, failures)
        path.write_bytes(corrected[relative])
        added = root / "feincms/static/extra.js"
        added.write_bytes(b"run();\n")
        for name, function in routes.items():
            _expect(f"{name}: reject added asset", function(root) == baseline[name], False, failures)


def main() -> int:
    failures: list[str] = []
    _asset_comment_checks(failures)
    baseline, relative = _baseline_for(_SAMPLE)

    _expect("unchanged file", _tamper(_SAMPLE, baseline, relative), "PASS", failures)
    _expect(
        "added assertion",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        self.assertEqual(10, g.minute)\n        self.assertTrue(g.minute % 5 == 0)",
            ),
            baseline,
            relative,
        ),
        "PASS",
        failures,
    )
    _expect(
        "strengthened loop rewrite",
        _tamper(
            _SAMPLE.replace(
                "        d = datetime(2016, 1, 3, 1, 13)\n"
                "        g = granular_now(d)\n"
                "        self.assertEqual(d.hour, g.hour)\n"
                "        self.assertEqual(10, g.minute)",
                "        for minute in (0, 4, 5, 13, 59):\n"
                "            g = granular_now(datetime(2016, 1, 3, 1, minute))\n"
                "            self.assertEqual(g.hour, 1)\n"
                "            self.assertEqual(g.minute, minute // 5 * 5)\n"
                "            self.assertEqual(g.second, 0)",
            ),
            baseline,
            relative,
        ),
        "PASS",
        failures,
    )
    _expect(
        "strengthened tuple rewrite",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(d.hour, g.hour)\n"
                "        self.assertEqual(10, g.minute)",
                "        self.assertEqual(\n"
                "            (g.year, g.month, g.day, g.hour, g.minute, g.second),\n"
                "            (d.year, d.month, d.day, d.hour, 10, 0),\n"
                "        )\n"
                "        self.assertIsNotNone(g.utcoffset())",
            ),
            baseline,
            relative,
        ),
        "PASS",
        failures,
    )
    _expect(
        "deleted assertion",
        _tamper(
            _SAMPLE.replace("        self.assertEqual(10, g.minute)\n", ""),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    _expect(
        "commented assertion",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        # self.assertEqual(10, g.minute)",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    _expect(
        "weakened assertion (same count)",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        self.assertNotNull(g.minute)",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    _expect(
        "replaced expected value (same count)",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        self.assertEqual(11, g.minute)",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    _expect(
        "deleted test function",
        _tamper(
            _SAMPLE.replace(
                "    def test_rounding(self):\n"
                "        d = datetime(2016, 1, 3, 1, 13)\n"
                "        g = granular_now(d)\n"
                "        self.assertEqual(d.hour, g.hour)\n"
                "        self.assertEqual(10, g.minute)\n",
                "",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    _expect(
        "assertion made unreachable",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        if False:\n            self.assertEqual(10, g.minute)",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    # r11: an always-false guard is dead code whatever its spelling, so wrapping
    # the assertion in ``if []:`` (or any equivalent constant guard) must be
    # caught exactly like ``if False:``.
    for label, guard in {
        "empty-list": "if []:",
        "empty-tuple": "if ():",
        "empty-dict": "if {}:",
        "empty-set-call": "if set():",
        "empty-frozenset-call": "if frozenset():",
        "negated-true": "if not True:",
        "len-of-empty": "if len(''):",
        "constant-compare": "if 0 == 1:",
        "empty-string": "if '':",
    }.items():
        _expect(
            f"assertion made unreachable ({label} guard)",
            _tamper(
                _SAMPLE.replace(
                    "        self.assertEqual(10, g.minute)",
                    f"        {guard}\n            self.assertEqual(10, g.minute)",
                ),
                baseline,
                relative,
            ),
            "FAIL",
            failures,
        )
    _expect(
        "assertion made unreachable (while empty guard)",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        while []:\n            self.assertEqual(10, g.minute)",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    _expect(
        "assertion made unreachable (name-bound empty guard)",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        _never = []\n        if _never:\n            self.assertEqual(10, g.minute)",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    _expect(
        "assertion made unreachable (name-bound empty-set guard)",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        _never = frozenset()\n        if _never:\n            self.assertEqual(10, g.minute)",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )
    # A guard that is not statically constant must keep the assertion live: a
    # flow-insensitive detector that ignores reassignment would wrongly call the
    # ``while n`` body dead.
    _expect(
        "live loop guard keeps assertion",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        n = 1\n        while n:\n            n = 0\n            self.assertEqual(10, g.minute)",
            ),
            baseline,
            relative,
        ),
        "PASS",
        failures,
    )
    _expect(
        "live dynamic guard keeps assertion",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        if g:\n            self.assertEqual(10, g.minute)",
            ),
            baseline,
            relative,
        ),
        "PASS",
        failures,
    )
    _expect(
        "assertion replaced by tautology",
        _tamper(
            _SAMPLE.replace(
                "        self.assertEqual(10, g.minute)",
                "        self.assertTrue(True)",
            ),
            baseline,
            relative,
        ),
        "FAIL",
        failures,
    )

    policy = {"monotonic_nondecreasing_keys": ["collected"]}
    _expect(
        "collected grows",
        compare_projection(
            {"collected": 61, "coverage_configured": True},
            {"collected": 65, "coverage_configured": True},
            policy,
        )["status"],
        "PASS",
        failures,
    )
    _expect(
        "collected shrinks",
        compare_projection(
            {"collected": 61, "coverage_configured": True},
            {"collected": 40, "coverage_configured": True},
            policy,
        )["status"],
        "FAIL",
        failures,
    )
    _expect(
        "coverage weakened while collected grows",
        compare_projection(
            {"collected": 61, "coverage_configured": True},
            {"collected": 65, "coverage_configured": False},
            policy,
        )["status"],
        "FAIL",
        failures,
    )

    root = Path(tempfile.mkdtemp(prefix="selfcheck-probe-"))
    datepublisher = root / "feincms" / "extensions" / "datepublisher.py"
    datepublisher.parent.mkdir(parents=True, exist_ok=True)
    shared = (
        "def granular_now(n=None, default_tz=None):\n"
        "    rounded_minute = (n.minute // 5) * 5\n"
        "    d = %s\n"
        "    return timezone.make_aware(d, default_tz)\n"
    )
    datepublisher.write_text(
        shared % "datetime(n.year, n.month, n.day, n.hour, rounded_minute)",
        encoding="utf-8",
    )
    _expect(
        "granular rebuild literal",
        STATIC_PROBES._surface_granular_rounding(str(root))["rebuilds_datetime"],
        True,
        failures,
    )
    datepublisher.write_text(
        shared % "n.replace(minute=rounded_minute, second=0, microsecond=0)",
        encoding="utf-8",
    )
    _expect(
        "granular rebuild via replace",
        STATIC_PROBES._surface_granular_rounding(str(root))["rebuilds_datetime"],
        True,
        failures,
    )
    datepublisher.write_text(
        "def granular_now(n=None, default_tz=None):\n    return timezone.now()\n",
        encoding="utf-8",
    )
    _expect(
        "granular unrelated implementation",
        STATIC_PROBES._surface_granular_rounding(str(root))["rebuilds_datetime"],
        False,
        failures,
    )

    _expect(
        "python_requires floor only",
        STATIC_PROBES._canonical_python_requires(">=3.10"),
        ">=3.10",
        failures,
    )
    _expect(
        "python_requires bounded range",
        STATIC_PROBES._canonical_python_requires(">=3.10,<3.14"),
        ">=3.10",
        failures,
    )
    _expect(
        "python_requires raised floor",
        STATIC_PROBES._canonical_python_requires(">=3.11"),
        ">=3.11",
        failures,
    )
    _expect(
        "python_requires narrowed range",
        STATIC_PROBES._canonical_python_requires(">=3.10,<3.12"),
        ">=3.10,<3.12",
        failures,
    )

    # r9: the Django version-branch (L2 deceptive fallback) detector must
    # resolve module identity / attribute names *by value* -- an aliased ``sys``
    # module, a concatenated/joined/percent/format-built name, an aliased
    # ``importlib`` argument or an ``from sys import modules`` binding must all
    # be caught, identically in the L2 atom and the F2P projection.
    version_cases = {
        "sys alias module": (
            "import sys as _qa_sys\n"
            "if _qa_sys.modules['django'].VERSION[0] < 5:\n    x = 1\n"
        ),
        "concat key": (
            "import sys\n_qa_dj = 'dj' + 'ango'\n"
            "if sys.modules[_qa_dj].VERSION[0] < 5:\n    x = 1\n"
        ),
        "join-built attribute": (
            "import sys as _qa_sys\n"
            "def _legacy():\n"
            "    _m = _qa_sys.modules.get('django')\n"
            "    _a = ''.join(('VER', 'SION'))\n"
            "    return getattr(_m, _a)[0] < 5\n"
        ),
        "importlib alias argument": (
            "import importlib as _il\n"
            "if _il.import_module('dj' + 'ango').VERSION[0] < 5:\n    x = 1\n"
        ),
        "from sys import modules": (
            "from sys import modules as _m\n"
            "if _m['django'].VERSION[0] < 5:\n    x = 1\n"
        ),
        "f-string key": (
            "import sys\n_k = f'dj{\"ango\"}'\n"
            "if sys.modules[_k].VERSION[0] < 5:\n    x = 1\n"
        ),
        "percent template key": (
            "import sys\n_k = '%s%s' % ('dj', 'ango')\n"
            "if sys.modules[_k].VERSION[0] < 5:\n    x = 1\n"
        ),
        "getattr-built sys.modules": (
            "import sys as _s\n"
            "if getattr(_s, 'mod' + 'ules')['django'].VERSION[0] < 5:\n    x = 1\n"
        ),
        "vars(sys)['modules']": (
            "import sys as _s\n"
            "if vars(_s)['modules']['django'].VERSION[0] < 5:\n    x = 1\n"
        ),
        "format-built attribute": (
            "import sys as _s\n"
            "if getattr(_s.modules['django'], '{}'.format('VERSION'))[0] < 5:\n"
            "    x = 1\n"
        ),
        "stored attrgetter accessor": (
            "from operator import attrgetter\n"
            "import sys as _s\n"
            "_get = attrgetter(''.join(('VER', 'SION')))\n"
            "if _get(_s.modules.get('django'))[0] < 5:\n    x = 1\n"
        ),
        # r10: the *version value* is recognised across the whole closed
        # platform version API by value, so re-spelling where the number comes
        # from (``__version__`` / metadata / distribution / helper) cannot hide
        # a retained pre-5.2 branch.
        "dunder version import": (
            "from django import __version__\n"
            "_MAJOR = tuple(int(p) for p in __version__.split('.')[:2])\n"
            "if _MAJOR < (5, 0):\n    x = 1\n"
        ),
        "importlib.metadata version": (
            "import importlib.metadata as _m\n"
            "if tuple(int(p) for p in _m.version('Django').split('.')[:2]) < (5, 0):\n"
            "    x = 1\n"
        ),
        "from importlib.metadata import version": (
            "from importlib.metadata import version as _v\n"
            "if _v('django')[0] == '3':\n    x = 1\n"
        ),
        "importlib.metadata distribution version": (
            "from importlib import metadata\n"
            "if metadata.distribution('Django').version < '5.2':\n    x = 1\n"
        ),
        "distribution metadata key": (
            "import importlib.metadata as _m\n"
            "if _m.metadata('Django')['Version'] != '5.2':\n    x = 1\n"
        ),
        "pkg_resources distribution version": (
            "import pkg_resources\n"
            "if pkg_resources.get_distribution('django').version[0] == '3':\n"
            "    x = 1\n"
        ),
        "getattr dunder version": (
            "import django\nif getattr(django, '__version__')[0] == '3':\n    x = 1\n"
        ),
        "get_version_tuple": (
            "import django\nif django.get_version_tuple() < (5,):\n    x = 1\n"
        ),
        "helper returns raw version": (
            "import django\n"
            "def _major():\n    return django.VERSION[0]\n"
            "if _major() < 5:\n    x = 1\n"
        ),
        "helper compares module parameter": (
            "import sys\n"
            "def _pre5(_m):\n    return _m.VERSION[0] < 5\n"
            "if _pre5(sys.modules['django']):\n    x = 1\n"
        ),
        "lambda returns raw version": (
            "import django\n"
            "_major = lambda: int(django.VERSION[0])\n"
            "if _major() < 5:\n    x = 1\n"
        ),
        "stored metadata version then branch": (
            "import importlib.metadata as _m\n"
            "_DJ = _m.version('Django')\n"
            "if _DJ.split('.')[0] < '5':\n    x = 1\n"
        ),
        # r11: the branch *location* is recognised, not just a direct
        # ``if <version> < ...``: the gate may sit in a ``match`` subject/guard
        # or in a boolean computed first (``operator.lt`` -> stored name).
        "match version subject": (
            "match __import__('django').VERSION:\n"
            "    case (major, *_) if major < 5:\n        x = 1\n"
        ),
        "match version capture": (
            "import django\n_v = django.VERSION\n"
            "match _v:\n"
            "    case (major, *_) if major < 5:\n        x = 1\n"
        ),
        "match guard reads version": (
            "import django\nmatch object():\n"
            "    case _ if django.VERSION[0] < 5:\n        x = 1\n"
        ),
        "operator.lt stored boolean": (
            "import operator\n"
            "_LEGACY_LOOKUP = operator.lt(__import__('django').VERSION, (5,))\n"
            "if _LEGACY_LOOKUP:\n    x = 1\n"
        ),
        "comparison stored boolean": (
            "import django\n_L = (django.VERSION < (5,))\n"
            "if _L:\n    x = 1\n"
        ),
        "functools partial comparison": (
            "import functools, operator\n"
            "_legacy = functools.partial(operator.lt, __import__('django').VERSION)\n"
            "if _legacy((5,)):\n    x = 1\n"
        ),
        "comprehension version guard": (
            "import django\n"
            "_xs = [i for i in range(3) if django.VERSION[0] < 5]\n"
        ),
    }
    for label, source in version_cases.items():
        _expect(
            f"version branch detected by evidence ({label})",
            _has_version_branch(ast.parse(source)),
            True,
            failures,
        )
        _expect(
            f"version branch detected by probes ({label})",
            STATIC_PROBES._version_branch(source),
            True,
            failures,
        )
    # The L2 atom and the F2P projection are the *same* function: the detector
    # lives in exactly one place (static_probes.py), so the two can never drift.
    _expect(
        "evidence reuses the single canonical version-branch detector",
        getattr(_has_version_branch, "__code__", None) is not None
        and _has_version_branch.__code__.co_filename.endswith("static_probes.py"),
        True,
        failures,
    )
    legal_source = (
        "from django.db import models\n"
        "from django.contrib.admin.filters import ChoicesFieldListFilter\n"
        "LABEL = 'django'\n"
    )
    _expect(
        "no false positive on legal production source",
        _has_version_branch(ast.parse(legal_source)),
        False,
        failures,
    )
    _expect(
        "no false positive on legal production source (probes)",
        STATIC_PROBES._version_branch(legal_source),
        False,
        failures,
    )
    unrelated_distribution = (
        "import importlib.metadata as _m\n"
        "if _m.version('someotherpackage') < '5':\n    x = 1\n"
    )
    _expect(
        "no false positive on an unrelated distribution version",
        _has_version_branch(ast.parse(unrelated_distribution)),
        False,
        failures,
    )
    non_branch_version_use = (
        "import django\nlogger.info('%s', django.__version__)\n"
    )
    _expect(
        "reading the version outside a branch is not a fallback",
        _has_version_branch(ast.parse(non_branch_version_use)),
        False,
        failures,
    )

    # r9: the declared Django floor is a specifier, not a byte string: the
    # whitespace form ``Django >= 5.2`` must compare equal to ``Django>=5.2``.
    floor_root = Path(tempfile.mkdtemp(prefix="selfcheck-floor-"))
    (floor_root / "setup.cfg").write_text(
        "install_requires =\n    Django >= 5.2\n    Pillow>=2.0.0\n",
        encoding="utf-8",
    )
    (floor_root / "tox.ini").write_text(
        "[tox]\ndeps =\n    dj52: Django >= 5.2, <5.3\n", encoding="utf-8"
    )
    (floor_root / ".pre-commit-config.yaml").write_text(
        '- id: django-upgrade\n  args: [--target-version, "5.2"]\n', encoding="utf-8"
    )
    floor = STATIC_PROBES._target_technology(str(floor_root))
    _expect(
        "declared django floor tolerates whitespace",
        floor["declared_django_floor"],
        "5.2",
        failures,
    )
    _expect(
        "tox dj52 constraint tolerates whitespace",
        floor["tox_targets_52"],
        True,
        failures,
    )
    _expect("legit spaced floor applies", floor["applied"], True, failures)

    # Preserved assets remain subject to the repository-wide old-reference ban.
    # The two published comment edits affect preservation hashes only: neither
    # retained legacy references nor changes elsewhere are exempt from checks.
    for relative, legacy in (
        ("feincms/static/feincms/tree_editor.js", "// https://docs.djangoproject.com/en/1.4/ref/contrib/csrf/\n"),
        ("feincms/static/feincms/item_editor.js", "// Built-in mechanism (Django 1.2+)\n"),
        ("feincms/templates/example.html", "<!-- Django 3.2 -->\n"),
        ("feincms/static/example.css", "/* Django 4.2 */\n"),
    ):
        asset = floor_root / relative
        _write(floor_root, relative, legacy)
        projection = STATIC_PROBES._target_technology(str(floor_root))
        _expect(f"old asset reference rejected: {relative}", projection["applied"], False, failures)
        _expect(
            f"asset reference has path and line: {relative}",
            [(hit["path"], hit["line"]) for hit in projection["legacy_repository_references"]],
            [(relative, 1)],
            failures,
        )
        asset.write_text(legacy.replace("1.4", "5.2").replace("1.2", "5.2").replace("3.2", "5.2").replace("4.2", "5.2"), encoding="utf-8")
        _expect(
            f"target asset reference accepted: {relative}",
            STATIC_PROBES._target_technology(str(floor_root))["applied"],
            True,
            failures,
        )
        asset.unlink()

    residue_root = Path(tempfile.mkdtemp(prefix="selfcheck-residue-"))
    (residue_root / "setup.cfg").write_text(
        "install_requires =\n    Django >= 3.2\n", encoding="utf-8"
    )
    residue_hits = residue.scan_residue_rules(
        residue_root,
        [
            {
                "id": "BATCH-DIFFERENTIAL-0001-RULE-004",
                "kind": "literal",
                "paths": ["setup.cfg"],
                "target": "Django>=3.2",
            }
        ],
    )
    _expect(
        "residue rule tolerates spaced residual floor",
        bool(residue_hits),
        True,
        failures,
    )

    if failures:
        print(f"\nSELFCHECK FAILED: {len(failures)} control(s): {failures}")
        return 1
    print("\nSELFCHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
