#!/usr/bin/env python3
"""Validate deterministic reports and publish a binary verifier-only result."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


class ReportError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ReportError(f"invalid JSON report {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReportError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def finite_binary(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ReportError(f"{label} must be numeric")
    score = float(value)
    if not math.isfinite(score) or score not in {0.0, 1.0}:
        raise ReportError(f"{label} must be 0 or 1")
    return score


def expected_statuses(config: dict[str, Any], ctrf: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    expected = {
        **{str(node): "f2p" for node in config["f2p"]},
        **{str(node): "p2p" for node in config["p2p"]},
    }
    required = [str(node) for node in config.get("required_nodes", [])]
    overlap = {str(node) for node in config["f2p"]} & {
        str(node) for node in config["p2p"]
    }
    if overlap:
        raise ReportError(f"F2P and P2P sets overlap: {sorted(overlap)}")
    if set(required) != set(expected):
        raise ReportError(
            "required_nodes must equal the F2P/P2P union: "
            f"missing_from_groups={sorted(set(required) - set(expected))}, "
            f"undeclared_groups={sorted(set(expected) - set(required))}"
        )
    identity = ctrf.get("identity")
    if not isinstance(identity, dict):
        raise ReportError("CTRF identity is missing")
    for key, expected_value in {
        "task_id": config["task_id"],
        "suite_id": config["suite_id"],
        "replay_tree": config["provenance"]["replay_tree"],
    }.items():
        if identity.get(key) != expected_value:
            raise ReportError(f"CTRF identity mismatch for {key}")

    actual: dict[str, str] = {}
    for entry in ctrf.get("results", {}).get("tests", []):
        node_id = str(entry.get("testId"))
        if node_id in actual:
            raise ReportError(f"duplicate CTRF node: {node_id}")
        if node_id not in expected:
            raise ReportError(f"unexpected CTRF node: {node_id}")
        status = str(entry.get("status"))
        if status not in {"passed", "failed"}:
            raise ReportError(f"invalid required-node status {status!r}: {node_id}")
        metadata = ((entry.get("extra") or {}).get("deepswe") or {})
        if metadata.get("group") != expected[node_id]:
            raise ReportError(f"wrong group for {node_id}")
        for required in (
            "execution_status",
            "measurement_status",
            "responsibility",
            "outcome",
            "validity_status",
            "blocked_by",
        ):
            if required not in metadata:
                raise ReportError(f"missing {required} metadata for {node_id}")
        if metadata.get("validity_status") != "valid":
            raise ReportError(f"invalid evaluation metadata for required node {node_id}")
        if metadata.get("measurement_status") not in {"measured", "not_measured"}:
            raise ReportError(f"invalid measurement status for {node_id}")
        if metadata.get("execution_status") not in {"completed", "not_run"}:
            raise ReportError(f"invalid execution status for {node_id}")
        actual[node_id] = status
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise ReportError(f"missing CTRF nodes: {missing}")
    summary = ctrf["results"]["summary"]
    if summary.get("tests") != len(expected):
        raise ReportError("CTRF summary test count mismatch")
    if summary.get("passed") != sum(value == "passed" for value in actual.values()):
        raise ReportError("CTRF summary passed count mismatch")
    if summary.get("failed") != sum(value == "failed" for value in actual.values()):
        raise ReportError("CTRF summary failed count mismatch")
    return actual, identity


def state_preservation_component(components: dict[str, Any]) -> dict[str, Any]:
    """Normalize the conditional state-preservation verifier component.

    ``na`` is only accepted when the verifier *positively* determined the
    check does not apply (``applicable`` is exactly ``False`` and the outcome
    is ``na``). A component that is entirely omitted, an applicable check that
    was never executed (``not_run``), or an undetermined applicability is
    never silently treated as ``na``: omission and undetermined applicability
    are rejected as an invalid verifier result, while an applicable-but-failed
    or not-run check is scored as a failure.
    """
    if "required_state_preservation_complete" not in components:
        raise ReportError(
            "verifier omitted required_state_preservation_complete; "
            "it must not be treated as not applicable"
        )
    state = components["required_state_preservation_complete"]
    if not isinstance(state, dict):
        raise ReportError("required_state_preservation_complete must be an object")
    applicable = state.get("applicable")
    outcome = state.get("outcome")
    reason = state.get("reason")
    if applicable is False:
        if outcome != "na":
            raise ReportError(
                "required_state_preservation_complete is marked not applicable "
                f"but reports outcome {outcome!r} instead of 'na'"
            )
        return {"applicable": False, "outcome": "na", "score": None, "reason": reason}
    if applicable is True:
        if outcome not in {"passed", "failed", "not_run"}:
            raise ReportError(
                f"required_state_preservation_complete has invalid outcome {outcome!r}"
            )
        return {
            "applicable": True,
            "outcome": outcome,
            "score": float(state.get("score") or 0.0),
            "reason": reason,
        }
    raise ReportError(
        "required_state_preservation_complete applicability is undetermined; "
        "it must not be treated as not applicable"
    )


def error_flags(gate_result: dict[str, Any]) -> dict[str, bool]:
    """Classify the fixed error dictionary required by the reward contract.

    Infrastructure failures never reach the gate: they abort before a reward is
    published. A failing target build is a candidate build/compile error and is
    reported as such; every other branch is a clean candidate or gate failure.
    """
    failed = set(gate_result.get("hard_gate", {}).get("failed_node_ids") or [])
    build_error = bool(failed & {"gate.dependency-resolved", "gate.build-pass"})
    compile_error = build_error or "gate.migration-contract" in failed
    return {
        "infrastructure_error": False,
        "agent_time_out": False,
        "build error": build_error,
        "compile error": compile_error,
    }


def gate(args: argparse.Namespace) -> int:
    config = read_json(args.config)
    ctrf = read_json(args.ctrf)
    hard_gate = read_json(args.hard_gate)
    verifier = read_json(args.verifier)
    if hard_gate.get("evaluation_valid") is not True or hard_gate.get("infrastructure_error") is True:
        raise ReportError("invalid evaluation may not enter candidate scoring")
    if ctrf.get("evaluation", {}).get("valid") is not True:
        raise ReportError("CTRF marks the evaluation invalid")
    statuses, identity = expected_statuses(config, ctrf)
    if verifier.get("identity") != identity:
        raise ReportError("verifier and CTRF identity mismatch")
    hard_gate_identity = {
        "task_id": hard_gate.get("task_id"),
        "suite_id": hard_gate.get("suite_id"),
        "replay_tree": hard_gate.get("replay_identity", {}).get("tree"),
        "candidate_patch_sha256": hard_gate.get("candidate_identity", {}).get("patch_sha256"),
        "evaluator_assets": hard_gate.get("asset_identity", {}),
        "runtime": hard_gate.get("runtime", {}),
    }
    for key, expected_value in hard_gate_identity.items():
        if identity.get(key) != expected_value:
            raise ReportError(f"CTRF and hard-gate identity mismatch for {key}")
    hard_gate_passed = hard_gate.get("passed") is True and not bool(args.apply_failed)
    cheating_detected = hard_gate.get("cheating_detected") is True
    f2p = [str(node) for node in config["f2p"]]
    p2p = [str(node) for node in config["p2p"]]
    required = [str(node) for node in config.get("required_nodes", [])]
    f2p_passed = sum(statuses[node] == "passed" for node in f2p)
    p2p_passed = sum(statuses[node] == "passed" for node in p2p)
    components = verifier.get("components", {})
    scope = components.get("migration_scope_complete", {})
    f2p_component = components.get("f2p_regression") or components.get("f2p", {})
    state = state_preservation_component(components)
    state_applicable = state["applicable"]
    scope_score = float(scope.get("score") or 0.0)
    f2p_score = float(f2p_component.get("score") or 0.0)
    f2p_total = int(f2p_component.get("total") or 0)
    # T is the mean of the applicable verifier components: migration scope
    # completeness, the F2P regression pass rate, and (when applicable) state
    # preservation. P2P is a hard gate (boolean) and never enters the score; NA
    # components (no persistent state) are excluded from the denominator.
    f2p_applicable = f2p_total > 0
    applicable_scores = [scope_score]
    if f2p_applicable:
        applicable_scores.append(f2p_score)
    if state_applicable:
        applicable_scores.append(state["score"])
    test_score = sum(applicable_scores) / len(applicable_scores) if applicable_scores else 0.0
    scope_ok = scope.get("complete") is True
    # F2P belongs to the verifier: a fully green fail-to-pass set is a
    # conjunction condition for verifier success, and its pass rate is scored.
    f2p_ok = (not f2p_applicable) or (
        int(f2p_component.get("passed") or 0) == f2p_total
    )
    state_ok = (not state_applicable) or state["outcome"] == "passed"
    required_failed = [
        str(node)
        for node in config.get("required_nodes", [])
        if statuses.get(str(node)) != "passed"
    ]
    all_passed = (
        hard_gate_passed
        and scope_ok
        and f2p_ok
        and state_ok
        and not required_failed
    )
    p2p_ratio = (p2p_passed / len(p2p)) if p2p else 0.0
    result = {
        "schema_version": "deepswe-gate-reward/v2",
        "identity": identity,
        "evaluation_valid": True,
        "candidate_outcome": "passed" if all_passed else "failed",
        "overall_eligible": all_passed,
        "failure_class": "cheating" if cheating_detected else (None if all_passed else "candidate_failure"),
        "cheating_detected": cheating_detected,
        "cheating_level": hard_gate.get("cheating_level"),
        "cheat_reasons": hard_gate.get("cheat_reasons") or [],
        "apply_failed": bool(args.apply_failed),
        "hard_gate": {
            "passed": hard_gate_passed,
            "failed_node_ids": hard_gate.get("failed_nodes") or [],
        },
        "test_score": test_score,
        "migration_scope_complete_ratio": scope_score,
        "f2p_pass_ratio": f2p_score if f2p_applicable else 0.0,
        "p2p_pass_ratio": p2p_ratio,
        "verifier": {
            "migration_scope_complete": {
                "complete": scope_ok,
                "score": scope_score,
                "passed_units": scope.get("passed_units"),
                "total_units": scope.get("total_units"),
            },
            "f2p": {
                "complete": f2p_ok,
                "score": f2p_score,
                "passed": f2p_component.get("passed"),
                "total": f2p_component.get("total"),
            },
            "required_state_preservation_complete": (
                {
                    "outcome": state["outcome"],
                    "applicable": True,
                    "score": state["score"],
                    "reason": state["reason"],
                }
                if state_applicable
                else {
                    "outcome": "na",
                    "applicable": False,
                    "reason": state["reason"],
                }
            ),
        },
        # Node-level diagnostics. P2P is reported but never scored.
        "f2p": {
            "passed": f2p_passed,
            "total": len(f2p),
            "complete": f2p_passed == len(f2p),
            "failed_node_ids": [node for node in f2p if statuses[node] != "passed"],
        },
        "p2p": {
            "passed": p2p_passed,
            "total": len(p2p),
            "complete": p2p_passed == len(p2p),
            "failed_node_ids": [node for node in p2p if statuses[node] != "passed"],
        },
        "required_nodes": {
            "total": len(required),
            "failed_node_ids": required_failed,
        },
    }
    write_json(args.output, result)
    return 0


def finalize(args: argparse.Namespace) -> int:
    config = read_json(args.config)
    gate_result = read_json(args.gate)
    if config.get("reward_mode") != "VERIFIER_ONLY":
        raise ReportError("this task supports VERIFIER_ONLY only")
    if gate_result.get("evaluation_valid") is not True:
        raise ReportError("invalid evaluation may not publish a reward")
    success = gate_result.get("overall_eligible") is True
    cheating = gate_result.get("cheating_detected") is True
    hard_gate_passed = gate_result.get("hard_gate", {}).get("passed") is True
    test_score = float(gate_result.get("test_score") or 0.0)
    test_score = max(0.0, min(1.0, test_score))
    scope_ratio = float(gate_result.get("migration_scope_complete_ratio") or 0.0)
    f2p_ratio = float(gate_result.get("f2p_pass_ratio") or 0.0)
    p2p_ratio = float(gate_result.get("p2p_pass_ratio") or 0.0)
    if cheating:
        reward = -1.0
        branch = "cheating_detected"
    elif not hard_gate_passed:
        reward = 0.0
        branch = "hard_gate_failed"
    elif not success:
        # spec: reward = T_weight * T (T_weight = 1), verifier failed with T < 1
        reward = config["weights"]["test"] * test_score
        branch = "verifier_failed"
    else:
        # spec: reward = T_weight * T with T = 1
        reward = config["weights"]["test"] * test_score
        branch = "verifier_success"
    reward = max(-1.0, min(1.0, reward))
    public = {
        "test": test_score,
        "migration_scope_complete_ratio": scope_ratio,
        "f2p_pass_ratio": f2p_ratio,
        "reward": reward,
    }
    details = {
        "schema_version": "deepswe-reward-details/v2",
        "identity": gate_result["identity"],
        "reward_mode": "VERIFIER_ONLY",
        "policy": "deterministic_verifier_only",
        "policy_version": "3",
        "evaluation_valid": True,
        "evaluation_success": success,
        "candidate_outcome": "passed" if success else "failed",
        "status": branch,
        "failure_class": gate_result.get("failure_class"),
        "cheating_detected": cheating,
        "cheating_level": gate_result.get("cheating_level"),
        "cheat_reasons": gate_result.get("cheat_reasons") or [],
        "scoring_executed": False,
        "skip_reason": "verifier_only",
        "test": test_score,
        "migration_scope_complete_ratio": scope_ratio,
        "f2p_pass_ratio": f2p_ratio,
        "p2p_pass_ratio": p2p_ratio,
        "errors": error_flags(gate_result),
        "components": {
            "test": {"score": test_score, "weight": config["weights"]["test"]},
        },
        "formula_branch": branch,
        "reward": reward,
        "apply_failed": gate_result.get("apply_failed") is True,
        "overall_eligibility": success,
        "hard_gate": gate_result["hard_gate"],
        "f2p": gate_result["f2p"],
        "p2p": gate_result["p2p"],
    }
    reports = config["reports"]
    write_json(Path(reports["reward"]), public)
    write_json(Path(reports["reward_details"]), details)
    Path(reports["reward_text"]).write_text(f"{reward:g}\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--config", type=Path, required=True)
    gate_parser.add_argument("--ctrf", type=Path, required=True)
    gate_parser.add_argument("--hard-gate", type=Path, required=True)
    gate_parser.add_argument("--verifier", type=Path, required=True)
    gate_parser.add_argument("--output", type=Path, required=True)
    gate_parser.add_argument("--apply-failed", type=int, choices=(0, 1), default=0)
    gate_parser.set_defaults(handler=gate)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--config", type=Path, required=True)
    finalize_parser.add_argument("--gate", type=Path, required=True)
    finalize_parser.set_defaults(handler=finalize)
    args = parser.parse_args()
    try:
        return args.handler(args)
    except ReportError as error:
        # A report-schema failure is an evaluator/configuration error, not a
        # candidate failure: SOP reserves exit 1 for candidate failure and
        # exit 2 for infrastructure/configuration problems.
        print(f"report validation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
