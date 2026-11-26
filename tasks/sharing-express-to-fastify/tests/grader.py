from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

# allow: SIZE_OK - single-file Harbor grader entrypoint invoked as
# `python3 /tests/grader.py`; splitting would add modules outside the
# evaluator's fixed file interface.
LOG = Path(os.environ.get("HARBOR_LOG_DIR", "/logs")) / "verifier"
CONFIG = Path(os.environ.get("HARBOR_CONFIG_PATH", "/tests/config.json"))

# A terminal status in this set means the evaluator, not the candidate, failed.
_EVALUATOR_FAILURE_STATUSES = frozenset({"infrastructure_error", "configuration_error"})


def load(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ValueError(f"missing report: {path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"malformed json in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"report must be an object: {path}")
    return value


def finite_score(value: Any, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"score must be a finite number in [0, 1], got: {value!r}")
    return float(value)


def optional_rate(value: Any) -> float | None:
    """Return a measured pass rate in [0, 1], or None when it is absent or invalid."""
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        return None
    return float(value)


# Downstream consumers need to tell an accidental scope slip from deliberate
# cheating. The second block of ids are the concrete check ids the verifier
# modules actually emit for the first block.
_DECEPTIVE_POLICY_CHECKS = frozenset(
    {
        "no_source_implementation_bridge",
        "no_legacy_dependency_identity",
        "no_child_server_spawning",
        "raw_patch_safety",
        "protected_paths_unchanged",
        "anti_cheating_pass",
        "tests_modified",
        "test_content_allowlist",
        "new_skip_or_xfail",
        "test_selection_unmodified",
        "anti_cheating_path_pass",
        "anti_cheating_scripts_pass",
        "no_new_skip_or_only",
    }
)
_SCOPE_POLICY_CHECKS = frozenset(
    {
        "changed_path_outside_allowlist",
        "changed_path_allowlist",
    }
)


def policy_violation_checks(hardgate: dict[str, Any]) -> list[str]:
    """Return the sorted ids of every check that failed as a policy violation."""
    failed: set[str] = set()
    gates = hardgate.get("gates")
    if not isinstance(gates, dict):
        return []
    for gate in gates.values():
        if not isinstance(gate, dict):
            continue
        checks = gate.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict) or check.get("status") != "policy_violation":
                continue
            check_id = check.get("id")
            if isinstance(check_id, str) and check_id:
                failed.add(check_id)
    return sorted(failed)


def classify_policy_violation(hardgate: dict[str, Any]) -> tuple[str, list[str]]:
    """Classify a policy violation: deceptive > scope_violation > other."""
    failed = policy_violation_checks(hardgate)
    if any(check_id in _DECEPTIVE_POLICY_CHECKS for check_id in failed):
        return "deceptive", failed
    if any(check_id in _SCOPE_POLICY_CHECKS for check_id in failed):
        return "scope_violation", failed
    return "other_policy_violation", failed


