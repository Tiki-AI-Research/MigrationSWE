"""Static acceptance checks for the repaired verifier contract.

These checks run without Harbor and cover the findings that can be proven
statically:

* the reward mapping distinguishes anti-cheat -1, ordinary hard-gate 0, an
  infrastructure outcome inside the documented range, and a valid T;
* P2P is an independent hard gate and never contributes to T;
* a migration unit cannot pass from self-reported observations alone;
* empty anti-cheating rules fail closed instead of passing unconditionally.
"""

from __future__ import annotations

import copy
import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping
from unittest import mock

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "assets"
REPO_ROOT = HERE.parents[2] if len(HERE.parents) >= 3 else HERE.parent
# `verifier/` also contains verifier.py, so the directory must not be first on
# sys.path when grader.py imports `verifier.component_result` as a namespace
# package.  Load grader explicitly and restore the import path afterwards.
while str(HERE) in sys.path:
    sys.path.remove(str(HERE))
sys.path.insert(0, str(HERE.parent))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("migration_grader", HERE.parent / "grader.py")
grader = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(grader)

sys.path.insert(0, str(HERE))
from hardgate_engine import evaluate_hardgate  # noqa: E402
from dependency_graph import audit_dependency_graph  # noqa: E402
from baseline import load_source_baseline  # noqa: E402
from evidence import build_hardgate_evidence, _derive_execution_atoms  # noqa: E402
from integrity import (  # noqa: E402
    _assertion_fingerprints,
    check_converted_tests,
    check_executed_cases,
    load_equivalence,
)
from unit_checks import evaluate_public_contract, evaluate_unit_static_checks, _mu026  # noqa: E402
from verifier_engine import evaluate_verifier  # noqa: E402
from state_preservation import (  # noqa: E402
    ApplicabilityError as StateApplicabilityError,
    resolve_state_applicability,
)
while str(HERE) in sys.path:
    sys.path.remove(str(HERE))
sys.path.insert(0, str(HERE.parent))
from verifier import harbor_entry  # noqa: E402
sys.path.insert(0, str(HERE))


def _gate(observations: list[dict], *, gate_status: str = "PASS", cheating: bool = False, level: str = "L0") -> dict:
    return {
        "status": gate_status,
        "cheating": cheating,
        "cheat_level": level,
        "reward_override": -1 if cheating else 0 if gate_status == "FAIL" else None,
        "ordinary_residue_hits": [],
        "observations": observations,
    }


def _fixture_config() -> dict:
    config = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
    config["policy"]["allowed_equivalent_test_paths"] = []
    return config


def _rows(kind: str, status: str, count: int = 2) -> list[dict]:
    return [
        {"id": f"{kind}-{index:03d}", "kind": kind, "status": status, "value": {"type": "json", "value": index}}
        for index in range(1, count + 1)
    ]


def _units(count: int = 2) -> list[dict]:
    return [
        {
            "id": f"MU{index:03d}",
            "kind": "file",
            "source_locators": [{"path": "index.js", "type": "file"}],
            "requirement_ids": ["R001"],
            "target_condition": "index.js keeps its exports",
            "validation_methods": ["static"],
            "evidence_strategy": ["p2p"],
            "allowed_exceptions": [],
        }
        for index in range(1, count + 1)
    ]


def _unit_checks(units: list[dict], observation_ids: list[str]) -> dict:
    return {unit["id"]: {"observation_ids": observation_ids} for unit in units}


def check_reward_mapping() -> list[str]:
    errors: list[str] = []
    cases = [
        ("anti-cheat L2", {"status": "failed", "hardgate": "FAIL", "cheating": True, "cheat_level": "L2", "reward_override": -1}, -1.0),
        ("anti-cheat L1", {"status": "failed", "hardgate": "FAIL", "cheating": True, "cheat_level": "L1", "reward_override": -1}, -1.0),
        ("ordinary hard-gate failure", {"status": "failed", "hardgate": "FAIL", "cheating": False, "cheat_level": "L0", "reward_override": 0}, 0.0),
        ("infrastructure outcome", {"status": "infra_error", "hardgate": "INFRA_ERROR", "cheating": False, "cheat_level": "L0"}, 0.0),
        ("valid T", {"status": "success", "hardgate": "PASS", "cheating": False, "cheat_level": "L0", "test_progress": {"T": 0.75}}, 0.75),
    ]
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory)
        for label, result, expected in cases:
            grader._write_reward(output, result)
            payload = json.loads((output / "reward.json").read_text(encoding="utf-8"))
            reward = payload.get("reward")
            if reward != expected:
                errors.append(f"{label}: expected reward {expected}, got {reward}")
            if not isinstance(reward, (int, float)) or not -1.0 <= reward <= 1.0:
                errors.append(f"{label}: reward {reward} is outside the documented range")
        return errors


def check_p2p_excluded_from_progress() -> list[str]:
    errors: list[str] = []
    units = _units()
    p2p_ids = [row["id"] for row in _rows("P2P", "PASS")]
    unit_checks = _unit_checks(units, p2p_ids)
    common = dict(
        migration_units=units,
        unit_checks=unit_checks,
        differential=[],
        state={"applicable": False, "checks": []},
        weights={},
        applicability={},
    )
    passing = evaluate_verifier(
        hardgate=_gate(_rows("P2P", "PASS")),
        p2p=_rows("P2P", "PASS"),
        f2p=_rows("F2P", "PASS"),
        **common,
    )
    failing = evaluate_verifier(
        hardgate=_gate(_rows("P2P", "FAIL")),
        p2p=_rows("P2P", "FAIL"),
        f2p=_rows("F2P", "PASS"),
        **common,
    )
    if passing["test_progress"]["T"] != failing["test_progress"]["T"]:
        errors.append(
            "P2P status must not change T "
            f"({passing['test_progress']['T']} vs {failing['test_progress']['T']})"
        )
    if passing["test_progress"]["total"] != len(units) + len(_rows("F2P", "PASS")):
        errors.append(
            "T denominator must cover migration scope and F2P only, got "
            f"{passing['test_progress']['total']}"
        )
    if "p2p_regression_preserved" not in passing["components"]:
        errors.append("a dedicated p2p_regression_preserved gate is required")
    if passing["components"]["p2p_regression_preserved"]["status"] != "PASS":
        errors.append("p2p_regression_preserved must reflect passing P2P rows")
    return errors


def check_state_applicability_fails_closed() -> list[str]:
    """Missing or contradictory state declarations must never become NA."""

    errors: list[str] = []
    policy = {"state_applicable": False}
    groups = {"STATE": {"status": "na", "reason": "fixture"}}
    contract = {"state_preservation": {"applicable": False}}
    resolved = resolve_state_applicability(
        policy=policy, group_applicability=groups, contract=contract
    )
    if resolved.get("applicable") is not False:
        errors.append(
            "consistent state declarations must resolve to not applicable, "
            f"got {resolved!r}"
        )
    invalid_cases = {
        "missing policy key": ({}, groups, contract),
        "missing group declaration": (policy, {}, contract),
        "missing contract declaration": (policy, groups, {}),
        "policy/group disagreement": (
            {"state_applicable": True},
            groups,
            contract,
        ),
        "non-boolean policy value": (
            {"state_applicable": "false"},
            groups,
            contract,
        ),
    }
    for label, (case_policy, case_groups, case_contract) in invalid_cases.items():
        try:
            resolve_state_applicability(
                policy=case_policy,
                group_applicability=case_groups,
                contract=case_contract,
            )
        except StateApplicabilityError:
            continue
        errors.append(f"{label} must raise a configuration error")
    units = _units()
    result = evaluate_verifier(
        hardgate=_gate(_rows("P2P", "PASS")),
        migration_units=units,
        unit_checks=_unit_checks(units, [row["id"] for row in _rows("P2P", "PASS")]),
        p2p=_rows("P2P", "PASS"),
        f2p=_rows("F2P", "PASS"),
        differential=[],
        state={"applicable": None, "checks": []},
        weights={},
        applicability=groups,
    )
    if result.get("status") != "inconclusive":
        errors.append(
            "an unresolved state declaration must make the verifier "
            f"inconclusive, got {result.get('status')!r}"
        )
    state = result.get("components", {}).get(
        "required_state_preservation_complete", {}
    )
    if state.get("status") == "NA":
        errors.append("an unresolved state declaration must not be reported as NA")
    progress = result.get("test_progress")
    if not isinstance(progress, Mapping) or progress.get("T") != 0.0:
        errors.append(
            "an unresolved required state dimension must not be compensated by "
            f"other groups (T={progress!r})"
        )
    return errors


def check_p2p_hard_gate() -> list[str]:
    errors: list[str] = []
    config = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
    rows = [
        {"id": "P2P-001", "kind": "P2P", "status": "PASS", "value": {"type": "json", "value": 1}},
        {"id": "P2P-002", "kind": "P2P", "status": "FAIL", "value": {"type": "json", "value": 2}},
    ]
    evidence = {
        "patch_applicable": {"status": "PASS", "source": "test", "evidence": []},
        "build_pass": {"status": "PASS", "source": "test", "evidence": []},
        "dependency_resolved": {"status": "PASS", "source": "test", "evidence": []},
        "no_deceptive_source_fallback": {"status": "PASS", "source": "test", "evidence": []},
        "p2p_pass": {"status": "FAIL", "source": "test", "evidence": rows},
        "public_contract_preserved": {"status": "PASS", "source": "test", "evidence": []},
        "critical_data_preserved": {"status": "NA", "source": "test", "evidence": []},
        "tests_not_tampered": {"status": "PASS", "source": "test", "evidence": []},
        "no_new_skip_or_xfail": {"status": "PASS", "source": "test", "evidence": []},
        "protected_paths_unchanged": {"status": "PASS", "source": "test", "evidence": []},
        "anti_cheating_pass": {"status": "PASS", "source": "test", "evidence": []},
        "ordinary_residue_hits": [],
        "deceptive_fallback_hits": [],
        "evidence_mode": "derived",
    }
    hardgate = evaluate_hardgate(Path("."), evidence)
    if hardgate["status"] != "FAIL":
        errors.append(f"a P2P regression must fail the hard gate, got {hardgate['status']}")
    if hardgate["compound_gates"]["p2p_regression_preserved"]["status"] != "FAIL":
        errors.append("p2p_regression_preserved must FAIL when a required P2P row fails")
    if hardgate["cheating"] or hardgate["cheat_level"] != "L0":
        errors.append("an ordinary P2P regression must not be classified as cheating")
    if hardgate["reward_override"] != 0:
        errors.append("an ordinary hard-gate failure must map to 0, not -1")
    del config
    return errors


def check_mu022_does_not_require_release_notes() -> list[str]:
    errors: list[str] = []
    root = Path(tempfile.mkdtemp())
    (root / "README.md").write_text("# devalue\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 4.3.3\n\n- previous release\n", encoding="utf-8"
    )
    config = _fixture_config()
    unit = next(row for row in config["migration_units"] if row["id"] == "MU022")
    result = evaluate_unit_static_checks(unit, root)
    if result.get("status") != "PASS":
        errors.append(
            "MU022 must not require a 6.0.0 CHANGELOG heading: "
            f"{result.get('status')} {result.get('checks')}"
        )
    return errors


def check_unit_requires_static_evidence() -> list[str]:
    errors: list[str] = []
    units = _units(1)
    unit = units[0]
    empty_root = Path(tempfile.mkdtemp())
    missing = evaluate_unit_static_checks(unit, empty_root)
    if missing["status"] == "PASS":
        errors.append("a unit with missing target files must not pass static checks")
    result = evaluate_verifier(
        hardgate=_gate(_rows("P2P", "PASS")),
        p2p=_rows("P2P", "PASS"),
        f2p=_rows("F2P", "PASS"),
        migration_units=units,
        unit_checks=_unit_checks(units, [row["id"] for row in _rows("P2P", "PASS")]),
        differential=[],
        state={"applicable": False, "checks": []},
        weights={},
        applicability={},
        candidate_root=empty_root,
    )
    scope = result["components"]["migration_scope_complete"]
    if scope["status"] != "FAIL":
        errors.append(
            "migration scope must FAIL when the declared target files are absent "
            f"(got {scope['status']})"
        )
    no_observations = evaluate_verifier(
        hardgate=_gate(_rows("P2P", "PASS")),
        p2p=_rows("P2P", "PASS"),
        f2p=_rows("F2P", "PASS"),
        migration_units=units,
        unit_checks=_unit_checks(units, []),
        differential=[],
        state={"applicable": False, "checks": []},
        weights={},
        applicability={},
        candidate_root=empty_root,
    )
    if no_observations["components"]["migration_scope_complete"]["status"] != "INCONCLUSIVE":
        errors.append("a unit with no observation rows must be INCONCLUSIVE, not success")
    return errors


def _single_unit(unit_id: str, locator: str) -> dict:
    return {
        "id": unit_id,
        "kind": "file",
        "source_locators": [{"path": locator, "type": "file"}],
        "requirement_ids": ["R001"],
        "target_condition": "declared target condition",
        "validation_methods": ["static"],
        "evidence_strategy": ["p2p"],
        "allowed_exceptions": [],
    }


def _index_with_public_exports() -> str:
    names = (
        "uneval",
        "parse",
        "unflatten",
        "stringify",
        "stringifyAsync",
        "defaultStringifyOperations",
        "defaultParseOperations",
        "DevalueError",
        "filterArrayIndices",
    )
    return "\n".join(f"export function {name}() {{}}" for name in names) + "\n"


