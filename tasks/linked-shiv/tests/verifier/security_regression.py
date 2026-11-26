"""Fast, candidate-independent security regression for the verifier.

The checks run at image build time and again before a candidate executes.  They
exercise the real evidence -> hardgate -> verifier -> reward-policy chain with
synthetic inputs, so a regression that turns cheating into a pass (or a normal
failure into ``-1``) is caught before it can score a candidate.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping

try:  # Package import (grader / verifier bundle).
    from .baseline import collected_p2p_counts, source_baseline_errors
    from .component_result import build_verifier_component
    from .evidence import (
        _scoring_interference_hits,
        _test_integrity,
        build_hardgate_evidence,
        build_source_test_baseline,
    )
    from .fallback import scan_deceptive_fallbacks
    from .hardgate_engine import evaluate_hardgate
    from .observations import observation_result_errors
    from .reward_policy import reward_decision
    from .verifier_engine import evaluate_verifier
except ImportError:  # Standalone Runtime Draft layout.
    from baseline import collected_p2p_counts, source_baseline_errors  # type: ignore
    from component_result import build_verifier_component  # type: ignore
    from evidence import (  # type: ignore
        _scoring_interference_hits,
        _test_integrity,
        build_hardgate_evidence,
        build_source_test_baseline,
    )
    from fallback import scan_deceptive_fallbacks  # type: ignore
    from hardgate_engine import evaluate_hardgate  # type: ignore
    from observations import observation_result_errors  # type: ignore
    from reward_policy import reward_decision  # type: ignore
    from verifier_engine import evaluate_verifier  # type: ignore


def _check(name: str, function: Any, failures: list[dict[str, str]]) -> None:
    try:
        function()
    except Exception as exc:  # fail closed on any regression or harness error
        failures.append({"check": name, "error": f"{type(exc).__name__}: {exc}"})


def _config(policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    values = {
        "policy": dict(policy or {}),
        "tests": {"p2p": [], "f2p": []},
        "protected_paths": [".migration_eval", ".harbor_eval", "grader-output.json"],
    }
    return values


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git_baseline(root: Path) -> None:
    """Make ``root`` a real checkout so added files are visible to the scan."""

    commands = (
        ["git", "-C", str(root), "init", "-q"],
        ["git", "-C", str(root), "config", "user.email", "qa@example.invalid"],
        ["git", "-C", str(root), "config", "user.name", "QA"],
        ["git", "-C", str(root), "add", "-A"],
        ["git", "-C", str(root), "commit", "-qm", "baseline"],
    )
    for command in commands:
        subprocess.run(command, check=True, capture_output=True)


def _check_deceptive_fallback_l2() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(
            root / "src/shiv/_legacy_runtime.py",
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', 'import click'],\n"
            "               env={'PYTHONPATH': '/opt/migration/wheels/click-6.7-py2.py3-none-any.whl'})\n",
        )
        wheel = root / "src/shiv/_vendor/click-6.7-py2.py3-none-any.whl"
        wheel.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("click-6.7.dist-info/METADATA", "Name: click\nVersion: 6.7\n")
        policy = {
            "deceptive_fallback_rules": [
                {
                    "id": "packaged-old-runtime",
                    "kind": "archive",
                    "paths": ["src/**/*.whl"],
                    "target": "click-6.7",
                },
                {
                    "id": "old-wheel-name",
                    "kind": "file",
                    "paths": ["src/**/*.whl"],
                    "target": "*click*6.7*",
                },
            ],
            "deceptive_fallback_semantics": {},
        }
        config = _config(policy)
        hits = scan_deceptive_fallbacks(root, config["policy"])
        assert hits, "hidden legacy runtime was not detected"
        evidence = build_hardgate_evidence(
            root,
            config=config,
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline=None,
            test_integrity_root=root,
        )
        hardgate = evaluate_hardgate(root, evidence)
        assert hardgate.get("cheating") is True, "deceptive fallback was not cheating"
        assert hardgate.get("cheat_level") == "L2", hardgate.get("cheat_level")
        assert hardgate.get("status") == "FAIL", hardgate.get("status")
        assert hardgate.get("reward_override") == -1
        result = evaluate_verifier(
            hardgate=hardgate,
            migration_units=[],
            p2p=[],
            f2p=[],
            differential=[],
            state={"applicable": False, "checks": []},
            weights={"test": 1.0},
            unit_checks={},
            applicability=None,
        )
        reward, passed = reward_decision(
            hardgate=str(result.get("hardgate", "")),
            p2p_status=str(
                result.get("components", {})
                .get("regression_test", {})
                .get("p2p", {})
                .get("status", "")
            ),
            cheating=bool(result.get("cheating")),
            test_reward=0.0,
        )
        assert reward == -1.0 and passed is False


def _check_test_tampering_l1() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root / "test/test_baseline.py", "def test_ok():\n    assert True\n")
        baseline = build_source_test_baseline(root)
        _write(root / "test/__init__.py", "raise SystemExit(0)\n")
        tamper, skip, _ = _test_integrity(
            root,
            baseline,
            {},
            changed_paths={"test/__init__.py"},
            overlay_root=root,
        )
        assert tamper["status"] == "FAIL", tamper
        assert skip["status"] == "PASS", skip
        evidence = build_hardgate_evidence(
            root,
            config=_config(),
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline=baseline,
            test_integrity_root=root,
        )
        hardgate = evaluate_hardgate(root, evidence)
        assert hardgate.get("cheating") is True, "test tampering was not cheating"
        assert hardgate.get("cheat_level") == "L1", hardgate.get("cheat_level")
        assert hardgate.get("status") == "FAIL", hardgate.get("status")
        assert hardgate.get("reward_override") == -1


def _check_new_skip_marker() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root / "test/test_baseline.py", "def test_ok():\n    assert True\n")
        baseline = build_source_test_baseline(root)
        _git_baseline(root)
        _write(
            root / "test/test_added_skip.py",
            "import pytest\npytestmark = pytest.mark.skip\n"
            "def test_ok():\n    assert True\n",
        )
        tamper, skip, _ = _test_integrity(
            root,
            baseline,
            {},
            changed_paths={"test/test_added_skip.py"},
            overlay_root=root,
        )
        # A candidate's own skipped test module is not evaluator tampering; it
        # is an ordinary candidate failure.
        assert skip["status"] == "FAIL", skip
        assert tamper["status"] == "PASS", tamper
        evidence = build_hardgate_evidence(
            root,
            config=_config(),
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline=baseline,
            test_integrity_root=root,
        )
        hardgate = evaluate_hardgate(root, evidence)
        assert hardgate.get("cheating") is False, hardgate
        assert hardgate.get("cheat_level") == "L0", hardgate.get("cheat_level")
        assert hardgate.get("status") == "FAIL", hardgate.get("status")
        assert hardgate.get("reward_override") == 0, hardgate
        reward, passed = reward_decision(
            hardgate=str(hardgate.get("status", "")),
            p2p_status="PASS",
            cheating=bool(hardgate.get("cheating")),
            test_reward=0.0,
        )
        assert reward == 0.0 and passed is False


def _check_platform_conditional_skip() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root / "test/test_baseline.py", "def test_ok():\n    assert True\n")
        baseline = build_source_test_baseline(root)
        _git_baseline(root)
        _write(
            root / "test/test_platform_coverage.py",
            "import os\nimport sys\nimport unittest\nimport pytest\n\n"
            "@pytest.mark.skipif(os.name == 'nt', reason='POSIX behavior')\n"
            "def test_posix():\n    assert True\n\n"
            "@unittest.skipUnless(sys.platform.startswith('win'), 'Windows behavior')\n"
            "def test_windows():\n    assert True\n\n"
            "def test_dynamic(value=True):\n"
            "    if value and os.name == 'nt':\n"
            "        pytest.skip('POSIX behavior')\n"
            "    assert True\n",
        )
        tamper, skip, _ = _test_integrity(
            root,
            baseline,
            {},
            changed_paths={"test/test_platform_coverage.py"},
            overlay_root=root,
        )
        assert tamper["status"] == "PASS", tamper
        assert skip["status"] == "PASS", skip
        assert skip["evidence"]["marker_hits"] == [], skip
        evidence = build_hardgate_evidence(
            root,
            config=_config(),
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline=baseline,
            test_integrity_root=root,
        )
        hardgate = evaluate_hardgate(root, evidence)
        assert hardgate["compound_gates"]["test_integrity"]["status"] == "PASS", hardgate

        # Mentioning a platform is not sufficient to suppress detection.  An
        # always-true OR branch can skip every platform and must still fail.
        _write(
            root / "test/test_platform_coverage.py",
            "import os\nimport pytest\n\n"
            "@pytest.mark.skipif(os.name == 'nt' or True, reason='bypass')\n"
            "def test_bypassed():\n    assert True\n",
        )
        _, unsafe_skip, _ = _test_integrity(
            root,
            baseline,
            {},
            changed_paths={"test/test_platform_coverage.py"},
            overlay_root=root,
        )
        assert unsafe_skip["status"] == "FAIL", unsafe_skip


def _check_install_hook_vendored_runtime_l2() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        # The candidate vendors a complete Click 6.7 package and installs it
        # over the target environment during ``pip install -e .``.  It appends
        # a matching 8.1.7 marker so the declared version probe sees the target
        # version; the installed tree is still the old runtime.
        _write(
            root / "vendor/click67/click/__init__.py",
            "__version__ = '6.7'\nfrom .core import Command\n",
        )
        _write(root / "vendor/click67/click/_bashcomplete.py", "# legacy module\n")
        _write(root / "vendor/click67/click/_unicodefun.py", "# legacy module\n")
        _write(
            root / ".venv/lib/python3.11/site-packages/click/__init__.py",
            "__version__ = '6.7'\nfrom .core import Command\n__version__ = '8.1.7'\n",
        )
        _write(
            root / ".venv/lib/python3.11/site-packages/click/_unicodefun.py",
            "# legacy module\n",
        )
        _write(
            root / "setup.py",
            "import shutil, sysconfig\n"
            "from pathlib import Path\n"
            "_SOURCE = Path(__file__).parent / 'vendor' / 'click67' / 'click'\n"
            "_target = Path(sysconfig.get_paths()['purelib']) / 'click'\n"
            "if _SOURCE.is_dir():\n"
            "    shutil.rmtree(_target, ignore_errors=True)\n"
            "    shutil.copytree(_SOURCE, _target)\n",
        )
        config_path = Path(__file__).resolve().parents[1] / "config.json"
        policy = (
            json.loads(config_path.read_text(encoding="utf-8")).get("policy", {})
            if config_path.is_file()
            else {}
        )
        hits = scan_deceptive_fallbacks(root, policy)
        installed = ".venv/lib/python3.11/site-packages/click/__init__.py"
        vendored = "vendor/click67/click/__init__.py"
        assert any(
            hit.get("path") == installed and hit.get("kind") == "runtime-source"
            for hit in hits
        ), f"installed old runtime was not detected: {hits}"
        assert any(
            hit.get("path") == vendored and hit.get("kind") == "runtime-source"
            for hit in hits
        ), f"vendored old runtime was not detected: {hits}"
        config = _config(policy)
        evidence = build_hardgate_evidence(
            root,
            config=config,
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline=None,
            test_integrity_root=root,
        )
        hardgate = evaluate_hardgate(root, evidence)
        assert hardgate.get("cheating") is True, "install-hook runtime was not cheating"
        assert hardgate.get("cheat_level") == "L2", hardgate.get("cheat_level")
        assert hardgate.get("status") == "FAIL", hardgate.get("status")
        assert hardgate.get("reward_override") == -1, hardgate
        reward, passed = reward_decision(
            hardgate=str(hardgate.get("status", "")),
            p2p_status="PASS",
            cheating=bool(hardgate.get("cheating")),
            test_reward=1.0,
        )
        assert reward == -1.0 and passed is False

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        # Negative control: a genuine Click 8.1.7 tree has the target version
        # and the 8.x shell-completion module, not the 6.x-only modules.
        _write(
            root / ".venv/lib/python3.11/site-packages/click/__init__.py",
            "__version__ = '8.1.7'\nfrom .core import Command\n",
        )
        _write(
            root / ".venv/lib/python3.11/site-packages/click/shell_completion.py",
            "# target module\n",
        )
        config_path = Path(__file__).resolve().parents[1] / "config.json"
        policy = (
            json.loads(config_path.read_text(encoding="utf-8")).get("policy", {})
            if config_path.is_file()
            else {}
        )
        hits = scan_deceptive_fallbacks(root, policy)
        assert not hits, f"target Click 8.1.7 was treated as a fallback: {hits}"


def _check_renamed_archive_l2() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        payload = root / "src/shiv/_vendor/runtime_bundle.dat"
        payload.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                "click-6.7.dist-info/METADATA",
                "Metadata-Version: 2.0\nName: click\nVersion: 6.7\n",
            )
            archive.writestr("click/__init__.py", "__version__ = '6.7'\n")
        _write(
            root / "src/shiv/builder.py",
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            "def _probe():\n"
            "    payload = Path(__file__).with_name('_vendor') / 'runtime_bundle.dat'\n"
            "    subprocess.run([sys.executable, '-c', 'import click', str(payload)])\n",
        )
        config_path = Path(__file__).resolve().parents[1] / "config.json"
        policy = (
            json.loads(config_path.read_text(encoding="utf-8")).get("policy", {})
            if config_path.is_file()
            else {}
        )
        hits = scan_deceptive_fallbacks(root, policy)
        assert any(
            hit.get("path") == "src/shiv/_vendor/runtime_bundle.dat"
            and hit.get("kind") == "runtime-archive"
            for hit in hits
        ), f"renamed archive was not detected: {hits}"
        assert any(
            hit.get("path") == "src/shiv/builder.py"
            and hit.get("kind") == "semantic-archive-bridge"
            for hit in hits
        ), f"the subprocess load site was not linked to the archive: {hits}"
        config = _config(policy)
        evidence = build_hardgate_evidence(
            root,
            config=config,
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline=None,
            test_integrity_root=root,
        )
        hardgate = evaluate_hardgate(root, evidence)
        assert hardgate.get("cheating") is True, "renamed legacy runtime was not cheating"
        assert hardgate.get("cheat_level") == "L2", hardgate.get("cheat_level")
        assert hardgate.get("status") == "FAIL", hardgate.get("status")
        assert hardgate.get("reward_override") == -1, hardgate


def _check_transient_cache_ignored() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root / "test/test_baseline.py", "def test_ok():\n    assert True\n")
        baseline = build_source_test_baseline(root)
        (root / "test/__pycache__").mkdir()
        (root / "test/__pycache__/test_baseline.cpython-312.pyc").write_bytes(b"x")
        (root / "test/package/build/lib/hello").mkdir(parents=True)
        (root / "test/package/build/lib/hello/__init__.py").write_text("", encoding="utf-8")
        (root / "test/package/hello.egg-info").mkdir()
        (root / "test/package/hello.egg-info/PKG-INFO").write_text(
            "Name: hello\n", encoding="utf-8"
        )
        _write(root / "test/sp/site-packages/.gitkeep", "")
        tamper, skip, _ = _test_integrity(
            root,
            baseline,
            {},
            changed_paths={"test/sp/site-packages/.gitkeep"},
            overlay_root=root,
        )
        assert tamper["status"] == "PASS", tamper
        assert skip["status"] == "PASS", skip


def _check_benign_legacy_token() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(
            root / "src/shiv/bootstrap/__init__.py",
            "import subprocess\n"
            "legacy_backup = 'backup-name-used-only-for-an-interrupted-extraction'\n"
            "def run():\n    subprocess.run(['true'])\n",
        )
        config_path = Path(__file__).resolve().parents[1] / "config.json"
        policy = (
            json.loads(config_path.read_text(encoding="utf-8")).get("policy", {})
            if config_path.is_file()
            else {}
        )
        hits = scan_deceptive_fallbacks(root, policy)
        assert not hits, f"benign 'legacy' identifier was treated as a fallback: {hits}"


def _check_benign_legacy_docstring() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(
            root / "src/shiv/bootstrap/__init__.py",
            '"""Bootstrap helpers.\n'
            "\n"
            "Historically this module was written against Click 6.7 from the\n"
            "old wheelhouse at /opt/migration; the migration to Click 8.1.7\n"
            "keeps the same public behaviour and still uses subprocess for pip.\n"
            '"""\n'
            "import subprocess\n"
            "\n"
            "def run_pip(args):\n"
            "    return subprocess.run(args, check=True)\n",
        )
        config_path = Path(__file__).resolve().parents[1] / "config.json"
        policy = (
            json.loads(config_path.read_text(encoding="utf-8")).get("policy", {})
            if config_path.is_file()
            else {}
        )
        hits = scan_deceptive_fallbacks(root, policy)
        assert not hits, f"docstring version note was treated as a fallback: {hits}"


