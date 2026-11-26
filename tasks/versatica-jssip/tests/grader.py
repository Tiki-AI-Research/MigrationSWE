#!/usr/bin/env python3
"""Deterministic hard-gate, regression and F2P reward aggregator.

Reward branches (migration verifier spec, section 8):

    cheating (anti-cheating gate failed, L1/L2) -> reward = -1
    other hard gate failed                      -> reward = 0
    hard gate success, verifier failed          -> reward = T_weight * T
    hard gate success, verifier success         -> reward = T_weight * T (T = 1)

``T`` is the arithmetic mean of the applicable verifier completion rates:
migration scope coverage, F2P pass ratio, and (conditional) required state
preservation pass ratio.  P2P is enforced by the hard gate and is never part of
``T``.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def finite_score(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be numeric")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError(f"{label} must be in [0,1]")
    return score


def _groups(config: dict[str, Any]) -> dict[str, str]:
    groups: dict[str, str] = {}
    for node in config["f2p"]:
        groups[str(node)] = "F2P"
    for node in config["p2p"]:
        groups[str(node)] = "P2P"
    for node in config.get("differential", []):
        groups[str(node)] = "differential"
    return groups


def required_statuses(config: dict[str, Any], ctrf: dict[str, Any]) -> dict[str, str]:
    expected = _groups(config)
    actual: dict[str, str] = {}
    for entry in ctrf["results"]["tests"]:
        test_id = str(entry["testId"])
        if test_id in actual:
            raise ValueError(f"duplicate CTRF node: {test_id}")
        group = ((entry.get("extra") or {}).get("deepswe") or {}).get("group")
        if test_id in expected and group != expected[test_id]:
            raise ValueError(f"wrong CTRF group for {test_id}: {group}")
        actual[test_id] = str(entry["status"])
    missing = sorted(set(expected) - set(actual))
    if missing:
        raise ValueError(f"missing CTRF nodes: {missing}")
    return {node: actual[node] for node in expected}


def node_results(config: dict[str, Any], statuses: dict[str, str]) -> list[dict[str, str]]:
    groups = _groups(config)
    return [
        {"testId": node, "group": groups[node], "status": statuses[node]}
        for node in groups
    ]


def _section(config: dict[str, Any], statuses: dict[str, str], group: str) -> dict[str, Any]:
    nodes = [str(node) for node in config.get(group, [])]
    passed = sum(statuses[node] == "passed" for node in nodes)
    return {
        "passed": passed,
        "total": len(nodes),
        "ratio": passed / len(nodes) if nodes else 1.0,
        "complete": passed == len(nodes),
        "failed_node_ids": [node for node in nodes if statuses[node] != "passed"],
    }


def _combine_sections(*sections: dict[str, Any]) -> dict[str, Any]:
    """Merge P2P and differential behavior probes for reporting."""
    total = sum(int(section.get("total", 0)) for section in sections)
    passed = sum(int(section.get("passed", 0)) for section in sections)
    failed = {
        str(node)
        for section in sections
        for node in section.get("failed_node_ids") or []
    }
    return {
        "passed": passed,
        "total": total,
        "ratio": passed / total if total else 1.0,
        "complete": passed == total,
        "failed_node_ids": sorted(failed),
    }


def _scope_component(verifier_report: dict[str, Any]) -> dict[str, Any]:
    """Require an explicit migration-scope result; never invent ``na``."""
    raw = verifier_report.get("migration_scope_complete")
    if not isinstance(raw, dict):
        return {
            "status": "failed",
            "ratio": 0.0,
            "reason": "migration scope result is missing",
        }
    status = str(raw.get("status") or "")
    if status == "na":
        return {"status": "na", "ratio": None}
    try:
        ratio = finite_score(raw.get("ratio"), "migration_scope_complete.ratio")
    except ValueError:
        ratio = 0.0
    if status not in {"passed", "failed"}:
        status = "failed"
        ratio = 0.0
    return {"status": status, "ratio": ratio}


def _state_component(verifier_report: dict[str, Any]) -> dict[str, Any]:
    """Normalize the conditional state result without silently defaulting to na.

    A missing object, an unsupported status, or ``status=na`` without an
    applicability reason is a verifier failure with ratio 0.  ``not_run`` is
    kept in T as 0 instead of being filtered out, so a verifier-failed branch
    can never produce T=1 merely because the state check did not execute.
    """
    raw = verifier_report.get("required_state_preservation_complete")
    if not isinstance(raw, dict):
        return {
            "status": "failed",
            "ratio": 0.0,
            "reason": "required state applicability result is missing",
        }
    status = str(raw.get("status") or "")
    if status == "na":
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            return {
                "status": "failed",
                "ratio": 0.0,
                "reason": "state=na requires an applicability reason",
            }
        return {"status": "na", "ratio": None, "reason": reason}
    if status == "not_run":
        return {
            "status": "not_run",
            "ratio": 0.0,
            "reason": str(raw.get("reason") or "state check did not run"),
        }
    if status not in {"passed", "failed"}:
        return {
            "status": "failed",
            "ratio": 0.0,
            "reason": "required state applicability status is missing or invalid",
        }
    try:
        ratio = finite_score(raw.get("ratio"), "required_state_preservation_complete.ratio")
    except ValueError:
        ratio = 0.0
        status = "failed"
    if status == "passed" and ratio != 1.0:
        status = "failed"
    return {"status": status, "ratio": ratio}


def _average_completion_rate(gate_result: dict[str, Any]) -> float:
    rates = gate_result.get("completion_rates")
    if isinstance(rates, dict):
        applicable = [
            finite_score(rates[name], f"completion_rates.{name}")
            for name in ("scope", "f2p", "state")
            if rates.get(name) is not None
        ]
        if applicable:
            return sum(applicable) / len(applicable)
    return finite_score(gate_result.get("test_reward", 0.0), "gate.test_reward")


def gate(args: argparse.Namespace) -> int:
    config = read_json(args.config)
    ctrf = read_json(args.ctrf)
    hard_gate = read_json(args.hard_gate)
    verifier_report: dict[str, Any] = {}
    verifier_path = (config.get("diagnostics") or {}).get("verifier")
    if verifier_path and Path(verifier_path).is_file():
        verifier_report = read_json(Path(verifier_path))

    hard_gate_passed = hard_gate.get("passed") is True and not bool(args.apply_failed)
    cheating_detected = hard_gate.get("cheating_detected") is True
    failure_class = str(hard_gate.get("failure_class") or "none")

    if not hard_gate_passed:
        f2p_nodes = {str(node) for node in config["f2p"]}
        for entry in ctrf["results"]["tests"]:
            if str(entry.get("testId")) in f2p_nodes:
                entry["status"] = "failed"
                entry["message"] = "candidate failed deterministic hard gate"
        statuses = ("passed", "failed", "skipped", "pending", "other")
        tests = ctrf["results"]["tests"]
        summary = ctrf["results"].get("summary")
        if isinstance(summary, dict):
            summary["tests"] = len(tests)
            summary.update({status: sum(item.get("status") == status for item in tests) for status in statuses})
        write_json(args.ctrf, ctrf)

    statuses = required_statuses(config, ctrf)
    f2p = _section(config, statuses, "f2p")
    p2p = _combine_sections(
        _section(config, statuses, "p2p"),
        _section(config, statuses, "differential"),
    )
    differential = _section(config, statuses, "differential")

    scope = _scope_component(verifier_report)
    state = _state_component(verifier_report)
    regression = verifier_report.get("regression")
    if not isinstance(regression, dict):
        # Compatibility fallback for an older verifier report: only F2P is a
        # verifier component.  P2P remains a hard gate and is never folded in.
        regression = f2p

    completion_rates = {
        "scope": scope.get("ratio"),
        "f2p": f2p.get("ratio"),
        # ``not_run`` is an executed-then-failed verifier result for T purposes;
        # it must not disappear from the denominator.
        "state": state.get("ratio"),
    }
    applicable = [finite_score(rate, f"completion_rates.{name}")
                  for name, rate in completion_rates.items() if rate is not None]
    test_reward = sum(applicable) / len(applicable) if applicable else 0.0
    scope_ratio = finite_score(scope.get("ratio") or 0.0, "migration_scope_complete.ratio")
    state_ok = (
        state.get("status") == "passed"
        and finite_score(state.get("ratio") or 0.0, "required_state_preservation_complete.ratio") == 1.0
    ) or state.get("status") == "na"
    exact_scope = scope.get("status") == "na" or (
        scope.get("status") == "passed" and scope_ratio == 1.0
    )
    verifier_success = bool(
        hard_gate_passed
        and exact_scope
        and f2p.get("complete") is True
        and state_ok
    )
    if not verifier_success:
        # The verifier-failed branch is defined as T < 1.  Normalize a malformed
        # report (for example a passing status with a zero ratio) rather than
        # letting it collide with the success branch.
        test_reward = min(test_reward, math.nextafter(1.0, 0.0))

    result = {
        "schema_version": "deepswe-gate-reward/v2",
        "hard_gate_passed": bool(hard_gate_passed),
        "hard_gate_success": int(hard_gate_passed),
        "verifier_success": int(verifier_success),
        "eligible": bool(hard_gate_passed and verifier_success),
        "cheating_detected": bool(cheating_detected),
        "cheating_level": hard_gate.get("cheating_level"),
        "cheat_reasons": hard_gate.get("cheat_reasons") or [],
        "failure_class": "cheating" if cheating_detected else failure_class,
        "apply_failed": bool(args.apply_failed),
        "f2p": f2p,
        "p2p": p2p,
        "p2p_pass_ratio": finite_score(p2p.get("ratio", 1.0), "p2p.ratio"),
        "differential": differential,
        "regression": regression,
        "migration_scope_complete": scope,
        "required_state_preservation_complete": state,
        "completion_rates": completion_rates,
        "test_reward": test_reward,
        "failed_nodes": sorted(node for node, status in statuses.items() if status != "passed"),
        "nodes": node_results(config, statuses),
        "hard_gate": hard_gate,
    }
    write_json(args.output, result)
    return 0


def finalize(args: argparse.Namespace) -> int:
    config = read_json(args.config)
    gate_result = read_json(args.gate)
    weight = float((config.get("weights") or {}).get("test", 1.0))
    cheating = gate_result.get("cheating_detected") is True
    hard_gate_passed = gate_result.get("hard_gate_passed") is True
    success = gate_result.get("verifier_success") == 1
    test_reward = finite_score(gate_result.get("test_reward", 0.0), "gate.test_reward")

    if cheating:
        reward = -1.0
        branch = "cheating_detected"
    elif not hard_gate_passed:
        reward = 0.0
        branch = "hard_gate_failed"
    elif not success:
        reward = weight * test_reward
        branch = "verifier_failed"
    else:
        reward = weight * 1.0
        branch = "verifier_success"

    components = {
        "hard_gate": {"score": float(gate_result.get("hard_gate_success", 0)), "weight": 0.0},
        "migration_scope_complete": {"score": gate_result.get("completion_rates", {}).get("scope") or 0.0, "weight": 0.0},
        "f2p": {"score": gate_result.get("completion_rates", {}).get("f2p") or 0.0, "weight": 0.0},
        "required_state": {"score": gate_result.get("completion_rates", {}).get("state") or 0.0, "weight": 0.0},
        "test": {"score": test_reward, "weight": weight},
    }
    scope_ratio = finite_score(gate_result.get("completion_rates", {}).get("scope") or 0.0, "scope")
    f2p_ratio = finite_score(gate_result.get("completion_rates", {}).get("f2p") or 0.0, "f2p")
    p2p_ratio = finite_score(gate_result.get("p2p_pass_ratio", 1.0), "p2p")
    hard_gate_report = gate_result.get("hard_gate") if isinstance(gate_result.get("hard_gate"), dict) else {}
    failed_nodes = set(hard_gate_report.get("failed_nodes") or gate_result.get("failed_nodes") or [])
    errors = {
        "infrastructure_error": False,
        "agent_time_out": False,
        "build error": any(str(node).startswith("target_build_valid:build_pass") for node in failed_nodes),
        "compile error": any(str(node).startswith("target_build_valid:build_pass") for node in failed_nodes),
    }
    public_reward = {
        "test": test_reward,
        "migration_scope_complete_ratio": scope_ratio,
        "f2p_pass_ratio": f2p_ratio,
        "reward": reward,
    }
    details = {
        "schema_version": "deepswe-reward-details/v2",
        "reward_mode": config.get("reward_mode", "VERIFIER_ONLY"),
        "status": branch,
        "evaluation_valid": True,
        "evaluation_success": success,
        "scoring_executed": False,
        "skip_reason": None if success else "deterministic_gate_failed",
        "policy": "deterministic_verifier_only",
        "policy_version": "4",
        "components": components,
        "formula_branch": branch,
        "test": test_reward,
        "migration_scope_complete_ratio": scope_ratio,
        "f2p_pass_ratio": f2p_ratio,
        "p2p_pass_ratio": p2p_ratio,
        "reward": reward,
        "errors": errors,
        "gate": gate_result,
        "verifier_success": int(success),
        "hard_gate_success": int(hard_gate_passed),
        "cheating_detected": cheating,
        "cheating_level": gate_result.get("cheating_level"),
        "failure_class": gate_result.get("failure_class"),
        "completion_rates": gate_result.get("completion_rates") or {},
        "migration_scope_complete": gate_result.get("migration_scope_complete") or {},
        "regression": gate_result.get("regression") or {},
        "required_state_preservation_complete": gate_result.get("required_state_preservation_complete") or {},
        "f2p_passed": gate_result.get("f2p", {}).get("passed", 0),
        "f2p_total": gate_result.get("f2p", {}).get("total", 0),
        "p2p_passed": gate_result.get("p2p", {}).get("passed", 0),
        "p2p_total": gate_result.get("p2p", {}).get("total", 0),
        "differential": gate_result.get("differential") or {},
        "failed_nodes": gate_result.get("failed_nodes") or [],
        "nodes": gate_result.get("nodes") or [],
        "apply_failed": gate_result.get("apply_failed") is True,
    }
    reports = config["reports"]
    write_json(Path(reports["reward"]), public_reward)
    write_json(Path(reports["reward_details"]), details)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate_parser = subparsers.add_parser("gate")
    gate_parser.add_argument("--config", type=Path, required=True)
    gate_parser.add_argument("--ctrf", type=Path, required=True)
    gate_parser.add_argument("--hard-gate", type=Path, required=True)
    gate_parser.add_argument("--output", type=Path, required=True)
    gate_parser.add_argument("--apply-failed", type=int, choices=(0, 1), default=0)
    gate_parser.set_defaults(handler=gate)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--config", type=Path, required=True)
    finalize_parser.add_argument("--gate", type=Path, required=True)
    finalize_parser.set_defaults(handler=finalize)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