def check_arithmetic_mean_progress() -> list[str]:
    """T must be the arithmetic mean of applicable group rates, not a product."""

    errors: list[str] = []
    root = Path(tempfile.mkdtemp())
    # Scope rate 0.5: MU003 (public exports) passes, MU001 (manifest) fails
    # because package.json is absent.
    (root / "index.js").write_text(_index_with_public_exports(), encoding="utf-8")
    units = [_single_unit("MU003", "index.js"), _single_unit("MU001", "package.json")]
    f2p = _rows("F2P", "PASS")
    unit_checks = _unit_checks(units, [row["id"] for row in f2p])
    # This assertion is about the arithmetic mean of the applicable scope and
    # F2P rates while state is explicitly not applicable.  Keep the fixture
    # self-contained: reading the mutable task config here would let an
    # independent applicability edit change the check instead of exercising
    # the engine.
    applicability = {
        "P2P": {"status": "required", "reason": "arithmetic-mean fixture"},
        "F2P": {"status": "required", "reason": "arithmetic-mean fixture"},
        "DIFFERENTIAL": {"status": "na", "reason": "arithmetic-mean fixture"},
        "STATE": {"status": "na", "reason": "arithmetic-mean fixture"},
    }
    result = evaluate_verifier(
        hardgate=_gate(_rows("P2P", "PASS")),
        migration_units=units,
        unit_checks=unit_checks,
        p2p=_rows("P2P", "PASS"),
        f2p=f2p,
        differential=[],
        state={"applicable": False, "checks": []},
        weights={},
        applicability=applicability,
        candidate_root=root,
    )
    scope = result["components"]["migration_scope_complete"]
    if scope["passed"] != 1 or scope["total"] != 2:
        errors.append(f"fixture must yield a 0.5 scope rate, got {scope['passed']}/{scope['total']}")
    expected = (0.5 + 1.0) / 2
    actual = result["test_progress"]["T"]
    if abs(actual - expected) > 1e-6:
        errors.append(
            "T must be the arithmetic mean of applicable group rates: "
            f"expected {expected}, got {actual}"
        )
    product = 0.5 * 1.0
    if abs(actual - product) <= 1e-6:
        errors.append("T must not be computed as a product of group rates")
    # A non-applicable group must be omitted from the denominator.
    if result["components"]["regression_test"]["differential"]["status"] != "NA":
        errors.append("non-applicable differential group must be NA")
    return errors