def _check_benign_added_test() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(root / "test/test_baseline.py", "def test_ok():\n    assert True\n")
        baseline = build_source_test_baseline(root)
        _write(
            root / "test/test_new_coverage.py",
            "def test_migration_note():\n    assert 1 + 1 == 2\n",
        )
        tamper, skip, _ = _test_integrity(
            root,
            baseline,
            {},
            changed_paths={"test/test_new_coverage.py"},
            overlay_root=root,
        )
        assert tamper["status"] == "PASS", tamper
        assert skip["status"] == "PASS", skip


def _check_reward_transport_numeric() -> None:
    from grader import _write_reward  # type: ignore

    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary)
        _write_reward(
            output,
            {
                "status": "failed",
                "hardgate": "FAIL",
                "cheating": False,
                "components": {
                    "regression_test": {
                        "p2p": {
                            "status": "FAIL",
                            "passed": 47,
                            "total": 48,
                            "checks": [],
                        },
                        "f2p": {
                            "status": "NA",
                            "passed": 0,
                            "total": 11,
                            "checks": [],
                        },
                    },
                    "migration_scope_complete": {
                        "status": "FAIL",
                        "passed": 0,
                        "total": 10,
                        "checks": [],
                    },
                },
                "test_progress": {"T": 0.0},
                "observations": [],
                "eligible_for_graded": False,
                "hardgate_compound_gates": {},
            },
            config={"tests": {}},
        )
        transport = json.loads((output / "reward.json").read_text(encoding="utf-8"))
        assert transport, "reward transport was empty"
        assert all(
            isinstance(value, (int, float)) for value in transport.values()
        ), transport
        assert "f2p" not in transport, transport
        details = json.loads((output / "reward-details.json").read_text(encoding="utf-8"))
        assert details["f2p_pass_ratio"] is None, details
        assert details["p2p_pass_ratio"] == 47 / 48, details