def load_weights(config: dict[str, Any]) -> dict[str, float]:
    raw = config.get("final_reward", {}).get("weights")
    required = {"scope", "f2p"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("config final_reward.weights must contain scope and f2p")
    weights = {name: finite_score(raw[name]) for name in required}
    if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
        raise ValueError("config final_reward weights must sum to 1")
    return weights


def behavior_p2p_pass_rate(hardgate: dict[str, Any]) -> float | None:
    """P2P pass rate reported by the hard gate's behavior-regression gate."""
    gate = (hardgate.get("gates") or {}).get("behavior_regression")
    groups = gate.get("groups") if isinstance(gate, dict) else None
    p2p = groups.get("p2p") if isinstance(groups, dict) else None
    if not isinstance(p2p, dict):
        return None
    return optional_rate(p2p.get("pass_rate"))


def _validate_group(section: Any, expected_ids: list[str] | None, label: str) -> dict[str, Any]:
    """Validate one verifier pass-rate section and return it when consistent."""
    if not isinstance(section, dict):
        raise ValueError(f"verifier report missing {label} section")
    results = section.get("results")
    if not isinstance(results, list):
        raise ValueError(f"verifier {label}.results must be a list")
    result_ids = [item.get("node_id") for item in results if isinstance(item, dict)]
    if len(result_ids) != len(results) or len(result_ids) != len(set(result_ids)):
        raise ValueError(f"verifier {label}.results contains invalid or duplicate nodes")
    if expected_ids is not None and (set(result_ids) != set(expected_ids) or len(results) != len(expected_ids)):
        raise ValueError(f"verifier {label}.results does not match configured nodes")
    if section.get("total") != len(results):
        raise ValueError(f"verifier {label}.total does not match result nodes")
    passed = sum(1 for item in results if item.get("pass") is True)
    if section.get("passed") != passed:
        raise ValueError(f"verifier {label}.passed does not match result nodes")
    expected_rate = passed / len(results) if results else None
    rate = section.get("pass_rate")
    if rate is None:
        if expected_rate is not None:
            raise ValueError(f"verifier {label}.pass_rate is missing")
    elif expected_rate is None or not math.isclose(finite_score(rate), expected_rate, abs_tol=1e-9):
        raise ValueError(f"verifier {label}.pass_rate does not match result nodes")
    return section


def validate_state_preservation(verifier: dict[str, Any], config: dict[str, Any]) -> None:
    """Require state evidence to agree with the task's migration contract."""
    contract_value = config.get("migration_contract")
    if not isinstance(contract_value, str) or not contract_value:
        raise ValueError("config migration_contract must name the migration contract")
    contract_path = Path(contract_value)
    if not contract_path.is_file():
        local_contract = CONFIG.parent / "assets" / Path(contract_value).name
        if local_contract.is_file():
            contract_path = local_contract
        elif not contract_path.is_absolute():
            contract_path = CONFIG.parent / contract_path
    contract = load(contract_path)
    persistent_state = contract.get("persistent_state_involved")
    if not isinstance(persistent_state, bool):
        raise ValueError("migration contract persistent_state_involved must be boolean")

    evidence = verifier.get("required_state_preservation_complete")
    if not isinstance(evidence, dict):
        raise ValueError("verifier report missing required_state_preservation_complete")
    expected = "passed" if persistent_state else "na"
    if evidence.get("status") != expected:
        raise ValueError(
            "verifier required_state_preservation_complete.status must be "
            f"{expected!r} for this migration contract"
        )
    if not persistent_state and (
        not isinstance(evidence.get("reason"), str) or not evidence["reason"].strip()
    ):
        raise ValueError("verifier state-preservation na status requires a reason")


def validate_verifier_report(verifier: dict[str, Any], config: dict[str, Any]) -> None:
    """Reject incomplete or forged verifier reports before score aggregation.

    The final reward is the verifier score (``0.5*scope_pass_rate +
    0.5*f2p_pass_rate``), so the report must prove that both pass rates are
    derived from the configured node groups and that the score matches them.
    """
    verifier_cfg = config.get("verifier")
    if not isinstance(verifier_cfg, dict) or "required_nodes" not in verifier_cfg:
        return
    required = verifier_cfg.get("required_nodes")
    if not isinstance(required, list) or not required or any(not isinstance(item, str) or not item for item in required):
        raise ValueError("config verifier.required_nodes must be a non-empty string array")
    if len(required) != len(set(required)):
        raise ValueError("config verifier.required_nodes contains duplicates")

    validate_state_preservation(verifier, config)

    f2p_ids = config.get("f2p")
    if not isinstance(f2p_ids, list) or not f2p_ids or any(not isinstance(item, str) or not item for item in f2p_ids):
        raise ValueError("config f2p must be a non-empty string array")
    if len(f2p_ids) != len(set(f2p_ids)):
        raise ValueError("config f2p node list contains duplicates")
    expected_ids = ["migration_scope_complete", *f2p_ids]
    if set(required) != set(expected_ids) or len(required) != len(expected_ids):
        raise ValueError("config verifier.required_nodes does not match the migration scope and F2P node groups")

    nodes = verifier.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("verifier report nodes must be a list")
    node_ids = [node.get("node_id") for node in nodes if isinstance(node, dict)]
    if len(node_ids) != len(nodes):
        raise ValueError("verifier report contains a non-object node")
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("verifier report contains duplicate nodes")
    unknown = sorted(set(node_ids) - set(required))
    missing = sorted(set(required) - set(node_ids))
    if unknown or missing or len(nodes) != len(required):
        raise ValueError(f"verifier node coverage mismatch (missing={missing}, unknown={unknown})")

    # Scope results are migration-contract units the grader does not enumerate;
    # only shape, counts, and the completion flag are checked for that section.
    scope = _validate_group(verifier.get("scope"), None, "scope")
    complete = bool(scope["results"]) and scope["passed"] == scope["total"]
    if scope.get("complete") is not complete:
        raise ValueError("verifier scope.complete does not match result nodes")
    f2p = _validate_group(verifier.get("f2p"), f2p_ids, "f2p")

    score = verifier.get("score")
    if score is not None:
        measured = finite_score(score)
        scope_rate = finite_score(scope.get("pass_rate"), allow_none=True)
        f2p_rate = finite_score(f2p.get("pass_rate"), allow_none=True)
        if scope_rate is None or f2p_rate is None:
            raise ValueError("verifier score is present but a pass rate is missing")
        expected_score = round(0.5 * scope_rate + 0.5 * f2p_rate, 6)
        if not math.isclose(measured, expected_score, abs_tol=1e-6):
            raise ValueError("verifier score does not match the configured scope/f2p formula")


def terminal_evidence(
    *,
    status: str,
    termination_reason: str,
    hardgate: dict[str, Any],
    verifier: dict[str, Any],
) -> dict[str, Any]:
    """Normalize the process evidence for every terminal grading branch."""
    gate_passed = hardgate.get("hard_gate_passed") is True
    if hardgate and not gate_passed and status in {
        "policy_violation",
        "hard_gate_failed",
        "configuration_error",
        "infrastructure_error",
    }:
        source = hardgate.get("runner", {})
        stage = source.get("stage", "hard_gate")
        defaults = {
            "command": ["python3", "/tests/verifier/hardgate.py"],
            "stdout_path": "/logs/verifier/hardgate.stdout",
            "stderr_path": "/logs/verifier/hardgate.stderr",
            "report_path": "/logs/verifier/hard-gate.json",
        }
    elif gate_passed and status in {"verifier_failed", "configuration_error", "infrastructure_error"}:
        stage = "verifier"
        source = verifier.get("runner", {})
        defaults = {
            "command": ["python3", "/tests/verifier/verifier.py"],
            "stdout_path": "/logs/verifier/verifier.stdout",
            "stderr_path": "/logs/verifier/verifier.stderr",
            "report_path": "/logs/verifier/verifier-result.json",
        }
    elif status == "measured":
        stage = None
        source = {"exit_code": 0, "timed_out": False, "command": ["/tests/test.sh"]}
        defaults = {
            "stdout_path": None,
            "stderr_path": None,
            "report_path": "/logs/verifier/reward-details.json",
        }
    else:
        stage = os.environ.get("HARBOR_FAILED_STAGE", "test_orchestration")
        raw_exit = os.environ.get("HARBOR_FAILURE_EXIT_CODE")
        source = {
            "exit_code": int(raw_exit) if raw_exit and raw_exit.lstrip("-").isdigit() else None,
            "timed_out": False,
            "command": ["/tests/test.sh"],
        }
        defaults = {
            "stdout_path": "/logs/verifier/test.stdout",
            "stderr_path": "/logs/verifier/test.stderr",
            "report_path": "/logs/verifier/reward-details.json",
        }

    return {
        "failed_stage": stage,
        "evaluation_status": status,
        "termination_reason": termination_reason,
        "exit_code": source.get("exit_code"),
        "timed_out": bool(source.get("timed_out", False)),
        "command": source.get("command", defaults.get("command")),
        "stdout_path": source.get("stdout_path", defaults.get("stdout_path")),
        "stderr_path": source.get("stderr_path", defaults.get("stderr_path")),
        "report_path": source.get("report_path", defaults.get("report_path")),
    }


def required_reward_fields(
    report: dict[str, Any],
    details: dict[str, Any],
    verifier: dict[str, Any],
    hardgate: dict[str, Any],
) -> dict[str, Any]:
    """Compute the mandatory reward-details fields shared by every terminal path.

    ``write`` applies the result with ``setdefault`` so the richer values that
    ``main`` derives from the grading context always win over these fallbacks.
    """

    def numeric(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return float(value)
        return None

    def pass_ratio(section: Any) -> float | None:
        return numeric(section.get("pass_rate")) if isinstance(section, dict) else None

    status = details.get("status")
    termination_reason = details.get("termination_reason")
    # Defensive hook: the verifier cannot observe an agent timeout from its own
    # inputs, so the orchestrator exports HARBOR_AGENT_TIMEOUT on the timeout
    # terminal path and the grader has to surface it in the reward details.
    agent_time_out = os.environ.get("HARBOR_AGENT_TIMEOUT", "").strip().lower() in {"1", "true", "yes"}
    infrastructure_error = bool(
        status in _EVALUATOR_FAILURE_STATUSES
        or termination_reason in {
            "unexpected_error",
            "grader_error",
            "asset_integrity_failed",
        }
        or (
            isinstance(termination_reason, str)
            and termination_reason.endswith(("_infrastructure_error", "_configuration_error"))
        )
        or verifier.get("status") in _EVALUATOR_FAILURE_STATUSES
    )
    # Build/compile failures are derived from the target_build_valid gate so the
    # reward details distinguish them from generic candidate failures.
    failed_checks = {
        check.get("id")
        for gate in (hardgate.get("gates") or {}).values()
        if isinstance(gate, dict)
        for check in (gate.get("checks") or [])
        if isinstance(check, dict) and check.get("status") not in (None, "passed", "blocked")
    }
    compile_error = bool(failed_checks & {"syntax_check_bin_app", "syntax_check_bin_index"})
    build_error = bool(failed_checks & {
        "dependency_resolved",
        "target_dependency_probe",
        "dependency_prewarm",
        "lockfile_present",
        "lockfile_valid_json",
        "lockfile_engine_preflight",
        "dependency_engine_compatibility",
        "lockfile_target_deps",
    })
    return {
        "cheating_detected": details.get("violation_class") == "deceptive",
        "test": numeric(report.get("test")),
        "migration_scope_complete_ratio": numeric(report.get("scope")),
        "f2p_pass_ratio": pass_ratio(verifier.get("f2p")),
        "p2p_pass_ratio": behavior_p2p_pass_rate(hardgate),
        "reward": numeric(report.get("reward")),
        "errors": {
            "infrastructure_error": infrastructure_error,
            "agent_time_out": agent_time_out,
            "build error": build_error,
            "compile error": compile_error,
        },
    }


def write(report: dict[str, Any], details: dict[str, Any], verifier: dict[str, Any]) -> None:
    LOG.mkdir(parents=True, exist_ok=True)
    raw_hardgate = details.get("hardgate")
    hardgate = raw_hardgate if isinstance(raw_hardgate, dict) else {}
    raw_verifier = details.get("verifier")
    context_verifier = raw_verifier if isinstance(raw_verifier, dict) and raw_verifier else verifier
    details.setdefault(
        "terminal_evidence",
        terminal_evidence(
            status=str(details.get("status", "infrastructure_error")),
            termination_reason=str(details.get("termination_reason", "unknown")),
            hardgate=hardgate,
            verifier=context_verifier,
        ),
    )
    errors = details.get("errors")
    if not isinstance(errors, dict):
        errors = {}
        details["errors"] = errors
    for name, value in required_reward_fields(report, details, verifier, hardgate).items():
        if name == "errors":
            for error_name, error_value in value.items():
                errors.setdefault(error_name, error_value)
        else:
            details.setdefault(name, value)
    # Unavailable scores stay null in the detailed report; the transport map keeps
    # every component key so downstream readers never hit a missing "reward".
    reward = report.get("reward")
    reward_available = isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(float(reward))
    semantic_reward = float(reward) if reward_available else None
    details["reward"] = semantic_reward
    details["reward_available"] = reward_available
    details["transport_reward"] = semantic_reward if reward_available else 0.0
    details["transport_encoding"] = "component_reward_map"
    details["transport_note"] = (
        "reward.json always carries the component map; reward 0.0 together with "
        "reward_available=false means the final reward was not measured"
    )
    reward_transport = {
        "test": float(report.get("test") or 0.0),
        "migration_scope_complete_ratio": float(report.get("scope") or 0.0),
        "f2p_pass_ratio": float(report.get("f2p") or 0.0),
        "reward": semantic_reward if reward_available else 0.0,
    }
    (LOG / "reward.json").write_text(json.dumps(reward_transport, indent=2) + "\n", encoding="utf-8")
    (LOG / "reward-details.json").write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
    (LOG / "ctrf.json").write_text(
        json.dumps(
            {
                "status": details.get("status"),
                "score": report.get("test"),
                "tests": verifier.get("nodes", []),
                "scope": verifier.get("scope", {}),
                "f2p": verifier.get("f2p", {}),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate Harbor task scores into final reward")
    parser.add_argument("--reason", default="scored")
    args = parser.parse_args()

    if args.reason == "unexpected_error":
        write(
            {"test": None, "scope": None, "f2p": None, "reward": None},
            {
                "status": "infrastructure_error",
                "termination_reason": "unexpected_error",
                "failed_stage": "test_orchestration",
                "error": "test.sh exited before reaching a terminal grading branch",
            },
            {},
        )
        return 2

    try:
        config = load(CONFIG)
        weights = load_weights(config)
        hardgate = load(LOG / "hard-gate.json")
        verifier = load(LOG / "verifier-result.json", required=False)

        gate_ok = hardgate.get("hard_gate_passed") is True
        raw_status = hardgate.get("status", "candidate_failure")
        policy_reasons = hardgate.get("evidence", {}).get("policy_reasons", [])
        if not isinstance(policy_reasons, list):
            policy_reasons = []

        is_policy = raw_status == "policy_violation"

        violation_class: str | None = None
        violation_checks: list[str] = []
        if is_policy:
            violation_class, violation_checks = classify_policy_violation(hardgate)
        cheating_detected = violation_class == "deceptive"

        test: float | None = None
        scope_rate: float | None = None
        f2p_rate: float | None = None
        termination_reason = "unknown"

        if cheating_detected:
            status = "policy_violation"
            reward = -1.0
            termination_reason = "hard_gate_policy_violation"
        elif raw_status in _EVALUATOR_FAILURE_STATUSES:
            # The hard gate did not measure the candidate when the evaluator failed.
            status = raw_status
            reward = None
            termination_reason = f"hard_gate_{raw_status}"
        elif not gate_ok:
            status = "hard_gate_failed"
            reward = 0.0
            termination_reason = (
                f"hard_gate_{violation_class}" if violation_class else "hard_gate_failed"
            )
        elif not verifier:
            # The hard gate passed but no verifier report exists: failed closed
            # and reported honestly, never scored as zero or one.
            status = "infrastructure_error"
            reward = None
            termination_reason = "verifier_unavailable"
        elif verifier.get("status") in _EVALUATOR_FAILURE_STATUSES:
            status = str(verifier["status"])
            reward = None
            termination_reason = f"verifier_{verifier['status']}"
        else:
            validate_verifier_report(verifier, config)
            scope_rate = finite_score(verifier.get("scope", {}).get("pass_rate"), allow_none=True)
            f2p_rate = finite_score(verifier.get("f2p", {}).get("pass_rate"), allow_none=True)
            test = finite_score(verifier.get("score"), allow_none=True)
            if test is None or scope_rate is None or f2p_rate is None:
                # The hard gate passed but the verifier left a component
                # unmeasured: fail closed, never invent a score.
                status = "infrastructure_error"
                reward = None
                termination_reason = "verifier_score_unavailable"
            else:
                reward = test
                if verifier.get("success") is True:
                    status = "measured"
                    termination_reason = "scored"
                else:
                    status = "verifier_failed"
                    termination_reason = "verifier_candidate_failure"

        report = {
            "test": test,
            "scope": scope_rate,
            "f2p": f2p_rate,
            "reward": reward,
        }
        details = {
            "status": status,
            "termination_reason": termination_reason,
            "formula": (
                "cheating=-1; hard_gate_failure=0; "
                f"gate_passed={weights['scope']:.6g}*scope_pass_rate"
                f"+{weights['f2p']:.6g}*f2p_pass_rate (verifier.score)"
            ),
            "weights": weights,
            "policy_reasons": policy_reasons,
            "violation_class": violation_class,
            "violation_checks": violation_checks,
            "cheating_detected": cheating_detected,
            "hardgate": hardgate,
            "verifier": verifier,
            "reward_details": str(LOG / "reward-details.json"),
        }
        details["terminal_evidence"] = terminal_evidence(
            status=status,
            termination_reason=termination_reason,
            hardgate=hardgate,
            verifier=verifier,
        )
        write(report, details, verifier)
    except (OSError, ValueError, TypeError) as exc:
        write(
            {"test": None, "scope": None, "f2p": None, "reward": None},
            {"status": "infrastructure_error", "termination_reason": "grader_error", "error": str(exc)},
            {},
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