def check_candidate_execution_failures() -> list[str]:
    """Unusable candidate execution must fail candidate_valid, never pass."""

    errors: list[str] = []
    config = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
    config["policy"]["allowed_equivalent_test_paths"] = []
    root = Path(tempfile.mkdtemp())
    protected_test = root / "test" / "kept.test.js"
    protected_test.parent.mkdir(parents=True)
    protected_test.write_text(
        "test('kept', () => { assert.equal(1, 1); });\n", encoding="utf-8"
    )
    test_baseline = {
        "schema_version": "migration-source-test-baseline:v1",
        "files": {
            "test/kept.test.js": {
                "sha256": "sha256:"
                + hashlib.sha256(protected_test.read_bytes()).hexdigest()
            }
        },
        "marker_counts": {"skip": 0, "xfail": 0, "disabled": 0},
    }
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin"}

    def receipt(kind: str, command: list[str], timeout: float = 5.0) -> dict:
        completed, value = harbor_entry._run_with_receipt(
            command,
            kind=kind,
            index=1,
            cwd=root,
            timeout=timeout,
            environment=environment,
        )
        del completed
        harbor_entry._annotate_report_presence(value, root / "missing-report.json")
        return value

    cases = {
        "no-report/nonzero": receipt(
            "target-execution",
            [sys.executable, "-c", "raise SystemExit(1)"],
        ),
        "no-report/zero": receipt(
            "target-execution",
            [sys.executable, "-c", "raise SystemExit(0)"],
        ),
        "timeout": receipt(
            "target-execution",
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.1,
        ),
        "signal": receipt(
            "target-execution",
            [
                sys.executable,
                "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
            ],
        ),
    }
    if cases["no-report/nonzero"].get("failed_phase") != "exit":
        errors.append("a nonzero exit without a report must preserve the exit phase")
    if cases["no-report/zero"].get("failed_phase") != "no-report":
        errors.append("a zero exit without a report must record the no-report phase")
    if cases["timeout"].get("failed_phase") != "timeout":
        errors.append("a timeout must record the timeout phase")
    if cases["signal"].get("failed_phase") != "signal":
        errors.append("a signal exit must record the signal phase")
    for label, value in cases.items():
        if value.get("report_missing") is not True:
            errors.append(f"{label}: missing report must be recorded explicitly")
    invalid_report = root / "result.json"
    invalid_report.write_text("{", encoding="utf-8")
    try:
        harbor_entry._load_json(invalid_report)
    except (ValueError, json.JSONDecodeError) as exc:
        invalid_message = f"candidate observation report is not valid JSON: {exc}"
    else:
        errors.append("invalid observation JSON must not be accepted")
        invalid_message = "invalid observation JSON accepted"

    for label, value in cases.items():
        message = harbor_entry._candidate_execution_error(label, [value])
        result = harbor_entry._candidate_evidence_result(
            message,
            candidate=root,
            config=config,
            units=[],
            test_baseline=test_baseline,
            receipts=[value],
            observation_errors=[message],
            harness_returncode=value.get("returncode"),
        )
        atom = result["hardgate_result"]["compound_gates"]["candidate_valid"]
        if atom["status"] != "FAIL":
            errors.append(f"{label}: candidate_valid must be FAIL, got {atom['status']}")
        if result.get("reward_override") != 0:
            errors.append(
                f"{label}: ordinary candidate failure must reward 0, "
                f"got {result.get('reward_override')}"
            )
        recorded = result["receipts"]["commands"][0]
        if recorded.get("report_present") is not False:
            errors.append(f"{label}: receipt must record that no report was produced")
        if recorded.get("failed_phase") not in {
            "exit",
            "timeout",
            "signal",
            "no-report",
        }:
            errors.append(f"{label}: receipt does not record a failure phase")

    invalid = harbor_entry._candidate_evidence_result(
        invalid_message,
        candidate=root,
        config=config,
        units=[],
        test_baseline=test_baseline,
        receipts=[cases["no-report/zero"]],
        observation_errors=[invalid_message],
    )
    if (
        invalid["hardgate_result"]["compound_gates"]["candidate_valid"]["status"]
        != "FAIL"
    ):
        errors.append("invalid JSON must fail candidate_valid")
    if invalid.get("reward_override") != 0:
        errors.append("invalid JSON must produce ordinary reward 0")

    failed_build = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": "target-build",
        "index": 1,
        "command": ["pnpm", "run", "build"],
        "returncode": 1,
        "timed_out": False,
        "failed_phase": "exit",
        "report_present": False,
    }
    successful_graph = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": "dependency-graph",
        "index": 2,
        "command": ["node", "verifier/dependency_graph.mjs"],
        "returncode": 0,
        "timed_out": False,
        "report_present": False,
    }
    diagnostic = harbor_entry._candidate_execution_error(
        "candidate prerequisite command failed",
        [failed_build, successful_graph],
    )
    if "target-build exited with code 1" not in diagnostic:
        errors.append(
            "the first real failed receipt must drive the candidate diagnostic: "
            f"{diagnostic!r}"
        )
    if "dependency-graph" in diagnostic:
        errors.append(
            "a later successful receipt must not overwrite the failed command: "
            f"{diagnostic!r}"
        )

    report_root = Path(tempfile.mkdtemp())
    raw_report = report_root / "raw-report.json"
    raw_report.write_text(
        json.dumps(
            {
                "numTotalTests": 2,
                "testResults": [
                    {
                        "name": "test/passing.test.js",
                        "assertionResults": [
                            {
                                "ancestorTitles": [],
                                "title": "passing",
                                "status": "passed",
                            }
                        ],
                    },
                    {
                        "name": "test/candidate-owned-skip.test.js",
                        "assertionResults": [
                            {
                                "ancestorTitles": [],
                                "title": "candidate-owned skip",
                                "status": "skipped",
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    assertion_log = report_root / "assertions.jsonl"
    assertion_log.write_text(
        json.dumps(
            {
                "file": "test/passing.test.js",
                "name": "passing",
                "expectCalls": 1,
                "assertCalls": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        persisted = harbor_entry._write_test_execution_report(
            raw_report,
            report_root / "persisted-report.json",
            candidate=report_root,
            assertion_log=assertion_log,
        )
        persisted_payload = json.loads(persisted.read_text(encoding="utf-8"))
    except harbor_entry.InfrastructureError as exc:
        errors.append(
            "a skipped candidate-owned test must not invalidate the execution "
            f"report: {exc}"
        )
    else:
        rows = {
            row["title"]: row
            for file_row in persisted_payload.get("testResults", [])
            for row in file_row.get("assertionResults", [])
        }
        if rows.get("passing", {}).get("assertionCount") != 2:
            errors.append(
                "a passed test must retain the evaluator assertion count"
            )
        if rows.get("candidate-owned skip", {}).get("assertionCount") != 0:
            errors.append(
                "a skipped test without an afterEach assertion row must record 0"
            )
    return errors


def check_not_run_reporting_and_diagnostic_isolation() -> list[str]:
    """Dependency failure must report NOT_RUN without grading diagnostics."""

    errors: list[str] = []
    config = _fixture_config()
    root = Path(tempfile.mkdtemp())
    protected_test = root / "test" / "kept.test.js"
    protected_test.parent.mkdir(parents=True)
    protected_test.write_text(
        "test('kept', () => { assert.equal(1, 1); });\n", encoding="utf-8"
    )
    test_baseline = {
        "schema_version": "migration-source-test-baseline:v1",
        "files": {
            "test/kept.test.js": {
                "sha256": "sha256:"
                + hashlib.sha256(protected_test.read_bytes()).hexdigest()
            }
        },
        "marker_counts": {"skip": 0, "xfail": 0, "disabled": 0},
    }
    dependency_receipt = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": "dependency-resolution",
        "index": 1,
        "command": ["pnpm", "install", "--offline", "--frozen-lockfile"],
        "returncode": 1,
        "timed_out": False,
        "report_present": False,
        "failed_phase": "exit",
    }
    diagnostic = {
        "schema_version": "migration-diagnostic:v1",
        "diagnostic_only": True,
        "eligible_for_graded": False,
        "status": "COMPLETED",
        "f2p_status": "PASS",
        "p2p_status": "PASS",
        "summary": {
            "f2p": {"status": "PASS", "passed": 4, "total": 4},
            "p2p": {"status": "PASS", "passed": 51, "total": 51},
        },
        "observations": [
            {"id": "F2P-001", "kind": "F2P", "status": "PASS"},
            {"id": "P2P-001", "kind": "P2P", "status": "PASS"},
        ],
        "contract_errors": [],
    }
    message = "candidate dependency graph or import probe failed: install exited 1"
    result = harbor_entry._candidate_evidence_result(
        message,
        candidate=root,
        config=config,
        units=[],
        test_baseline=test_baseline,
        receipts=[dependency_receipt],
        observation_errors=[message],
        not_run_reason=message,
        diagnostic=diagnostic,
    )
    if result.get("hardgate") != "FAIL":
        errors.append(f"dependency failure must keep hardgate FAIL, got {result.get('hardgate')}")
    if result.get("reward_override") != 0:
        errors.append("dependency failure must keep reward override 0")
    if result.get("f2p_status") != "NOT_RUN" or result.get("p2p_status") != "NOT_RUN":
        errors.append("dependency failure must report F2P/P2P NOT_RUN")
    if result.get("not_run_reason") != message:
        errors.append("dependency failure must preserve the not-run reason")
    if result.get("eligible_for_graded") is not False:
        errors.append("a diagnostic-only result must not be eligible for grading")
    if result.get("diagnostic_only") is not True:
        errors.append("a diagnostic-only result must set diagnostic_only")
    if result.get("observations") != []:
        errors.append("diagnostic observations must not enter formal observations")
    regression = result.get("components", {}).get("regression_test", {})
    for group in ("f2p", "p2p"):
        component = regression.get(group, {}) if isinstance(regression, Mapping) else {}
        if component.get("status") != "NOT_RUN":
            errors.append(f"formal {group} component must be NOT_RUN, got {component.get('status')}")
        if component.get("evaluated") is not False:
            errors.append(f"formal {group} component must record evaluated=false")
    if grader._score(result)["reward"] != 0.0:
        errors.append("diagnostic evidence must not change the zero reward")
    if result.get("diagnostic", {}).get("f2p_status") != "PASS":
        errors.append("diagnostic F2P detail must remain available")

    output = Path(tempfile.mkdtemp())
    grader._write_reward_details(output, result)
    details = json.loads((output / "reward-details.json").read_text(encoding="utf-8"))
    if details.get("f2p_status") != "NOT_RUN" or details.get("p2p_status") != "NOT_RUN":
        errors.append("reward-details must expose NOT_RUN group statuses")
    if details.get("not_run_reason") != message:
        errors.append("reward-details must expose the not-run reason")
    if details.get("diagnostic_only") is not True:
        errors.append("reward-details must mark diagnostic-only output")
    if details.get("reward") != 0.0:
        errors.append("reward-details must keep reward 0 for a dependency failure")
    if details.get("diagnostic", {}).get("f2p_status") != "PASS":
        errors.append("reward-details must retain the diagnostic F2P detail")

    verifier_path = output / "verifier-result.json"
    verifier_path.write_text(json.dumps(result), encoding="utf-8")
    component = grader.write_verifier_component(
        result,
        verifier_result_path=verifier_path,
        output_path=output / "verifier-component.json",
    )
    groups = component.get("groups", {})
    if groups.get("f2p", {}).get("status") != "NOT_RUN":
        errors.append("verifier component must expose F2P as NOT_RUN")
    if groups.get("p2p", {}).get("status") != "NOT_RUN":
        errors.append("verifier component must expose P2P as NOT_RUN")
    if component.get("f2p_pass_ratio") is not None:
        errors.append("a NOT_RUN F2P group must not publish a pass ratio")
    return errors


def check_declared_type_entry_and_benchmark_scope() -> list[str]:
    errors: list[str] = []
    units_payload = json.loads(
        (HERE.parent / "assets" / "migration-units.json").read_text(encoding="utf-8")
    )
    units = units_payload.get("units", [])
    if any(row.get("id") == "MU023" for row in units if isinstance(row, dict)):
        errors.append("MU023 benchmark unit must not be part of the migration scope")
    if any(
        "bench" in str(row.get("target_condition", "")).casefold()
        for row in units
        if isinstance(row, dict)
    ):
        errors.append("migration units must not impose an undeclared benchmark contract")

    root = Path(tempfile.mkdtemp())
    (root / "declarations").mkdir()
    (root / "declarations" / "public.d.ts").write_text(
        "export declare function stringify(value: unknown): string;\n"
        "export declare function parse(value: string): unknown;\n",
        encoding="utf-8",
    )
    (root / "index.js").write_text(_index_with_public_exports(), encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "devalue",
                "version": "6.0.0",
                "type": "module",
                "types": "./declarations/public.d.ts",
                "exports": {
                    ".": {
                        "types": "./declarations/public.d.ts",
                        "import": "./index.js",
                        "default": "./index.js",
                    }
                },
                "scripts": {"test": "vitest run", "test:types": "tsc --noEmit"},
            }
        ),
        encoding="utf-8",
    )
    contract = evaluate_public_contract(root)
    if contract["status"] != "PASS":
        errors.append(
            "a package with a valid relocated declaration entry must pass the "
            f"public contract: {contract['checks']}"
        )
    mu009 = next((row for row in units if row.get("id") == "MU009"), None)
    if mu009 is None:
        errors.append("MU009 type-entry unit is missing")
    else:
        static = evaluate_unit_static_checks(mu009, root)
        if static["status"] != "PASS":
            errors.append(
                "MU009 must resolve the declared type entry rather than "
                f"src/types.d.ts: {static['checks']}"
            )
    missing = Path(tempfile.mkdtemp())
    (missing / "package.json").write_text(
        json.dumps(
            {
                "types": "./declarations/missing.d.ts",
                "exports": {".": {"types": "./declarations/missing.d.ts"}},
            }
        ),
        encoding="utf-8",
    )
    missing_contract = evaluate_public_contract(missing)
    if missing_contract["status"] != "FAIL":
        errors.append("an unreadable declared type entry must fail the public contract")
    return errors


def _derived_evidence(**overrides: str) -> dict:
    defaults = {
        "patch_applicable": "PASS",
        "observation_contract_valid": "PASS",
        "build_pass": "PASS",
        "dependency_resolved": "PASS",
        "no_deceptive_source_fallback": "PASS",
        "p2p_pass": "PASS",
        "public_contract_preserved": "PASS",
        "critical_data_preserved": "NA",
        "tests_not_tampered": "PASS",
        "no_new_skip_or_xfail": "PASS",
        "protected_paths_unchanged": "PASS",
        "anti_cheating_pass": "PASS",
        "isolation_enforced": "PASS",
    }
    defaults.update(overrides)
    evidence = {
        atom: {"status": status, "source": "acceptance-test", "evidence": []}
        for atom, status in defaults.items()
    }
    evidence.update(
        {
            "ordinary_residue_hits": [],
            "deceptive_fallback_hits": [],
            "evidence_mode": "derived",
        }
    )
    return evidence


def _evaluator_node_modules() -> Path | None:
    candidates: list[Path] = []
    configured = os.environ.get("MIGRATION_EVALUATOR_NODE_MODULES")
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path("/opt/migration/dependencies/node_modules"))
    candidates.extend(sorted(Path("/tmp").glob("devalue-ref.*/node_modules")))
    for candidate in candidates:
        if (candidate / "@js-temporal/polyfill" / "package.json").is_file():
            return candidate
    return None


def _format_observations(
    expectations: dict,
    digest: str,
    *,
    wrap_payloads: bool = False,
) -> list[dict]:
    observations: list[dict] = []
    for identifier in harbor_entry._FORMAT_OBSERVATION_IDS:
        fmt = copy.deepcopy(expectations[identifier])
        if wrap_payloads and "payload" in fmt:
            fmt["payload"] = ["review-format-incompatible", fmt["payload"]]
            if isinstance(fmt.get("async_payload"), list):
                fmt["async_payload"] = [
                    "review-format-incompatible",
                    fmt["async_payload"],
                ]
        observations.append(
            {
                "id": identifier,
                "kind": "P2P",
                "status": "PASS",
                "value": {"type": "json", "value": {"format": fmt}},
                "evidence": {
                    "source": "acceptance-self-check",
                    "check": identifier,
                    "format_cross_inputs_sha256": digest,
                },
            }
        )
    return observations


def check_wire_format_cross_consumption() -> list[str]:
    """Target format must match the frozen corpus and still accept source data."""

    errors: list[str] = []
    config = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
    baseline = load_source_baseline(
        ASSETS / "source-baseline-outputs.json", config=config
    )
    expectations = harbor_entry._format_expectations(ASSETS.resolve(), config)
    source_inputs = harbor_entry._source_format_inputs(baseline)
    cross_inputs = harbor_entry._cross_input_payload(source_inputs)
    digest = str(cross_inputs["digest"])
    if not cross_inputs["cases"]:
        errors.append(
            "the source runtime must contribute at least one consumable payload"
        )

    valid = _format_observations(expectations, digest)
    valid_errors, _, valid_records, valid_contract = (
        harbor_entry._candidate_format_errors(
            expectations, valid, expected_digest=digest
        )
    )
    if valid_errors:
        errors.append(
            "the frozen target payload corpus must be self-consistent: "
            f"{valid_errors[:2]}"
        )

    # The public malformed-payload contract requires rejection, not one exact
    # exception name or message.  A candidate that throws a different error for
    # the same malformed input is still compatible.
    error_variant = copy.deepcopy(valid)
    for row in error_variant:
        if row.get("id") != "P2P-007":
            continue
        row["value"]["value"]["format"]["error"] = {
            "message": "different but still a rejection",
            "name": "Error",
            "path": None,
            "threw": True,
        }
    variant_errors, _, _, _ = harbor_entry._candidate_format_errors(
        expectations, error_variant, expected_digest=digest
    )
    if variant_errors:
        errors.append(
            "a different malformed-payload exception type or message must not "
            f"fail wire-format compatibility: {variant_errors[:2]}"
        )

    # A single incompatible observation must not cascade to every format
    # observation; the caller uses the per-id map to localize the failure.
    broken_one = copy.deepcopy(valid)
    for row in broken_one:
        if row.get("id") != "P2P-007":
            continue
        row["value"]["value"]["format"]["unflatten_value"] = [["Uint8Array", 100]]
    one_errors, one_by_id, _, one_contract = harbor_entry._candidate_format_errors(
        expectations, broken_one, expected_digest=digest
    )
    if not one_errors or set(one_by_id) != {"P2P-007"}:
        errors.append(
            "a single incompatible format observation must be attributed only "
            f"to that observation: {sorted(one_by_id)}"
        )
    if valid_contract:
        errors.append(
            "a compatible candidate must not produce wire-format contract "
            f"errors: {valid_contract[:2]}"
        )
    if set(one_contract) != set(one_errors):
        errors.append(
            "an incompatible P2P wire format must stay a candidate-validity "
            f"contract error: {one_contract[:2]}"
        )

    # A target-only F2P capability that the candidate has not implemented is a
    # behavioural miss: it fails its own observation and lowers the F2P ratio,
    # but it must not invalidate the whole candidate.
    f2p_only = copy.deepcopy(valid)
    for row in f2p_only:
        if row.get("id") != "P2P-051":
            continue
        row["kind"] = "F2P"
        row["value"]["value"]["format"]["parse"] = {
            "type": "Array",
            "length": 1,
            "value": [],
        }
    f2p_errors, f2p_by_id, _, f2p_contract = harbor_entry._candidate_format_errors(
        expectations, f2p_only, expected_digest=digest
    )
    if not f2p_errors or set(f2p_by_id) != {"P2P-051"}:
        errors.append(
            "an incompatible F2P wire format must fail only its own "
            f"observation: {sorted(f2p_by_id)}"
        )
    if f2p_contract:
        errors.append(
            "an incompatible F2P wire format must lower the F2P ratio, not "
            f"invalidate the candidate: {f2p_contract[:2]}"
        )

    # Evidence integrity is not a behavioural miss: an F2P observation that did
    # not consume the evaluator cross-inputs still invalidates the candidate.
    f2p_tampered = copy.deepcopy(f2p_only)
    for row in f2p_tampered:
        if row.get("id") != "P2P-051":
            continue
        row["evidence"]["format_cross_inputs_sha256"] = "sha256:" + "0" * 64
    _, _, _, f2p_tampered_contract = harbor_entry._candidate_format_errors(
        expectations, f2p_tampered, expected_digest=digest
    )
    if not f2p_tampered_contract:
        errors.append(
            "an F2P observation that did not consume the evaluator cross-inputs "
            "must remain a contract error"
        )

    missing = copy.deepcopy(valid)
    for row in missing:
        row["evidence"].pop("format_cross_inputs_sha256", None)
    missing_errors, _, _, _ = harbor_entry._candidate_format_errors(
        expectations, missing, expected_digest=digest
    )
    if not missing_errors:
        errors.append("missing format_cross_inputs_sha256 must fail wire-format checks")

    wrong = copy.deepcopy(valid)
    for row in wrong:
        row["evidence"]["format_cross_inputs_sha256"] = "sha256:" + "0" * 64
    wrong_errors, _, _, _ = harbor_entry._candidate_format_errors(
        expectations, wrong, expected_digest=digest
    )
    if not wrong_errors:
        errors.append("an incorrect format_cross_inputs_sha256 must fail wire-format checks")

    # A target that cannot revive an evaluator-owned source-era payload must
    # fail the source-to-target direction.
    source_to_target = copy.deepcopy(valid)
    for row in source_to_target:
        if row.get("id") != "P2P-011":
            continue
        row["value"]["value"]["format"]["cross"]["parse"] = {
            "type": "Object",
            "value": [],
        }
    source_errors, source_by_id, _, _ = harbor_entry._candidate_format_errors(
        expectations, source_to_target, expected_digest=digest
    )
    if set(source_by_id) != {"P2P-011"}:
        errors.append(
            "a candidate that cannot consume the source payload must fail "
            f"the source-to-target check: {source_errors[:2]}"
        )

    wrapped = _format_observations(expectations, digest, wrap_payloads=True)
    wrapped_errors, _, wrapped_records, _ = harbor_entry._candidate_format_errors(
        expectations, wrapped, expected_digest=digest
    )
    if not wrapped_errors:
        errors.append(
            "a wrapped payload that only the candidate can parse must fail P2P"
        )
    # The defect is specifically that the candidate remains self-consistent:
    # its own parse/unflatten still restore the original value.
    for identifier in harbor_entry._FORMAT_OBSERVATION_IDS:
        record = wrapped_records.get(identifier)
        expected = expectations[identifier]
        if not isinstance(record, dict) or expected.get("kind") != "value":
            continue
        if record.get("parse") != expected.get("parse") or record.get(
            "unflatten"
        ) != expected.get("unflatten"):
            errors.append(
                f"the wrapper counterexample must remain self-consumable at {identifier}"
            )

    node_modules = _evaluator_node_modules()
    if node_modules is None:
        errors.append(
            "an evaluator-owned node_modules tree is required to replay the "
            "source consumer"
        )
    else:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "MIGRATION_EVALUATOR_NODE_MODULES": str(node_modules),
        }
        source_commit = str(baseline.get("source_commit") or "")

        def replay(
            records: dict,
            *,
            observation_kinds: dict[str, str] | None = None,
        ) -> tuple[list[str], dict[str, str], dict, list[str]]:
            return harbor_entry._source_consume_errors(
                expectations,
                assets=ASSETS.resolve(),
                cases=harbor_entry._source_consume_cases(expectations, records),
                source_commit=source_commit,
                timeout=60.0,
                environment=environment,
                observation_kinds=observation_kinds,
            )

        try:
            consume_errors, consume_by_id, receipt, consume_contract = replay(
                valid_records
            )
        except harbor_entry.InfrastructureError as exc:
            errors.append(f"source-consumption replay failed to run: {exc}")
        else:
            if consume_errors:
                errors.append(
                    "a source-era consumer must restore every backward-"
                    f"compatible target payload: {consume_errors[:2]}"
                )
            if consume_by_id:
                errors.append(
                    "a successful source-consumption replay must not attribute "
                    f"errors to observations: {sorted(consume_by_id)}"
                )
            if consume_contract:
                errors.append(
                    "a successful source-consumption replay must not produce "
                    f"contract errors: {consume_contract[:2]}"
                )
            if receipt.get("matched") is not True:
                errors.append(
                    "the source-consumption receipt must record the successful match"
                )
            f2p_consume, _, _, f2p_contract = replay(
                wrapped_records,
                observation_kinds={
                    identifier: "F2P"
                    for identifier in harbor_entry._FORMAT_OBSERVATION_IDS
                },
            )
            if not f2p_consume:
                errors.append(
                    "an F2P payload the source runtime cannot restore must still "
                    "be recorded as an observation failure"
                )
            if f2p_contract:
                errors.append(
                    "an F2P payload the source runtime cannot restore must lower "
                    "the F2P ratio, not invalidate the candidate: "
                    f"{f2p_contract[:2]}"
                )
        try:
            wrapped_consume, wrapped_by_id, wrapped_receipt, _ = replay(
                wrapped_records
            )
        except harbor_entry.InfrastructureError as exc:
            errors.append(f"source-consumption replay failed to run: {exc}")
        else:
            if not wrapped_consume:
                errors.append(
                    "the source runtime must reject or misread a payload that "
                    "only the candidate parser understands"
                )
            if not wrapped_by_id:
                errors.append(
                    "a failed source-consumption replay must attribute the "
                    "failure to the affected observation"
                )
            if wrapped_receipt.get("matched") is not False:
                errors.append(
                    "the source-consumption receipt must record the failed match"
                )

    hardgate = evaluate_hardgate(
        REPO_ROOT, _derived_evidence(p2p_pass="FAIL")
    )
    if hardgate["compound_gates"]["p2p_regression_preserved"]["status"] != "FAIL":
        errors.append(
            "an incompatible wire payload must fail p2p_regression_preserved"
        )
    if hardgate["status"] != "FAIL" or hardgate["reward_override"] != 0:
        errors.append(
            "an ordinary P2P format failure must produce hardgate FAIL and reward 0"
        )
    return errors


def _copy_equivalence_tests() -> Path:
    root = Path(tempfile.mkdtemp())
    for relative in ("test/test.js",):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ASSETS / "reference-tests" / relative, destination)
    return root


def _write_execution_report(
    equivalence: dict,
    destination: Path,
    *,
    omit_stable: str | None = None,
    failed_stable: str | None = None,
    status: str = "failed",
    assertion_delta: int = 0,
    drop_assertion_count: bool = False,
    title_only: bool = False,
) -> Path:
    files: dict[str, list[dict]] = {}
    total = 0
    for record in equivalence.values():
        for case in record.get("cases", []):
            stable_id = str(case.get("stable_id", ""))
            if stable_id == omit_stable:
                continue
            target_file = str(case.get("target_file", ""))
            for raw_title in case.get("target_titles", []):
                title = [str(item) for item in raw_title]
                row = {
                    "ancestorTitles": title[:-1],
                    "title": title[-1],
                    "status": status if stable_id == failed_stable else "passed",
                }
                if not drop_assertion_count:
                    row["assertionCount"] = max(
                        0, int(case.get("assertion_count", 0)) + assertion_delta
                    )
                files.setdefault(target_file, []).append(row)
                total += 1
    if title_only:
        test_results = [
            {"name": relative, "status": "passed"}
            for relative in sorted(files)
        ]
    else:
        test_results = [
            {"name": relative, "assertionResults": rows}
            for relative, rows in sorted(files.items())
        ]
    payload = {
        "numTotalTests": total,
        "numPassedTests": sum(
            row["status"] == "passed"
            for rows in files.values()
            for row in rows
        ),
        "testResults": test_results,
    }
    destination.write_text(json.dumps(payload), encoding="utf-8")
    return destination


def _test_tree_failure(
    equivalence: dict,
    relative: str,
    mutate: callable,
) -> list[str]:
    root = _copy_equivalence_tests()
    path = root / relative
    text = path.read_text(encoding="utf-8")
    mutated = mutate(text)
    if mutated == text:
        raise AssertionError(f"mutation did not change {relative}")
    path.write_text(mutated, encoding="utf-8")
    registered = int(equivalence.get(relative, {}).get("reference_test_count", 0) or 0)
    failures, _ = check_converted_tests(
        root,
        allowed_paths=set(equivalence),
        equivalence=equivalence,
        executed_tests={relative: registered} if registered else None,
    )
    return failures


def check_test_integrity_mutations() -> list[str]:
    """Early exits, weakened assertions and control-flow bypasses must fail."""

    errors: list[str] = []
    equivalence = load_equivalence(ASSETS / "source-test-equivalence.json")
    baseline = json.loads(
        (ASSETS / "source-test-baseline.json").read_text(encoding="utf-8")
    )
    relative = "test/test.js"
    callback = "\t\t\ttest(t.name, () => {\n"
    executed = {relative: 222}

    def assert_rejected(label: str, mutate) -> None:
        failures = _test_tree_failure(equivalence, relative, mutate)
        if not failures:
            errors.append(f"{label}: converted-test mutation was not rejected")

    assert_rejected(
        "early-return",
        lambda text: text.replace(callback, callback + "\t\t\treturn;\n", 1),
    )
    assert_rejected(
        "nested-block-early-return",
        lambda text: text.replace(callback, callback + "\t\t\t{ return; }\n", 1),
    )
    assert_rejected(
        "early-throw",
        lambda text: text.replace(
            callback,
            callback + "\t\t\tthrow new Error('assertions are unreachable');\n",
            1,
        ),
    )
    assert_rejected(
        "constant-false-branch",
        lambda text: text.replace(
            callback,
            callback
            + "\t\t\tif (false) {\n"
            + "\t\t\t\texpect(actual).toEqual(expected);\n"
            + "\t\t\t}\n",
            1,
        ),
    )
    assert_rejected(
        "nested-constant-false-branch",
        lambda text: text.replace(
            callback,
            callback
            + "\t\t\t{\n"
            + "\t\t\t\tif (false) {\n"
            + "\t\t\t\t\texpect(actual).toEqual(expected);\n"
            + "\t\t\t\t}\n"
            + "\t\t\t}\n",
            1,
        ),
    )
    assert_rejected(
        "assertion-helper-shadow",
        lambda text: "const expect = () => {};\n" + text,
    )
    assert_rejected(
        "swallowed-assertion",
        lambda text: text.replace(
            "expect(actual).toEqual(expected);",
            "try { expect(actual).toEqual(expected); } catch (error) {}",
            1,
        ),
    )
    assert_rejected(
        "literal-only",
        lambda text: "\n".join(
            line for line in text.splitlines() if "expect(" not in line
        )
        + "\n",
    )
    assert_rejected(
        "assertion-weakened",
        lambda text: text.replace(
            "expect(actual).toEqual(expected);",
            "expect(actual).toBeDefined();",
            1,
        ),
    )
    deletion_failures = _test_tree_failure(
        equivalence,
        relative,
        lambda text: text.replace("'woo!!!'", "'changed scenario value'", 1),
    )
    if not any(
        "retained scenario literal missing" in failure
        or "deleted or weakened" in failure
        for failure in deletion_failures
    ):
        errors.append(
            "deleting a protected scenario literal must still fail integrity"
        )

    # Legitimate conversion controls: inserted assertions and the protected
    # expected-throw try/catch idiom must be accepted; swallowing the catch is
    # still rejected.
    insertion_root = _copy_equivalence_tests()
    insertion_path = insertion_root / relative
    insertion_text = insertion_path.read_text(encoding="utf-8")
    anchor = "\t\t\texpect(actual).toEqual(expected);\n"
    if anchor not in insertion_text:
        errors.append("reference test lost the uneval assertion anchor")
    else:
        insertion_path.write_text(
            insertion_text.replace(
                anchor,
                anchor + "\t\t\texpect(typeof actual).toEqual('string');\n",
                1,
            ),
            encoding="utf-8",
        )
        insertion_failures, _ = check_converted_tests(
            insertion_root,
            allowed_paths=set(equivalence),
            equivalence=equivalence,
            executed_tests=executed,
        )
        if insertion_failures:
            errors.append(
                "a new assertion inserted next to protected assertions must "
                f"remain valid: {insertion_failures[:2]}"
            )

    idiom = (
        "\n\ntest('deferred error contract', () => {\n"
        "\ttry {\n"
        "\t\tparse('');\n"
        "\t\texpect.unreachable('should have thrown');\n"
        "\t} catch (error) {\n"
        "\t\texpect(error.message).toEqual('Unexpected end of JSON input');\n"
        "\t}\n"
        "});\n"
    )
    idiom_root = _copy_equivalence_tests()
    idiom_path = idiom_root / relative
    idiom_path.write_text(
        idiom_path.read_text(encoding="utf-8") + idiom, encoding="utf-8"
    )
    idiom_failures, _ = check_converted_tests(
        idiom_root,
        allowed_paths=set(equivalence),
        equivalence=equivalence,
        executed_tests=executed,
    )
    if idiom_failures:
        errors.append(
            "the protected expected-throw try/catch idiom must remain valid: "
            f"{idiom_failures[:2]}"
        )

    inverse_idiom = (
        "\n\ntest('inverse deferred error contract', () => {\n"
        "\ttry {\n"
        "\t\tparse('');\n"
        "\t} catch (error) {\n"
        "\t\texpect(error.message).toEqual('Unexpected end of JSON input');\n"
        "\t\treturn;\n"
        "\t}\n"
        "\texpect.unreachable('should have thrown');\n"
        "});\n"
    )
    inverse_root = _copy_equivalence_tests()
    inverse_path = inverse_root / relative
    inverse_path.write_text(
        inverse_path.read_text(encoding="utf-8") + inverse_idiom,
        encoding="utf-8",
    )
    inverse_failures, _ = check_converted_tests(
        inverse_root,
        allowed_paths=set(equivalence),
        equivalence=equivalence,
        executed_tests=executed,
    )
    if inverse_failures:
        errors.append(
            "the inverse expected-throw catch/return idiom must remain valid: "
            f"{inverse_failures[:2]}"
        )

    inverse_swallowed_root = _copy_equivalence_tests()
    inverse_swallowed_path = inverse_swallowed_root / relative
    inverse_swallowed_path.write_text(
        inverse_swallowed_path.read_text(encoding="utf-8")
        + inverse_idiom.replace(
            "\t\texpect(error.message).toEqual('Unexpected end of JSON input');\n",
            "",
        ),
        encoding="utf-8",
    )
    inverse_swallowed_failures, _ = check_converted_tests(
        inverse_swallowed_root,
        allowed_paths=set(equivalence),
        equivalence=equivalence,
        executed_tests=executed,
    )
    if not any(
        "control flow bypasses assertions" in failure
        for failure in inverse_swallowed_failures
    ):
        errors.append(
            "an inverse expected-throw catch/return without a caught-value "
            "assertion must still fail control-flow integrity"
        )

    swallowed_root = _copy_equivalence_tests()
    swallowed_path = swallowed_root / relative
    swallowed_path.write_text(
        swallowed_path.read_text(encoding="utf-8")
        + "\n\ntest('swallowed contract', () => {\n"
        + "\ttry {\n"
        + "\t\tparse('');\n"
        + "\t\texpect.unreachable('should have thrown');\n"
        + "\t} catch (error) {}\n"
        + "});\n",
        encoding="utf-8",
    )
    swallowed_failures, _ = check_converted_tests(
        swallowed_root,
        allowed_paths=set(equivalence),
        equivalence=equivalence,
        executed_tests=executed,
    )
    if not any(
        "control flow bypasses assertions" in failure
        for failure in swallowed_failures
    ):
        errors.append(
            "an expected-throw try/catch with an empty catch must still fail "
            "control-flow integrity"
        )

    acceptable_root = _copy_equivalence_tests()
    acceptable_path = acceptable_root / relative
    acceptable_path.write_text(
        acceptable_path.read_text(encoding="utf-8").replace(
            "expect(value.size).toBe(2);",
            "expect(value.size).toEqual(2);",
            1,
        ),
        encoding="utf-8",
    )
    acceptable_failures, _ = check_converted_tests(
        acceptable_root,
        allowed_paths=set(equivalence),
        equivalence=equivalence,
        executed_tests=executed,
    )
    if acceptable_failures:
        errors.append(
            "the accepted primitive matcher equivalence "
            f"(toBe -> toEqual) must remain valid: {acceptable_failures[:2]}"
        )

    # The instruction requires a single local helper module, not a namespace
    # import or a particular source spelling.  Named helper imports, trailing
    # call commas, equivalent primitive equality, and UTF-16 escape spelling
    # must retain their protected semantics.
    named_root = Path(tempfile.mkdtemp())
    named_test = named_root / "named.test.js"
    named_test.write_text(
        "import { equal, is, throws } from './assertions.js';\n"
        "test('\\uD834\\uDF06', () => {\n"
        "  equal(actual, expected);\n"
        "  is(Object.getPrototypeOf(value), null);\n"
        "  throws(() => operation(), (error) => error.ok,);\n"
        "});\n",
        encoding="utf-8",
    )
    named_equivalence = {
        "named.test.js": {
            "assertion_count": 3,
            "test_count": 1,
            "source_assertions": {
                "assert.equal(actual,expected)": 1,
                "assert.equal(Object.getPrototypeOf(value),null)": 1,
                "assert.throws(()=>operation(),(error)=>error.ok)": 1,
            },
            "required_literal_tokens": ["'𝌆'"],
            "comment_only_tokens": [],
            "cases": [],
        }
    }
    named_failures, _ = check_converted_tests(
        named_root,
        allowed_paths={"named.test.js"},
        equivalence=named_equivalence,
        executed_tests={"named.test.js": 1},
    )
    if named_failures:
        errors.append(
            "named local assertion imports and equivalent JavaScript spelling "
            f"must remain valid: {named_failures[:2]}"
        )

    adapter_root = Path(tempfile.mkdtemp())
    (adapter_root / "adapter.test.js").write_text(
        "const register = (name, fn) => fn();\n"
        "for (const name of ['alpha', 'beta', 'gamma', 'delta'])\n"
        "\tregister(name, () => expect(name.length).toBeGreaterThan(0));\n",
        encoding="utf-8",
    )
    adapter_equivalence = {
        "adapter.test.js": {
            "assertion_count": 1,
            "test_count": 4,
            "cases": [],
        }
    }
    adapter_static_failures, _ = check_converted_tests(
        adapter_root,
        allowed_paths={"adapter.test.js"},
        equivalence=adapter_equivalence,
    )
    if not any(
        "test declarations" in failure for failure in adapter_static_failures
    ):
        errors.append(
            "an adapter file without an execution report must still fail the "
            "protected test floor"
        )
    adapter_executed_failures, _ = check_converted_tests(
        adapter_root,
        allowed_paths={"adapter.test.js"},
        equivalence=adapter_equivalence,
        executed_tests={"adapter.test.js": 4},
    )
    if adapter_executed_failures:
        errors.append(
            "a suite adapter proven by the evaluator report must satisfy the "
            f"protected test floor: {adapter_executed_failures[:2]}"
        )

    passing_root = _copy_equivalence_tests()
    passing_report = _write_execution_report(
        equivalence, passing_root / "vitest-report.json"
    )
    execution_failures, _ = check_executed_cases(
        equivalence, report=passing_report
    )
    if execution_failures:
        errors.append(
            "the complete execution fixture must pass the mapped-case check: "
            f"{execution_failures[:2]}"
        )

    first_case = str(equivalence[relative]["cases"][0]["stable_id"])
    missing_report = _write_execution_report(
        equivalence,
        passing_root / "missing-report.json",
        omit_stable=first_case,
    )
    missing_failures, _ = check_executed_cases(
        equivalence, report=missing_report
    )
    if not any(first_case in failure for failure in missing_failures):
        errors.append(
            "a mapped source case that never executed must fail the execution check"
        )

    failed_report = _write_execution_report(
        equivalence,
        passing_root / "failed-report.json",
        failed_stable=first_case,
        status="skipped",
    )
    failed_failures, _ = check_executed_cases(
        equivalence, report=failed_report
    )
    if not any(first_case in failure for failure in failed_failures):
        errors.append(
            "a skipped or timed-out mapped case must fail the execution check"
        )
    absent_failures, _ = check_executed_cases(equivalence, report=None)
    if not absent_failures:
        errors.append("a missing execution report must fail the mapped-case check")

    execution_mutations = {
        "title-only": _write_execution_report(
            equivalence,
            passing_root / "title-only-report.json",
            title_only=True,
        ),
        "missing-assertion-count": _write_execution_report(
            equivalence,
            passing_root / "missing-assertion-count-report.json",
            drop_assertion_count=True,
        ),
        "insufficient-assertion-count": _write_execution_report(
            equivalence,
            passing_root / "insufficient-assertion-count-report.json",
            assertion_delta=-1,
        ),
    }
    full_config = json.loads(
        (HERE.parent / "config.json").read_text(encoding="utf-8")
    )
    ordinary_receipt = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": "target-build",
        "index": 1,
        "command": ["pnpm", "run", "build"],
        "returncode": 0,
        "timed_out": False,
        "report_present": False,
    }
    for label, mutated_report in execution_mutations.items():
        mutated_failures, _ = check_executed_cases(
            equivalence, report=mutated_report
        )
        if not mutated_failures:
            errors.append(
                f"a {label} execution report must fail the mapped-case check"
            )
        mutated_evidence = build_hardgate_evidence(
            _copy_equivalence_tests(),
            config=full_config,
            observations=[],
            observation_errors=[],
            receipts=[ordinary_receipt],
            patch_applicable=True,
            source_test_baseline=baseline,
            isolation={"status": "PASS", "violations": [], "evidence": []},
            execution_report=mutated_report,
        )
        if mutated_evidence["tests_not_tampered"]["status"] != "PASS":
            errors.append(
                f"a {label} execution report alone must not be classified as "
                "protected-test tampering"
            )
        diagnostics = mutated_evidence["tests_not_tampered"]["evidence"]
        gate_failures = diagnostics.get("converted_test_gate_failures", [])
        for failure in mutated_failures:
            if failure not in gate_failures:
                errors.append(
                    f"a {label} execution report must retain {failure!r} as a "
                    "converted-test gate failure"
                )
                break
        if not diagnostics.get("execution_report_failures"):
            errors.append(
                f"a {label} execution report must be recorded as execution "
                "evidence failure"
            )
        if mutated_evidence["observation_contract_valid"]["status"] != "FAIL":
            errors.append(
                f"a {label} execution report must fail observation_contract_valid"
            )
        mutated_hardgate = evaluate_hardgate(REPO_ROOT, mutated_evidence)
        if mutated_hardgate["compound_gates"]["test_integrity"]["status"] != "PASS":
            errors.append(
                f"a {label} execution report must not fail test_integrity"
            )
        if (
            mutated_hardgate["compound_gates"]["candidate_valid"]["status"]
            != "FAIL"
        ):
            errors.append(
                f"a {label} execution report must fail candidate_valid"
            )
        if (
            mutated_hardgate["status"] != "FAIL"
            or mutated_hardgate["cheating"]
            or mutated_hardgate["cheat_level"] != "L0"
            or mutated_hardgate["reward_override"] != 0
        ):
            errors.append(
                f"a {label} execution failure must produce hardgate FAIL, L0 "
                "and ordinary reward 0"
            )

    missing_report_evidence = build_hardgate_evidence(
        _copy_equivalence_tests(),
        config=full_config,
        observations=[],
        observation_errors=[],
        receipts=[ordinary_receipt],
        patch_applicable=True,
        source_test_baseline=baseline,
        isolation={"status": "PASS", "violations": [], "evidence": []},
        execution_report=None,
    )
    if missing_report_evidence["tests_not_tampered"]["status"] != "PASS":
        errors.append(
            "a missing execution report must not be classified as protected-test "
            "tampering"
        )
    missing_diagnostics = missing_report_evidence["tests_not_tampered"]["evidence"]
    if not missing_diagnostics.get("execution_report_failures"):
        errors.append(
            "a missing execution report must be recorded as execution evidence "
            "failure"
        )
    if missing_diagnostics.get("converted_test_failures"):
        errors.append(
            "a missing execution report must not enter converted_test_failures"
        )
    if missing_report_evidence["observation_contract_valid"]["status"] != "FAIL":
        errors.append(
            "a missing execution report must fail observation_contract_valid"
        )
    missing_hardgate = evaluate_hardgate(REPO_ROOT, missing_report_evidence)
    if missing_hardgate["compound_gates"]["test_integrity"]["status"] != "PASS":
        errors.append(
            "a missing execution report must not fail the test_integrity gate"
        )
    if (
        missing_hardgate["compound_gates"]["candidate_valid"]["status"] != "FAIL"
    ):
        errors.append(
            "a missing execution report must fail candidate_valid as an ordinary "
            "execution failure"
        )
    if (
        missing_hardgate["status"] != "FAIL"
        or missing_hardgate["cheating"]
        or missing_hardgate["cheat_level"] != "L0"
        or missing_hardgate["reward_override"] != 0
    ):
        errors.append(
            "a missing execution report must produce hardgate FAIL, L0 and "
            "ordinary reward 0"
        )

    invalid_report = Path(tempfile.mkdtemp()) / "invalid-report.json"
    invalid_report.write_text("{", encoding="utf-8")
    empty_report = Path(tempfile.mkdtemp()) / "empty-report.json"
    empty_report.write_text(
        '{"numTotalTests": 0, "testResults": []}\n', encoding="utf-8"
    )
    empty_report_evidence = build_hardgate_evidence(
        _copy_equivalence_tests(),
        config=full_config,
        observations=[],
        observation_errors=[],
        receipts=[ordinary_receipt],
        patch_applicable=True,
        source_test_baseline=baseline,
        isolation={"status": "PASS", "violations": [], "evidence": []},
        execution_report=empty_report,
    )
    if empty_report_evidence["tests_not_tampered"]["status"] != "PASS":
        errors.append(
            "an empty execution report must not be classified as protected-test "
            "tampering"
        )
    if empty_report_evidence["observation_contract_valid"]["status"] != "FAIL":
        errors.append(
            "an empty execution report must fail observation_contract_valid"
        )
    invalid_report_evidence = build_hardgate_evidence(
        _copy_equivalence_tests(),
        config=full_config,
        observations=[],
        observation_errors=[],
        receipts=[ordinary_receipt],
        patch_applicable=True,
        source_test_baseline=baseline,
        isolation={"status": "PASS", "violations": [], "evidence": []},
        execution_report=invalid_report,
    )
    if invalid_report_evidence["tests_not_tampered"]["status"] != "PASS":
        errors.append(
            "an unreadable execution report must not be classified as "
            "protected-test tampering"
        )
    if invalid_report_evidence["observation_contract_valid"]["status"] != "FAIL":
        errors.append(
            "an unreadable execution report must fail observation_contract_valid"
        )
    invalid_hardgate = evaluate_hardgate(REPO_ROOT, invalid_report_evidence)
    if (
        invalid_hardgate["status"] != "FAIL"
        or invalid_hardgate["cheating"]
        or invalid_hardgate["cheat_level"] != "L0"
        or invalid_hardgate["reward_override"] != 0
    ):
        errors.append(
            "an unreadable execution report must produce hardgate FAIL, L0 and "
            "ordinary reward 0"
        )

    candidate_skip_root = _copy_equivalence_tests()
    candidate_skip = candidate_skip_root / "test/qa-candidate-skip.test.js"
    candidate_skip.parent.mkdir(parents=True, exist_ok=True)
    candidate_skip.write_text(
        "import { test } from 'vitest';\n\n"
        "test.skip('qa candidate-owned skip control', () => {\n"
        "  throw new Error('candidate-owned skipped test');\n"
        "});\n",
        encoding="utf-8",
    )
    candidate_skip_evidence = build_hardgate_evidence(
        candidate_skip_root,
        config=full_config,
        observations=[],
        observation_errors=[],
        receipts=[ordinary_receipt],
        patch_applicable=True,
        source_test_baseline=baseline,
        isolation={"status": "PASS", "violations": [], "evidence": []},
        execution_report=passing_report,
    )
    if candidate_skip_evidence["tests_not_tampered"]["status"] != "PASS":
        errors.append(
            "a candidate-owned skip must not be classified as protected-test "
            "tampering"
        )
    skip_atom = candidate_skip_evidence["no_new_skip_or_xfail"]
    if skip_atom["status"] != "FAIL":
        errors.append("a candidate-owned skip must fail no_new_skip_or_xfail")
    skip_delta = skip_atom["evidence"].get("candidate_owned_delta", {})
    if skip_delta.get("disabled") != 1:
        errors.append(
            "a candidate-owned skip must be counted in candidate_owned_delta"
        )
    candidate_skip_hardgate = evaluate_hardgate(REPO_ROOT, candidate_skip_evidence)
    if (
        candidate_skip_hardgate["status"] != "FAIL"
        or candidate_skip_hardgate["cheating"]
        or candidate_skip_hardgate["cheat_level"] != "L0"
        or candidate_skip_hardgate["reward_override"] != 0
    ):
        errors.append(
            "a candidate-owned skip must produce hardgate FAIL, L0 and reward 0"
        )

    early_root = _copy_equivalence_tests()
    early_path = early_root / relative
    early_path.write_text(
        early_path.read_text(encoding="utf-8").replace(
            callback, callback + "\t\t\t{ return; }\n", 1
        ),
        encoding="utf-8",
    )
    evidence = build_hardgate_evidence(
        early_root,
        config=full_config,
        observations=[],
        observation_errors=[],
        receipts=[ordinary_receipt],
        patch_applicable=True,
        source_test_baseline=baseline,
        isolation={"status": "PASS", "violations": [], "evidence": []},
        execution_report=passing_report,
    )
    if evidence["tests_not_tampered"]["status"] != "FAIL":
        errors.append(
            "an early return in a protected callback must fail tests_not_tampered"
        )
    hardgate = evaluate_hardgate(REPO_ROOT, evidence)
    if hardgate["compound_gates"]["test_integrity"]["status"] != "FAIL":
        errors.append(
            "a nested-block early return in a protected callback must fail "
            "test_integrity"
        )
    if (
        hardgate["status"] != "FAIL"
        or hardgate["cheat_level"] != "L1"
        or hardgate["reward_override"] != -1
    ):
        errors.append(
            "a protected-test tamper must produce hardgate FAIL, L1 and "
            "cheating reward -1"
        )
    return errors


_REQUIRED_ALLOWED_PROBES = {
    "read-candidate-workspace",
    "write-candidate-workspace",
    "read-public-protocol",
    "read-allowed-test-patch",
}

_REQUIRED_DENIED_PROBES = {
    "read-public-evaluation-assets",
    "read-hidden-harness",
    "read-hidden-shared-harness",
    "read-hidden-fixtures",
    "read-hidden-source-baseline",
    "read-hidden-test-evidence",
    "read-hidden-source-runtime",
    "read-hidden-target-runtime",
    "read-hidden-verifier",
    "read-hidden-through-symlink",
    "readlink-hidden-path",
    "openat-hidden-path",
    "dynamic-import-hidden-harness",
    "create-require-hidden-module",
    "read-hidden-via-dependency-loader",
    "execve-child-process",
    "clone-worker-thread",
    "write-verifier-output",
    "write-final-verdict",
    "write-protected-tests",
    "write-hidden-evaluation-assets",
    "write-candidate-log-report",
    "outbound-network",
}


def check_replacer_protocol_reachability() -> list[str]:
    """Replacer gates must survive the out-of-process candidate protocol.

    The scored evaluator never imports candidate code in-process; it forwards
    operations through ``candidate-runner.mjs``.  A callback replacer cannot be
    serialised, so the protocol has to execute a fixed scenario candidate-side.
    This check proves the wiring with a stub candidate: if the proxy drops the
    replacer, the tagged-template gate observes ``tag_received=false`` even for
    a candidate that supports the 6.0.0 signature.
    """

    errors: list[str] = []
    node = shutil.which("node")
    if node is None:
        return ["node is required to exercise the replacer protocol"]
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        candidate = root / "candidate"
        candidate.mkdir()
        (candidate / "package.json").write_text(
            json.dumps({"name": "replacer-protocol-stub", "version": "0.0.0", "type": "module"}),
            encoding="utf-8",
        )
        # A deliberately non-compliant stub: it supports the replacer signature
        # but cannot rebuild the value, so the gate must FAIL while still
        # observing that a tag function reached the candidate.
        (candidate / "index.js").write_text(
            "export function uneval(value, replacer) {\n"
            "\tif (typeof replacer !== 'function') return JSON.stringify(value);\n"
            "\tconst js = (strings, ...values) =>\n"
            "\t\tstrings.raw.reduce(\n"
            "\t\t\t(acc, chunk, index) => acc + chunk + (index < values.length ? String(values[index]) : ''),\n"
            "\t\t\t''\n"
            "\t\t);\n"
            "\tconst replaced = replacer(value, js);\n"
            "\treturn typeof replaced === 'string' ? replaced : JSON.stringify(value);\n"
            "}\n",
            encoding="utf-8",
        )
        logs = root / "logs"
        logs.mkdir()
        try:
            public = harbor_entry._prepare_public_assets(ASSETS, logs)
        except Exception as exc:  # noqa: BLE001 - surface the wiring failure
            return [f"could not prepare the public replacer protocol assets: {exc}"]
        report = root / "report.json"
        environment = {
            **os.environ,
            "MIGRATION_REPO_ROOT": str(candidate),
            "MIGRATION_PUBLIC_ASSETS": str(public),
            "MIGRATION_EVAL_SANDBOX": str(ASSETS / "verifier" / "eval-expression.mjs"),
            "MIGRATION_CANDIDATE_HOME": str(root / "home"),
        }
        (root / "home").mkdir()
        completed = subprocess.run(
            [node, str(ASSETS / "verifier" / "evaluator-runner.mjs"), str(report)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120.0,
            env=environment,
            check=False,
        )
        if not report.is_file():
            return [
                "the replacer protocol run produced no report: "
                + (completed.stderr or completed.stdout or "no output")[-800:]
            ]
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"the replacer protocol report is invalid: {exc}"]
        rows = {
            row.get("id"): row
            for row in payload.get("observations", [])
            if isinstance(row, Mapping)
        }
        for gate in ("F2P-008", "F2P-009", "F2P-010", "F2P-011"):
            row = rows.get(gate)
            if row is None:
                errors.append(f"{gate} is missing from the replacer protocol report")
                continue
            if row.get("status") != "FAIL":
                errors.append(
                    f"{gate} must not pass a candidate that cannot rebuild values"
                )
        tagged = rows.get("F2P-008")
        behavior = (tagged or {}).get("value", {})
        behavior = behavior.get("value", {}) if isinstance(behavior, Mapping) else {}
        behavior = behavior.get("behavior", {}) if isinstance(behavior, Mapping) else {}
        if not (isinstance(behavior, Mapping) and behavior.get("tag_received") is True):
            errors.append(
                "the candidate protocol must deliver the replacer tag to the "
                "candidate; F2P-008 observed tag_received=false"
            )
    return errors


def check_isolation_boundary() -> list[str]:
    """Hidden assets and trusted report writers must stay inaccessible."""

    errors: list[str] = []
    with tempfile.TemporaryDirectory() as raw:
        logs = Path(raw) / "logs"
        logs.mkdir()
        with mock.patch.object(harbor_entry.os, "access", return_value=False):
            public = harbor_entry._prepare_public_assets(ASSETS, logs)
        exposed = sorted(
            path.relative_to(public).as_posix()
            for path in public.rglob("*")
            if path.is_file() or path.is_symlink()
        )
        expected_public = sorted(harbor_entry._PUBLIC_ASSET_NAMES)
        if exposed != expected_public:
            errors.append(
                "the candidate must only receive the public protocol files: "
                f"{exposed}"
            )
        if (public / "shared").exists() or (public / "fixtures").exists():
            errors.append(
                "hidden harnesses or fixtures must not be copied into the "
                "candidate-readable public assets"
            )

        candidate = Path(tempfile.mkdtemp())
        no_identity = harbor_entry._isolation_probe(
            candidate=candidate,
            public_assets=public,
            identity=None,
            timeout=1.0,
            environment={"PATH": os.environ.get("PATH", "")},
        )
        if no_identity.get("status") != "FAIL" or not no_identity.get(
            "required_failures"
        ):
            errors.append(
                "a verifier without a distinct candidate identity must fail "
                "closed"
            )

        def fake_run(command, *args, **kwargs):  # noqa: ANN001, ANN002
            del args, kwargs
            return harbor_entry.subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(
            harbor_entry.subprocess, "run", side_effect=fake_run
        ):
            probe = harbor_entry._isolation_probe(
                candidate=candidate,
                public_assets=public,
                identity=(4242, 4242),
                timeout=1.0,
                environment={
                    "PATH": os.environ.get("PATH", ""),
                    "MIGRATION_EVALUATOR_NODE_MODULES": "/opt/migration/dependencies/node_modules",
                },
                hidden_root=Path("/tests"),
                verifier_root=Path("/logs/verifier"),
                candidate_logs=Path("/logs/candidate"),
                candidate_home=Path("/logs/candidate/home"),
            )
        checks = {
            str(row.get("name")): row
            for row in probe.get("checks", [])
            if isinstance(row, dict)
        }
        missing = sorted(_REQUIRED_DENIED_PROBES - set(checks))
        if missing:
            errors.append(f"isolation probe is missing checks: {missing}")
        missing_allowed = sorted(_REQUIRED_ALLOWED_PROBES - set(checks))
        if missing_allowed:
            errors.append(
                f"isolation probe is missing allowed capability checks: {missing_allowed}"
            )
        for name in _REQUIRED_ALLOWED_PROBES & set(checks):
            if checks[name].get("expected") != "allowed":
                errors.append(f"{name}: must be an allowed capability")
            if checks[name].get("ok") is not True:
                errors.append(f"{name}: a required allowed capability must pass")
        for name in _REQUIRED_DENIED_PROBES & set(checks):
            if checks[name].get("expected") != "denied":
                errors.append(f"{name}: must be a denied capability")
            if checks[name].get("ok") is not False:
                errors.append(
                    f"{name}: a successful forbidden capability must be a violation"
                )
        if probe.get("status") != "FAIL":
            errors.append("a successful forbidden capability must fail isolation")
        if not _REQUIRED_DENIED_PROBES.issubset(set(probe.get("violations", []))):
            errors.append(
                "every forbidden capability must be recorded in isolation violations"
            )

        evidence = build_hardgate_evidence(
            candidate,
            config=_fixture_config(),
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline={"files": {}, "marker_counts": {}},
            isolation=probe,
        )
        if evidence["isolation_enforced"]["status"] != "FAIL":
            errors.append("isolation violations must fail isolation_enforced")
        if evidence["anti_cheating_pass"]["status"] != "FAIL":
            errors.append("isolation violations must fail anti_cheating_pass")
        hardgate = evaluate_hardgate(REPO_ROOT, evidence)
        if (
            hardgate["compound_gates"]["evaluation_environment_integrity"][
                "status"
            ]
            != "FAIL"
        ):
            errors.append(
                "isolation violations must fail the evaluation environment gate"
            )
        if not hardgate["cheating"] or hardgate["cheat_level"] != "L2":
            errors.append(
                "hidden-answer/subprocess/network isolation violations must be L2 "
                f"cheating, got {hardgate['cheat_level']!r}"
            )

        l1_probe = {
            "status": "FAIL",
            "violations": ["write-verifier-output"],
            "evidence": [],
            "checks": [
                {
                    "name": "write-verifier-output",
                    "expected": "denied",
                    "ok": False,
                }
            ],
        }
        l1_evidence = build_hardgate_evidence(
            candidate,
            config=_fixture_config(),
            observations=[],
            observation_errors=[],
            receipts=[],
            patch_applicable=True,
            source_test_baseline={"files": {}, "marker_counts": {}},
            isolation=l1_probe,
        )
        l1_hardgate = evaluate_hardgate(REPO_ROOT, l1_evidence)
        if not l1_hardgate["cheating"] or l1_hardgate["cheat_level"] != "L1":
            errors.append(
                "protected-output tampering must remain L1 cheating, got "
                f"{l1_hardgate['cheat_level']!r}"
            )
        if l1_hardgate["reward_override"] != -1:
            errors.append("L1 isolation tampering must produce reward_override -1")

    tampered = Path(tempfile.mkdtemp())
    (tampered / "index.js").write_text(
        "const hidden = '/tests/assets/source-baseline-outputs.json';\n"
        "const report = '/logs/verifier/result.json';\n"
        "const marker = 'HARDCODED_VERIFIER_PASS';\n"
        "export default [hidden, report, marker];\n",
        encoding="utf-8",
    )
    # A forged report is a non-code artifact: the marker search must still see
    # its JSON string even though JavaScript string literals are inert.
    (tampered / "forged-report.json").write_text(
        '{"verdict":"HARDCODED_VERIFIER_PASS"}\n',
        encoding="utf-8",
    )
    tampered_evidence = build_hardgate_evidence(
        tampered,
        config=_fixture_config(),
        observations=[],
        observation_errors=[],
        receipts=[],
        patch_applicable=True,
        source_test_baseline={"files": {}, "marker_counts": {}},
        isolation={"status": "PASS", "violations": [], "evidence": []},
    )
    if tampered_evidence["anti_cheating_pass"]["status"] != "FAIL":
        errors.append(
            "referencing hidden observations or the verifier report must fail "
            "anti_cheating_pass"
        )
    anti = tampered_evidence["anti_cheating_pass"].get("evidence", {})
    if not anti.get("hidden_reference_hits"):
        errors.append("hidden evaluator references must be recorded as hits")
    if not anti.get("report_marker_hits"):
        errors.append("forged pass markers in a candidate report must be recorded")

    allowed_patch = Path(tempfile.mkdtemp())
    (allowed_patch / "index.js").write_text(
        "export const patchPath = '/tests/verifier/test.patch';\n",
        encoding="utf-8",
    )
    allowed_evidence = build_hardgate_evidence(
        allowed_patch,
        config=_fixture_config(),
        observations=[],
        observation_errors=[],
        receipts=[],
        patch_applicable=True,
        source_test_baseline={"files": {}, "marker_counts": {}},
        isolation={"status": "PASS", "violations": [], "evidence": []},
    )
    allowed_anti = allowed_evidence["anti_cheating_pass"].get("evidence", {})
    if allowed_anti.get("hidden_reference_hits"):
        errors.append(
            "a reference to the explicitly allowed test.patch must not be a "
            "hidden-reference hit"
        )

    comment_only = Path(tempfile.mkdtemp())
    (comment_only / "index.js").write_text(
        "// HARDCODED_VERIFIER_PASS (inert marker fixture)\n"
        "export const value = 1;\n",
        encoding="utf-8",
    )
    comment_evidence = build_hardgate_evidence(
        comment_only,
        config=_fixture_config(),
        observations=[],
        observation_errors=[],
        receipts=[],
        patch_applicable=True,
        source_test_baseline={"files": {}, "marker_counts": {}},
        isolation={"status": "PASS", "violations": [], "evidence": []},
    )
    comment_anti = comment_evidence["anti_cheating_pass"]
    if comment_anti["status"] != "PASS":
        errors.append(
            "a forbidden marker in a JavaScript comment must remain inert"
        )
    if comment_anti["evidence"].get("report_marker_hits"):
        errors.append(
            "a comment-only marker must not be recorded as a report marker hit"
        )

    string_only = Path(tempfile.mkdtemp())
    (string_only / "index.js").write_text(
        "const marker = 'HARDCODED_VERIFIER_PASS';\n"
        "export default marker;\n",
        encoding="utf-8",
    )
    string_evidence = build_hardgate_evidence(
        string_only,
        config=_fixture_config(),
        observations=[],
        observation_errors=[],
        receipts=[],
        patch_applicable=True,
        source_test_baseline={"files": {}, "marker_counts": {}},
        isolation={"status": "PASS", "violations": [], "evidence": []},
    )
    string_anti = string_evidence["anti_cheating_pass"]
    if string_anti["status"] != "PASS":
        errors.append(
            "a forbidden marker in a JavaScript string literal must remain inert"
        )
    if string_anti["evidence"].get("report_marker_hits"):
        errors.append(
            "a string-only marker must not be recorded as a report marker hit"
        )

    executable_marker = Path(tempfile.mkdtemp())
    (executable_marker / "index.js").write_text(
        "export const HARDCODED_VERIFIER_PASS = true;\n",
        encoding="utf-8",
    )
    executable_evidence = build_hardgate_evidence(
        executable_marker,
        config=_fixture_config(),
        observations=[],
        observation_errors=[],
        receipts=[],
        patch_applicable=True,
        source_test_baseline={"files": {}, "marker_counts": {}},
        isolation={"status": "PASS", "violations": [], "evidence": []},
    )
    executable_anti = executable_evidence["anti_cheating_pass"]
    if executable_anti["status"] != "FAIL":
        errors.append(
            "a forbidden marker in executable JavaScript must fail anti_cheating_pass"
        )
    if not executable_anti["evidence"].get("report_marker_hits"):
        errors.append(
            "an executable marker must be recorded as a report marker hit"
        )
    return errors


def _dependency_fixture(
    root: Path,
    actual_version: str = "1.0.0",
    *,
    manifest_version: str = "1.0.0",
    lock_specifier: str | None = None,
    lock_version: str = "1.0.0",
) -> None:
    (root / ".pnpm").mkdir(parents=True, exist_ok=True)
    virtual = (
        root / "node_modules" / ".pnpm" / f"left-pad@{lock_version}" / "node_modules"
    )
    virtual.mkdir(parents=True)
    (virtual / "left-pad").mkdir()
    (virtual / "left-pad" / "package.json").write_text(
        json.dumps(
            {"name": "left-pad", "version": actual_version, "main": "index.js"}
        ),
        encoding="utf-8",
    )
    (virtual / "left-pad" / "index.js").write_text(
        "export const leftPad = (value) => value;\n", encoding="utf-8"
    )
    transitive = (
        root
        / "node_modules"
        / ".pnpm"
        / "right-pad@1.0.0"
        / "node_modules"
        / "right-pad"
    )
    transitive.mkdir(parents=True)
    (transitive / "package.json").write_text(
        json.dumps(
            {"name": "right-pad", "version": "1.0.0", "main": "index.js"}
        ),
        encoding="utf-8",
    )
    (transitive / "index.js").write_text(
        "export const rightPad = (value) => value;\n", encoding="utf-8"
    )
    (virtual / "right-pad").symlink_to(
        Path("..") / ".." / "right-pad@1.0.0" / "node_modules" / "right-pad"
    )
    (root / "node_modules" / "left-pad").symlink_to(
        Path(".pnpm") / f"left-pad@{lock_version}" / "node_modules" / "left-pad"
    )
    (root / "node_modules" / ".modules.yaml").write_text(
        "layoutVersion: 5\npackageManager: pnpm@10.0.0\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "dependency-audit-fixture",
                "version": "1.0.0",
                "devDependencies": {"left-pad": manifest_version},
            }
        ),
        encoding="utf-8",
    )
    (root / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n"
        "importers:\n"
        "  .:\n"
        "    devDependencies:\n"
        "      left-pad:\n"
        f"        specifier: {lock_specifier or manifest_version}\n"
        f"        version: {lock_version}\n"
        "packages:\n"
        f"  left-pad@{lock_version}:\n"
        "    resolution: {integrity: sha512-fixture}\n"
        "  right-pad@1.0.0:\n"
        "    resolution: {integrity: sha512-fixture-right}\n"
        "snapshots:\n"
        f"  left-pad@{lock_version}:\n"
        "    dependencies:\n"
        "      right-pad: 1.0.0\n"
        "  right-pad@1.0.0: {}\n",
        encoding="utf-8",
    )


def check_dependency_graph_is_recomputed() -> list[str]:
    errors: list[str] = []
    root = Path(tempfile.mkdtemp())
    _dependency_fixture(root)
    passing = audit_dependency_graph(root)
    if passing.get("status") != "PASS":
        errors.append(
            "a consistent manifest/lockfile/installed graph must pass the "
            f"dependency audit: {passing.get('errors')}"
        )
    if not isinstance(passing.get("digest"), str):
        errors.append("a passing dependency audit must record a graph digest")
    mismatch = Path(tempfile.mkdtemp())
    _dependency_fixture(mismatch, actual_version="2.0.0")
    failing = audit_dependency_graph(mismatch)
    if failing.get("status") != "FAIL":
        errors.append("declared/locked/installed version mismatch must fail the audit")

    manifest_mismatch = Path(tempfile.mkdtemp())
    _dependency_fixture(
        manifest_mismatch, manifest_version="2.0.0", lock_specifier="1.0.0"
    )
    manifest_result = audit_dependency_graph(manifest_mismatch)
    if manifest_result.get("status") != "FAIL":
        errors.append(
            "a manifest specifier that differs from the lockfile must fail the audit"
        )

    lock_mismatch = Path(tempfile.mkdtemp())
    _dependency_fixture(lock_mismatch, lock_version="2.0.0")
    lock_result = audit_dependency_graph(lock_mismatch)
    if lock_result.get("status") != "FAIL":
        errors.append(
            "a lockfile resolution that differs from installed modules must "
            "fail the audit"
        )

    direct_mutation = Path(tempfile.mkdtemp())
    _dependency_fixture(direct_mutation)
    (
        direct_mutation
        / "node_modules"
        / ".pnpm"
        / "left-pad@1.0.0"
        / "node_modules"
        / "left-pad"
        / "index.js"
    ).write_text("export const leftPad = () => 'tampered';\n", encoding="utf-8")
    direct_result = audit_dependency_graph(direct_mutation)
    if direct_result.get("status") != "PASS":
        errors.append(
            "a modified direct package file must be detected by the final "
            f"graph audit rather than the single-install audit: {direct_result.get('errors')}"
        )
    if direct_result.get("digest") == passing.get("digest"):
        errors.append(
            "modifying a direct package file must change the graph digest"
        )

    transitive_mutation = Path(tempfile.mkdtemp())
    _dependency_fixture(transitive_mutation)
    (
        transitive_mutation
        / "node_modules"
        / ".pnpm"
        / "right-pad@1.0.0"
        / "node_modules"
        / "right-pad"
        / "index.js"
    ).write_text("export const rightPad = () => 'tampered';\n", encoding="utf-8")
    transitive_result = audit_dependency_graph(transitive_mutation)
    if transitive_result.get("digest") == passing.get("digest"):
        errors.append(
            "modifying a transitive package file must change the graph digest"
        )

    metadata_mutation = Path(tempfile.mkdtemp())
    _dependency_fixture(metadata_mutation)
    (metadata_mutation / "node_modules" / ".modules.yaml").write_text(
        "layoutVersion: 5\npackageManager: pnpm@0.0.0-tampered\n",
        encoding="utf-8",
    )
    metadata_result = audit_dependency_graph(metadata_mutation)
    if metadata_result.get("digest") == passing.get("digest"):
        errors.append(
            "modifying pnpm virtual-store metadata must change the graph digest"
        )

    phase_root = Path(tempfile.mkdtemp())
    _dependency_fixture(phase_root)
    initial_receipt = harbor_entry._dependency_graph_receipt(
        phase_root, phase="initial", index=1
    )
    if not initial_receipt.get("valid"):
        errors.append(
            "the initial dependency graph phase must accept a consistent fixture"
        )
    (
        phase_root
        / "node_modules"
        / ".pnpm"
        / "left-pad@1.0.0"
        / "node_modules"
        / "left-pad"
        / "package.json"
    ).write_text(
        json.dumps(
            {"name": "left-pad", "version": "2.0.0", "main": "index.js"}
        ),
        encoding="utf-8",
    )
    version_receipt = harbor_entry._dependency_graph_receipt(
        phase_root,
        phase="post-build",
        index=2,
        previous=initial_receipt,
    )
    if version_receipt.get("valid") or version_receipt.get("returncode") != 1:
        errors.append(
            "a build-time direct package version rewrite must invalidate the "
            "post-build dependency audit"
        )
    version_graph = version_receipt.get("resolved_graph", {})
    if (
        not isinstance(version_graph, Mapping)
        or version_graph.get("status") != "FAIL"
    ):
        errors.append(
            "a build-time direct package version rewrite must record a FAIL graph"
        )

    tree_phase_root = Path(tempfile.mkdtemp())
    _dependency_fixture(tree_phase_root)
    tree_initial = harbor_entry._dependency_graph_receipt(
        tree_phase_root, phase="initial", index=1
    )
    (
        tree_phase_root
        / "node_modules"
        / ".pnpm"
        / "right-pad@1.0.0"
        / "node_modules"
        / "right-pad"
        / "index.js"
    ).write_text("export const rightPad = () => 'tampered';\n", encoding="utf-8")
    tree_final = harbor_entry._dependency_graph_receipt(
        tree_phase_root,
        phase="final",
        index=2,
        previous=tree_initial,
    )
    if tree_final.get("valid") or tree_final.get("returncode") != 1:
        errors.append(
            "a build-time transitive package file rewrite must invalidate the "
            "final dependency audit"
        )

    cleared = Path(tempfile.mkdtemp())
    _dependency_fixture(cleared)
    harbor_entry._clear_candidate_dependencies(cleared)
    cleared_result = audit_dependency_graph(cleared)
    if cleared_result.get("status") != "FAIL":
        errors.append(
            "removing the installed graph must make the dependency audit fail"
        )

    install = next(
        command
        for kind, command in harbor_entry._dependency_commands(Path("."))
        if kind == "dependency-resolution" and "install" in command
    )
    for flag in ("--offline", "--frozen-lockfile", "--store-dir"):
        if flag not in install:
            errors.append(f"the real dependency install must include {flag}")
    if "--lockfile-only" in install:
        errors.append("dependency validation must not stop at a lockfile-only probe")

    receipt = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": "target-build",
        "index": 1,
        "command": ["pnpm", "run", "build"],
        "returncode": 0,
        "timed_out": False,
        "report_present": False,
    }
    dependency_receipt = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": "dependency-resolution",
        "index": 2,
        "command": ["pnpm", "install", "--offline", "--frozen-lockfile"],
        "returncode": 1,
        "timed_out": False,
        "report_present": False,
        "failed_phase": "exit",
    }
    evidence = build_hardgate_evidence(
        root,
        config=_fixture_config(),
        observations=[],
        observation_errors=[],
        receipts=[receipt, dependency_receipt],
        patch_applicable=True,
        source_test_baseline={"files": {}, "marker_counts": {}},
        isolation={"status": "PASS", "violations": [], "evidence": []},
    )
    if evidence["dependency_resolved"]["status"] != "FAIL":
        errors.append(
            "a failed dependency install receipt must fail dependency_resolved"
        )
    hardgate = evaluate_hardgate(REPO_ROOT, evidence)
    if hardgate["compound_gates"]["target_build_valid"]["status"] != "FAIL":
        errors.append(
            "an unresolved dependency graph must fail target_build_valid"
        )
    if hardgate["status"] != "FAIL" or hardgate["reward_override"] != 0:
        errors.append(
            "an ordinary dependency failure must produce hardgate FAIL and reward 0"
        )

    def graph_receipt(
        phase: str,
        index: int,
        digest: str,
        *,
        valid: bool = True,
    ) -> dict:
        return {
            "schema_version": "migration-execution-receipt:v1",
            "kind": "dependency-graph",
            "phase": phase,
            "index": index,
            "returncode": 0 if valid else 1,
            "timed_out": False,
            "signal": None,
            "failed_phase": None if valid else f"dependency-graph:{phase}",
            "report_present": False,
            "valid": valid,
            "resolved_graph": {
                "status": "PASS" if valid else "FAIL",
                "source": "evaluator-dependency-graph-audit",
                "errors": [] if valid else ["graph changed after the build"],
                "digest": digest,
            },
        }

    initial_digest = "sha256:initial"
    changed_digests = {
        "final-failed": [
            receipt,
            {
                "schema_version": "migration-execution-receipt:v1",
                "kind": "dependency-resolution",
                "index": 1,
                "command": ["pnpm", "install", "--offline", "--frozen-lockfile"],
                "returncode": 0,
                "timed_out": False,
                "report_present": False,
            },
            {
                "schema_version": "migration-execution-receipt:v1",
                "kind": "target-build",
                "index": 2,
                "command": ["pnpm", "run", "build"],
                "returncode": 0,
                "timed_out": False,
                "report_present": False,
            },
            graph_receipt("initial", 3, initial_digest),
            graph_receipt("post-build", 4, initial_digest),
            graph_receipt("final", 5, "sha256:changed", valid=False),
        ],
        "digest-mismatch": [
            {
                "schema_version": "migration-execution-receipt:v1",
                "kind": "dependency-resolution",
                "index": 1,
                "command": ["pnpm", "install", "--offline", "--frozen-lockfile"],
                "returncode": 0,
                "timed_out": False,
                "report_present": False,
            },
            {
                "schema_version": "migration-execution-receipt:v1",
                "kind": "target-build",
                "index": 2,
                "command": ["pnpm", "run", "build"],
                "returncode": 0,
                "timed_out": False,
                "report_present": False,
            },
            graph_receipt("initial", 3, initial_digest),
            graph_receipt("post-build", 4, initial_digest),
            graph_receipt("final", 5, "sha256:changed"),
        ],
    }
    for label, phase_receipts in changed_digests.items():
        mutated_evidence = build_hardgate_evidence(
            root,
            config=_fixture_config(),
            observations=[],
            observation_errors=[],
            receipts=phase_receipts,
            patch_applicable=True,
            source_test_baseline={"files": {}, "marker_counts": {}},
            isolation={"status": "PASS", "violations": [], "evidence": []},
        )
        if mutated_evidence["dependency_resolved"]["status"] != "FAIL":
            errors.append(
                f"a {label} dependency graph must fail dependency_resolved"
            )
        mutated_hardgate = evaluate_hardgate(REPO_ROOT, mutated_evidence)
        if (
            mutated_hardgate["compound_gates"]["target_build_valid"]["status"]
            != "FAIL"
        ):
            errors.append(
                f"a {label} dependency graph must fail target_build_valid"
            )
        if (
            mutated_hardgate["status"] != "FAIL"
            or mutated_hardgate["reward_override"] != 0
        ):
            errors.append(
                f"a {label} dependency graph must produce hardgate FAIL and reward 0"
            )
    return errors