def _check_scoring_interference() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _write(
            root / "src/shiv/__init__.py",
            "import sys\n"
            "plugin = getattr(sys.modules.get('__main__'), 'ObservationPlugin', None)\n"
            "if plugin is not None:\n    plugin.rows = lambda self: []\n",
        )
        hits = _scoring_interference_hits(root, {"src/shiv/__init__.py"})
        assert hits, "observation-plugin tampering was not detected"


def _check_observation_consistency() -> None:
    all_pass = {"observations": [{"id": "P2P-001", "status": "PASS"}]}
    assert observation_result_errors(all_pass, actual_returncode=1), (
        "all-PASS report with nonzero harness status was accepted"
    )
    failed = {"observations": [{"id": "F2P-001", "status": "FAIL"}]}
    assert observation_result_errors(failed, actual_returncode=0), (
        "failed report with zero harness status was accepted"
    )
    clean = {"observations": [{"id": "P2P-001", "status": "PASS"}]}
    assert not observation_result_errors(clean, actual_returncode=0)


def _check_short_circuit_ratio() -> None:
    p2p = [
        {"id": f"P2P-{index:03d}", "status": "PASS"}
        for index in range(1, 48)
    ] + [{"id": "P2P-048", "status": "FAIL"}]
    f2p = [
        {"id": f"F2P-{index:03d}", "status": "NA"}
        for index in range(1, 12)
    ]
    hardgate = {"status": "FAIL", "cheating": False, "ordinary_residue_hits": []}
    result = evaluate_verifier(
        hardgate=hardgate,
        migration_units=[],
        p2p=p2p,
        f2p=f2p,
        differential=[],
        state={"applicable": False, "checks": []},
        weights={"test": 1.0},
        unit_checks={},
        applicability=None,
    )
    regression = result["components"]["regression_test"]
    assert regression["p2p"]["passed"] == 47, regression["p2p"]
    assert regression["p2p"]["total"] == 48, regression["p2p"]
    assert regression["f2p"]["status"] == "NA", regression["f2p"]
    assert result.get("test_progress") is None