def check_native_timing_jitter() -> list[str]:
    """Host jitter must be retried, never relaxed; persistent failures stay."""

    errors: list[str] = []

    def receipt(text: str, kind: str = "target-native-test") -> dict[str, object]:
        return {"kind": kind, "stderr": text, "returncode": 1}

    timing = (
        "\x1b[31m FAIL \x1b[0m unit test/test.js > "
        "handles very sparse arrays efficiently\n"
        "\x1b[31mAssertionError\x1b[0m: uneval took 123.88425099999972ms, "
        "expected < 100ms: expected false to be truthy\n"
        "\x1b[31m Failed Tests 1 \x1b[0m\n"
    )
    if harbor_entry._timing_jitter_failure(receipt(timing)) != [
        123.88425099999972
    ]:
        errors.append(
            "a lone wall-clock failure within the jitter envelope must be "
            "recognised as retryable"
        )

    slow = timing.replace("123.88425099999972ms", "2500ms")
    if harbor_entry._timing_jitter_failure(receipt(slow)) is not None:
        errors.append(
            "a slow sparse-array implementation beyond the jitter envelope "
            "must not be retried as jitter"
        )

    mixed = timing.replace(
        " Failed Tests 1 ", " Failed Tests 2 "
    ) + (
        "\x1b[31m FAIL \x1b[0m unit test/test.js > "
        "uneval: primitives\n"
        "AssertionError: expected 1 to be 2\n"
    )
    if harbor_entry._timing_jitter_failure(receipt(mixed)) is not None:
        errors.append(
            "a timing failure accompanied by another failing test must not be "
            "retried as jitter"
        )

    non_timing = (
        " FAIL  unit test/test.js > uneval: primitives\n"
        "AssertionError: expected 1 to be 2\n"
        " Failed Tests 1 \n"
    )
    if harbor_entry._timing_jitter_failure(receipt(non_timing)) is not None:
        errors.append("a non-timing native-test failure must not be retried")

    if (
        harbor_entry._timing_jitter_failure(
            receipt(timing, kind="target-build")
        )
        is not None
    ):
        errors.append("only native-test and evaluator-run receipts may be retried")

    root = Path(tempfile.mkdtemp())
    flaky = root / "flaky.py"
    flaky.write_text(
        "import pathlib, sys\n"
        "state = pathlib.Path(sys.argv[1])\n"
        "sys.stderr.write(' FAIL  unit test/test.js > '\n"
        "                 'handles very sparse arrays efficiently\\n')\n"
        "sys.stderr.write('AssertionError: uneval took 123.884ms, '\n"
        "                 'expected < 100ms: expected false to be truthy\\n')\n"
        "sys.stderr.write(' Failed Tests 1 \\n')\n"
        "if state.exists():\n"
        "    raise SystemExit(0)\n"
        "state.write_text('seen')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    persistent = root / "persistent.py"
    persistent.write_text(
        "import sys\n"
        "sys.stderr.write(' FAIL  unit test/test.js > '\n"
        "                 'handles very sparse arrays efficiently\\n')\n"
        "sys.stderr.write('AssertionError: uneval took 123.884ms, '\n"
        "                 'expected < 100ms: expected false to be truthy\\n')\n"
        "sys.stderr.write(' Failed Tests 1 \\n')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    environment = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}

    state = root / "state"
    receipts, failed, infra = harbor_entry._run_declared_commands(
        [
            (
                "target-native-test",
                [sys.executable, str(flaky), str(state)],
            )
        ],
        cwd=root,
        timeout=30.0,
        environment=environment,
    )
    if failed or infra:
        errors.append("a one-off wall-clock failure must be retried to a pass")
    elif (
        len(receipts) != 1
        or receipts[0].get("returncode") != 0
        or receipts[0].get("timing_jitter_attempt_count") != 2
        or len(receipts[0].get("timing_jitter_retries") or []) != 1
    ):
        errors.append(
            "a recovered wall-clock retry must be reported as one passing "
            "receipt with its prior attempts preserved"
        )

    receipts, failed, infra = harbor_entry._run_declared_commands(
        [
            (
                "target-native-test",
                [sys.executable, str(persistent)],
            )
        ],
        cwd=root,
        timeout=30.0,
        environment=environment,
    )
    if not failed or infra:
        errors.append(
            "a persistent wall-clock failure must not be masked by retries"
        )
    elif (
        not receipts
        or receipts[-1].get("returncode") != 1
        or receipts[-1].get("timing_jitter_attempt_count")
        != harbor_entry._TIMING_JITTER_MAX_ATTEMPTS
    ):
        errors.append(
            "a persistent wall-clock failure must exhaust the retry budget "
            "and keep the failing receipt"
        )
    return errors


def check_evaluator_test_include_coverage() -> list[str]:
    """The evaluator Vitest project must execute every mapped target file.

    Protected cases are required in the execution report, so the file that
    carries them must be part of the evaluator-owned run.  Conventional globs
    alone missed ``test/test.js`` once and turned a correct migration into a
    hard-gate failure, so the include list is derived from the equivalence map
    and checked here without Harbor.
    """

    errors: list[str] = []
    config = json.loads(
        (HERE.parent / "config.json").read_text(encoding="utf-8")
    )
    equivalence = load_equivalence(ASSETS / "source-test-equivalence.json")
    includes = harbor_entry._evaluator_test_includes(ASSETS, config)
    mapped: set[str] = set()
    for record in equivalence.values():
        if not isinstance(record, Mapping):
            continue
        for case in record.get("cases", []):
            if not isinstance(case, Mapping):
                continue
            target = str(case.get("target_file", "")).replace("\\", "/")
            if target:
                mapped.add(target)
    if not mapped:
        errors.append("the protected equivalence map declares no target files")
    for target in sorted(mapped):
        if target not in includes:
            errors.append(
                f"evaluator Vitest include list does not name the mapped file {target}"
            )
    root = Path(tempfile.mkdtemp())
    try:
        config_path, _ = harbor_entry._write_evaluator_vitest_files(
            root, includes=includes
        )
        payload = json.loads(
            config_path.read_text(encoding="utf-8")
            .split("export default ", 1)[1]
            .rstrip()
            .rstrip(";")
        )
        emitted = payload["test"]["projects"][0]["test"]["include"]
        if emitted != [*includes, str(root / ".migration-evaluator" / "assertion-contract.test.mjs")]:
            errors.append(
                "the evaluator Vitest config does not carry the derived include list"
            )
        for target in sorted(mapped):
            if target not in emitted:
                errors.append(
                    f"the evaluator Vitest config does not include {target}"
                )
    finally:
        harbor_entry._remove_evaluator_vitest_files(root)
        shutil.rmtree(root, ignore_errors=True)
    return errors


def check_public_contract_gate_is_preserved_only() -> list[str]:
    """The contract hard gate must only guard the pre-existing public API.

    ``public_contract_preserved`` must fail when a source (4.3.3) export is
    removed, but must NOT fail merely because a 6.0.0 addition is missing: that
    is an incomplete migration that scope (MU003) and F2P score with a partial
    ``T``.  This locks the fix for the finding that a candidate missing
    ``DevalueError`` was zeroed instead of keeping its partial progress.
    """

    errors: list[str] = []
    preserved = ("uneval", "parse", "unflatten", "stringify")

    def build(names: tuple[str, ...]) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "index.js").write_text(
            "\n".join(f"export function {name}() {{}}" for name in names) + "\n",
            encoding="utf-8",
        )
        (root / "types").mkdir()
        (root / "types" / "index.d.ts").write_text(
            "export declare function stringify(value: unknown): string;\n",
            encoding="utf-8",
        )
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "devalue",
                    "version": "6.0.0",
                    "type": "module",
                    "types": "./types/index.d.ts",
                    "exports": {
                        ".": {
                            "types": "./types/index.d.ts",
                            "import": "./index.js",
                            "default": "./index.js",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return root

    partial = build(preserved)  # keeps the 4.3.3 contract, 6.0.0 additions absent
    contract = evaluate_public_contract(partial)
    if contract["status"] != "PASS":
        errors.append(
            "a candidate that keeps the source contract but has not added the "
            "6.0.0 exports must still pass public_contract_preserved: "
            f"{contract['checks']}"
        )
    broken = build(("parse", "unflatten", "stringify"))  # drops the `uneval` export
    if evaluate_public_contract(broken)["status"] != "FAIL":
        errors.append("dropping a source export must fail public_contract_preserved")

    units = json.loads(
        (ASSETS / "migration-units.json").read_text(encoding="utf-8")
    ).get("units", [])
    mu003 = next(
        (row for row in units if isinstance(row, Mapping) and row.get("id") == "MU003"),
        None,
    )
    if mu003 is None:
        errors.append("MU003 migration unit is missing")
    elif evaluate_unit_static_checks(mu003, partial)["status"] != "FAIL":
        errors.append("scope MU003 must FAIL while the 6.0.0 exports are absent")
    return errors


def check_async_rejection_token_match_is_case_insensitive() -> list[str]:
    """Legal failure reasons must not depend on messages or traversal order."""
    node = shutil.which("node")
    if node is None:
        return ["node is required to exercise the async-rejection contract"]
    harness = ASSETS / "shared" / "batches" / "BATCH-P2P-0001" / "harness.mjs"
    script = "import { run } from " + json.dumps(harness.as_uri()) + ";\n" + r'''
const paths = {
  'symbol-sibling': '.bad', 'nested-function': '.nest.deep', 'map-value': '.bad',
  'array-element': '[1]', 'late-rejection': '.bad', 'symbol-before': '.bad',
  'array-before': '[0]', 'nested-before': '.bad', 'map-before': '.m.get(...)', 'late-before': '.bad'
};
async function probe(mode) {
  const candidate = {
    asyncRejectionCase(scenario) {
      if (scenario === 'solo-resolved') return { status: 'resolved', value_matches: true, unhandled: 0 };
      const marker = scenario === 'solo-rejected' ? 'solo' : scenario === 'two-rejections' ? 'first' : scenario;
      if (mode === 'promise' || !paths[scenario]) return { status: 'rejected', marker, unhandled: 0 };
      return {
        status: 'rejected', name: 'DevalueError', message: 'unsupported input',
        path: paths[scenario], devalue_error: true, root_matches: true,
        value_kind: ['nested-function', 'array-element', 'array-before', 'late-before'].includes(scenario) ? 'function' : 'symbol',
        unhandled: mode === 'unhandled' && scenario === 'symbol-before' ? 1 : 0
      };
    }
  };
  const payload = await run(candidate, { repoRoot: process.cwd() });
  return Object.fromEntries(payload.observations.map(row => [row.id, row.status]));
}
process.stdout.write(JSON.stringify({ traversal: await probe('traversal'), promise: await probe('promise'), unhandled: await probe('unhandled') }));
'''
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                               text=True, timeout=120, check=False)
    try:
        rows = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ["async contract probe produced no result: " + completed.stderr[-800:]]
    errors = []
    for mode in ("traversal", "promise"):
        for gate in ("F2P-018", "F2P-019", "F2P-021", "F2P-022", "F2P-023"):
            if rows[mode].get(gate) != "PASS":
                errors.append(f"{gate} must accept the actual {mode} failure independent of its message")
    if rows["unhandled"].get("F2P-018") != "FAIL":
        errors.append("a rejection after an earlier failing sibling must not pass")
    return errors


def check_controlled_test_failures_are_scored() -> list[str]:
    receipts = [
        {"schema_version": "migration-execution-receipt:v1", "kind": kind,
         "index": index, "returncode": status, "timed_out": False}
        for index, (kind, status) in enumerate(
            [("target-native-test", 0), ("test-execution", 1)], start=1
        )
    ]
    build, _, _ = _derive_execution_atoms(receipts)
    if build["status"] != "FAIL":
        return ["a failed evaluator-controlled assertion-helper probe must fail the execution gate"]
    receipts[-1]["returncode"] = 0
    build, _, _ = _derive_execution_atoms(receipts)
    return [] if build["status"] == "PASS" else ["passing controlled tests must retain PASS"]


def check_error_phase_classification() -> list[str]:
    errors = []
    for kind, expected in (
        ("target-build", "build error"),
        ("target-native-test", "test error"),
        ("test-execution", "test error"),
        ("dependency-resolution", "dependency error"),
    ):
        result = {
            "receipts": {"commands": [{"kind": kind, "returncode": 1, "timed_out": False}]},
            "hardgate_result": {"compound_gates": {"target_build_valid": {"status": "FAIL"}}},
        }
        flags = grader._build_error_flags(result)
        if not flags[expected]:
            errors.append(f"failed {kind} receipt must identify {expected}")
        if kind != "target-build" and (flags["build error"] or flags["compile error"]):
            errors.append(f"failed {kind} must not be reported as a build/compile error")
    return errors


def check_type_export_resolution() -> list[str]:
    errors = []
    declarations = (
        "declare const brand: unique symbol;\n"
        "export type JavaScriptFragment = { readonly [brand]: true };\n"
        "export interface JavaScriptTag { (s: TemplateStringsArray, ...v: unknown[]): JavaScriptFragment }\n"
        "export type UnevalReplacer = (v: unknown, js: JavaScriptTag) => JavaScriptFragment | false | null | undefined | void;\n"
    )
    ambient_declarations = declarations.replace("declare const", "const")
    with tempfile.TemporaryDirectory(prefix="type-exports-acceptance-") as directory:
        root = Path(directory)
        (root / "package.json").write_text(json.dumps({"type": "module", "types": "./index.d.ts"}))
        (root / "public.d.ts").write_text(declarations)
        (root / "nested.d.ts").write_text("export * from './public.js';\nexport * from './index.js';\n")
        valid = {
            "direct": declarations,
            "star": "export * from './public.js';\n",
            "type-star": "export type * from './public.js';\n",
            "cyclic-barrel": "export * from './nested.js';\n",
            "named": "export type { JavaScriptFragment, JavaScriptTag, UnevalReplacer } from './public.js';\n",
            "ambient": "declare module 'devalue' {\n" + ambient_declarations + "export {};\n}\n",
        }
        invalid = {
            "comment": "// export type JavaScriptFragment = unknown;\nexport {};\n",
            "missing-module": "export * from './missing.js';\n",
            "unexported": declarations.replace("export ", "") + "export {};\n",
            "namespace-only": "export * as internals from './public.js';\n",
            "missing-name": "export type { JavaScriptFragment, JavaScriptTag } from './public.js';\n",
            "wrong-ambient-package": "declare module 'other-package' {\n" + ambient_declarations + "export {};\n}\n",
            "ambient-namespace-only": "declare module 'devalue' {\nnamespace internals {\n" + ambient_declarations + "}\nexport {};\n}\n",
        }
        for name, entry in {**valid, **invalid}.items():
            (root / "index.d.ts").write_text(entry)
            checks = _mu026(root)
            expected = "PASS" if name in valid else "FAIL"
            passed = all(check["status"] == "PASS" for check in checks)
            failed = any(check["status"] == "FAIL" for check in checks)
            if (expected == "PASS" and not passed) or (expected == "FAIL" and not failed):
                errors.append(f"{name} declaration layout must {expected}: {checks}")
    return errors


def check_type_opacity_and_primitive_encoding() -> list[str]:
    completed = subprocess.run(
        ["node", str(HERE / "semantic_checks.mjs")], capture_output=True,
        text=True, timeout=180, check=False,
    )
    if completed.returncode:
        return ["type opacity / primitive encoding regressions: " + completed.stderr[-4000:]]
    return []


def check_view_backings_use_candidate_format() -> list[str]:
    node = shutil.which("node")
    if node is None:
        return ["node is required to exercise view backing observations"]
    module = ASSETS / "public" / "replacer-cases.mjs"
    script = "import { viewBackingOutcomes } from " + json.dumps(module.as_uri()) + ";\n" + r'''
function candidate(endRange, invalidBacking = false) {
  const parse = (source, revivers) => {
    const [tag, bytes, offset, last] = JSON.parse(source);
    const buffer = revivers?.ArrayBuffer ? revivers.ArrayBuffer() : new Uint8Array(bytes).buffer;
    if (!invalidBacking) new DataView(buffer, 0, 0);
    return new globalThis[tag](buffer, offset, endRange ? last - offset : last);
  };
  return {
    stringify(view) { return JSON.stringify([view.constructor.name, Array.from(new Uint8Array(view.buffer)), view.byteOffset,
      endRange ? view.byteOffset + (view.length ?? view.byteLength) : (view.length ?? view.byteLength)]); },
    parse,
    unflatten(value, revivers) { return parse(JSON.stringify(value), revivers); }
  };
}
const pass = value => Object.values(viewBackingOutcomes(value)).every(v => v === true);
process.stdout.write(JSON.stringify({ length: pass(candidate(false)), range: pass(candidate(true)), invalid: pass(candidate(false, true)) }));
'''
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                               text=True, timeout=60, check=False)
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        return ["view backing regression probe produced no result: " + completed.stderr[-800:]]
    return [] if result == {"length": True, "range": True, "invalid": False} else [
        f"view backing observations must accept independent valid layouts and reject coercion: {result}"
    ]


def check_null_prototype_key_rejection_is_independent() -> list[str]:
    """A valid reference must not mask acceptance of a forbidden key."""
    node = shutil.which("node")
    if node is None:
        return ["node is required to exercise null-prototype key rejection"]
    harness = ASSETS / "shared" / "batches" / "BATCH-P2P-0001" / "harness.mjs"
    script = "import { nullPrototypeRejectsNonStringKey as gate } from " + json.dumps(harness.as_uri()) + ";\n" + r'''
function decoder(rejectProto, rejectAll = false, onlyRoot = false) {
  const unflatten = (values) => {
    const hydrate = (index, root = false) => {
      if (rejectAll) throw Error('reject all');
      if (index === -1) return undefined;
      if (!Number.isInteger(index) || index < 0 || index >= values.length) throw Error('index');
      const value = values[index];
      if (Array.isArray(value) && value[0] === 'null') {
        const object = Object.create(null);
        for (let i = 1; i < value.length; i += 2) {
          const key = value[i];
          if (typeof key !== 'string' || (rejectProto && key === '__proto__' && (!onlyRoot || root))) throw Error('key');
          object[key] = hydrate(value[i + 1]);
        }
        return object;
      }
      if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, ref]) => [key, hydrate(ref)]));
      return value;
    };
    return hydrate(0, true);
  };
  return { unflatten, parse: text => unflatten(JSON.parse(text)) };
}
process.stdout.write(JSON.stringify({
  correct: gate(decoder(true)), missingGuard: gate(decoder(false)),
  blanketReject: gate(decoder(true, true)), rootOnly: gate(decoder(true, false, true))
}));
'''
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                               text=True, timeout=30, check=False)
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        return ["null-prototype regression probe produced no result: " + completed.stderr[-800:]]
    expected = {"correct": True, "missingGuard": False, "blanketReject": False, "rootOnly": False}
    return [] if completed.returncode == 0 and result == expected else [
        f"null-prototype rejection must be independent of index validity and keep legal keys: {result}"
    ]


def main() -> int:
    errors = [
        *check_reward_mapping(),
        *check_p2p_excluded_from_progress(),
        *check_state_applicability_fails_closed(),
        *check_p2p_hard_gate(),
        *check_arithmetic_mean_progress(),
        *check_mu022_does_not_require_release_notes(),
        *check_unit_requires_static_evidence(),
        *check_candidate_execution_failures(),
        *check_native_timing_jitter(),
        *check_not_run_reporting_and_diagnostic_isolation(),
        *check_declared_type_entry_and_benchmark_scope(),
        *check_public_contract_gate_is_preserved_only(),
        *check_async_rejection_token_match_is_case_insensitive(),
        *check_controlled_test_failures_are_scored(),
        *check_error_phase_classification(),
        *check_type_export_resolution(),
        *check_type_opacity_and_primitive_encoding(),
        *check_view_backings_use_candidate_format(),
        *check_null_prototype_key_rejection_is_independent(),
        *check_wire_format_cross_consumption(),
        *check_test_integrity_mutations(),
        *check_evaluator_test_include_coverage(),
        *check_replacer_protocol_reachability(),
        *check_isolation_boundary(),
        *check_dependency_graph_is_recomputed(),
    ]
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("PASS: verifier acceptance checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