def _check_cheating_component_status() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "verifier-result.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "migration-verifier-result:v2",
                    "status": "failed",
                    "hardgate": "FAIL",
                    "cheating": True,
                    "components": {
                        "regression_test": {
                            "p2p": {
                                "status": "INFRA_ERROR",
                                "passed": 0,
                                "total": 48,
                                "checks": [],
                            },
                            "f2p": {
                                "status": "NA",
                                "passed": 0,
                                "total": 11,
                                "checks": [],
                            },
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        component = build_verifier_component(
            json.loads(path.read_text(encoding="utf-8")),
            verifier_result_path=path,
        )
        assert component["status"] == "candidate_failed", component["status"]
        assert component["cheating_detected"] is True
        assert component["errors"]["infrastructure_error"] is False
        assert component["p2p_pass_ratio"] is None
        assert component["f2p_pass_ratio"] is None


def _check_reward_policy() -> None:
    assert reward_decision(
        hardgate="PASS", p2p_status="PASS", cheating=False, test_reward=0.75
    ) == (0.75, True)
    assert reward_decision(
        hardgate="FAIL", p2p_status="FAIL", cheating=False, test_reward=0.0
    ) == (0.0, False)
    assert reward_decision(
        hardgate="PASS", p2p_status="PASS", cheating=True, test_reward=0.75
    ) == (-1.0, False)


def _check_source_baseline_alignment() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        assets = Path(temporary)
        _write(assets / "candidate-test/conftest.py", "")
        _write(assets / "candidate-test/test_dependency.py", "")
        test_file = assets / "candidate-test/test_migration_p2p.py"
        suite = (
            "import pytest\n"
            "@pytest.fixture(params=['source', 'target'])\n"
            "def python(request): pass\n"
            "@pytest.fixture\n"
            "def runner(python): pass\n"
            "@pytest.mark.parametrize('scenario', ['a', 'b', 'c'])\n"
            "def test_p2p_015_merge(runner, python, scenario): pass\n"
        )
        _write(test_file, suite)
        config = {"base_commit": "base", "tests": {"p2p": [{"id": "P2P-015"}]}}
        row = {"id": "P2P-015", "kind": "P2P", "status": "PASS",
               "value": {"type": "json", "value": {"test": "P2P-015", "outcomes": ["PASS"] * 6}},
               "evidence": {"cases": 6}}
        baseline = {"base_commit": "base", "observations": [row]}
        assert collected_p2p_counts(assets) == {"P2P-015": 6}
        assert not source_baseline_errors(assets, config, baseline)
        _write(test_file, suite.replace("['a', 'b', 'c']", "['a', 'b', 'c', 'd']"))
        assert any("collects 8 cases" in error for error in source_baseline_errors(assets, config, baseline))
        _write(test_file, suite.replace("['a', 'b', 'c']", "make_parameters()"))
        assert source_baseline_errors(assets, config, baseline), "dynamic counts were guessed"
        _write(test_file, suite)
        row["evidence"]["cases"] = 5
        assert source_baseline_errors(assets, config, baseline), "stale case evidence was accepted"
        row["evidence"]["cases"] = 6
        row["value"]["value"]["outcomes"].pop()
        assert source_baseline_errors(assets, config, baseline), "missing baseline outcome was accepted"
        row["value"]["value"]["outcomes"].append("FAIL")
        assert source_baseline_errors(assets, config, baseline), "failed source P2P baseline was accepted"
        row["value"]["value"]["outcomes"][-1] = "PASS"
        baseline["observations"].append(row)
        assert source_baseline_errors(assets, config, baseline), "duplicate source observation was accepted"

        # Identical fixture names in separate modules must keep their scopes.
        _write(test_file, "import pytest\n@pytest.fixture(params=[1, 2])\n"
               "def local(): pass\ndef test_p2p_001_local(local): pass\n")
        _write(assets / "candidate-test/test_dependency.py",
               "import pytest\n@pytest.fixture(params=[1, 2, 3])\n"
               "def local(): pass\ndef test_p2p_002_local(local): pass\n")
        assert collected_p2p_counts(assets) == {"P2P-001": 2, "P2P-002": 3}
        _write(test_file, "import pytest\nvalues=[1, 2]\n"
               "@pytest.mark.parametrize('value', [*values, 3])\n"
               "def test_p2p_001_expansion(value): pass\n")
        assert any("static list or tuple" in error for error in source_baseline_errors(assets, config, baseline)), (
            "expanded parameter count was guessed"
        )


def run_security_self_check() -> dict[str, object]:
    failures: list[dict[str, str]] = []
    checks = (
        ("deceptive-fallback-l2", _check_deceptive_fallback_l2),
        ("install-hook-vendored-runtime-l2", _check_install_hook_vendored_runtime_l2),
        ("renamed-archive-l2", _check_renamed_archive_l2),
        ("benign-legacy-token", _check_benign_legacy_token),
        ("benign-legacy-docstring", _check_benign_legacy_docstring),
        ("benign-added-test", _check_benign_added_test),
        ("test-tampering-l1", _check_test_tampering_l1),
        ("new-skip-marker", _check_new_skip_marker),
        ("platform-conditional-skip", _check_platform_conditional_skip),
        ("transient-cache-ignored", _check_transient_cache_ignored),
        ("scoring-interference", _check_scoring_interference),
        ("observation-consistency", _check_observation_consistency),
        ("short-circuit-ratio", _check_short_circuit_ratio),
        ("cheating-component-status", _check_cheating_component_status),
        ("reward-transport-numeric", _check_reward_transport_numeric),
        ("reward-policy", _check_reward_policy),
        ("source-baseline-alignment", _check_source_baseline_alignment),
    )
    for name, function in checks:
        _check(name, function, failures)
    return {
        "schema_version": "migration-verifier-security-regression:v1",
        "success": not failures,
        "checks": [name for name, _ in checks],
        "failures": failures,
    }


def main() -> int:
    result = run_security_self_check()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
