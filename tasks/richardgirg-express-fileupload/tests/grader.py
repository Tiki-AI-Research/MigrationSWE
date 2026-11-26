#!/usr/bin/env python3
"""Reward aggregation for the express-fileupload -> Koa 2 migration task.

Four branches (see the migration dataset spec v1.2):

    cheating (integrity atom failed)         -> reward = -1
    other hard gate failure                  -> reward = 0
    hard gate pass, verifier failed          -> reward = T_weight * T
    hard gate pass, verifier success         -> full reward (VERIFIER_ONLY: 1.0)

``T`` is the *weighted average* of the verifier's three normalized components
(migration scope coverage, F2P pass ratio and — when the task declares
persistent state — the state-preservation ratio), renormalized over the
applicable ones. P2P no longer enters ``T``: it is the boolean hard-gate atom
``p2p_regression_preserved.p2p_no_regression``. Nothing here trusts a
self-declared verifier success: the per-node report is the source of truth and
is cross-checked against the verifier result, the CTRF summary and the
configured node registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GATE_SCHEMA = "migration-gate/v1"
DETAILS_SCHEMA = "deepswe-reward-details/v1"
VERIFIER_ONLY_POLICY = "deterministic_verifier_only"
VALID_STATUSES = {"passed", "failed", "skipped", "missing", "not_run"}


def read_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, dict) else None


def write_json(path: str, payload: Dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def config_nodes(config: Dict[str, Any], key: str) -> List[str]:
    value = config.get(key)
    return list(value) if isinstance(value, list) else []


def node_report_map(report: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Return {node_id: entry} plus a list of duplicate ids."""
    mapping: Dict[str, Dict[str, Any]] = {}
    duplicates: List[str] = []
    if not report:
        return mapping, duplicates
    for section in ("f2p", "p2p"):
        for entry in report.get(section) or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            if entry["id"] in mapping:
                duplicates.append(entry["id"])
            mapping[entry["id"]] = entry
    return mapping, duplicates


def ratios(nodes: List[str], statuses: Dict[str, Dict[str, Any]]) -> Tuple[int, int, float]:
    passed = 0
    for node in nodes:
        entry = statuses.get(node)
        if entry and entry.get("status") == "passed":
            passed += 1
    total = len(nodes)
    return passed, total, (passed / total if total else 0.0)


def patch_apply_failed(hard_gate: Optional[Dict[str, Any]]) -> bool:
    """True when the candidate patch is absent or could not be applied."""
    if not hard_gate:
        return True
    atoms = (((hard_gate.get("checks") or {}).get("candidate_valid") or {}).get("atoms") or {})
    for atom in ("patch_present", "patch_applicable"):
        if (atoms.get(atom) or {}).get("status") == "fail":
            return True
    return False


def gate_verifier_scores(gate: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The verifier sub-scores as recorded by ``compute_gate``."""
    declared = (gate or {}).get("verifier_scores")
    entries: List[Dict[str, Any]] = []
    if isinstance(declared, list):
        for value in declared:
            if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                continue
            score = value.get("score")
            weight = value.get("weight")
            entries.append({
                "id": str(value["id"]),
                "score": float(score) if isinstance(score, (int, float)) else None,
                "weight": float(weight) if isinstance(weight, (int, float)) else 1.0,
                "applicable": bool(value.get("applicable", score is not None)),
                "status": str(value["status"]) if isinstance(value.get("status"), str) else None,
                "applicability": (
                    str(value["applicability"]) if isinstance(value.get("applicability"), str) else None
                ),
            })
    return entries


def verifier_score_entries(
    verifier_result: Optional[Dict[str, Any]],
    f2p_ratio: float,
    scope_coverage: float,
) -> List[Dict[str, Any]]:
    """The verifier's applicable components with their weights.

    Prefers the verifier's own ``scores`` block (it holds the declared weights
    and the conditional components) and falls back to re-deriving the two
    unconditional ones so a report from an older revision still grades.
    """
    declared = (verifier_result or {}).get("scores")
    entries: List[Dict[str, Any]] = []
    if isinstance(declared, dict) and declared:
        for key, value in declared.items():
            if not isinstance(value, dict):
                continue
            score = value.get("score")
            if score is not None and not isinstance(score, (int, float)):
                continue
            weight = value.get("weight")
            if not isinstance(weight, (int, float)):
                weight = 1.0
            entries.append({
                "id": str(key),
                "score": None if score is None else float(score),
                "weight": float(weight),
                "applicable": bool(value.get("applicable", score is not None)),
                "status": str(value["status"]) if isinstance(value.get("status"), str) else None,
                "applicability": (
                    str(value["applicability"]) if isinstance(value.get("applicability"), str) else None
                ),
            })
        if entries:
            return entries
    return [
        {"id": "migration_scope_complete", "score": scope_coverage, "weight": 1.0,
         "applicable": True, "status": "pass" if float(scope_coverage) == 1.0 else "fail",
         "applicability": "applicable"},
        {"id": "f2p", "score": f2p_ratio, "weight": 1.0,
         "applicable": True, "status": "pass" if float(f2p_ratio) == 1.0 else "fail",
         "applicability": "applicable"},
        # The report did not carry this component at all. An omission is *not*
        # an explicit ``na``: keep it in the weighted average as a 0 so a check
        # that was never reported can never be silently dropped from ``T``.
        {"id": "required_state_preservation_complete", "score": 0.0, "weight": 1.0,
         "applicable": True, "status": "indeterminate",
         "applicability": "indeterminate"},
    ]


def verifier_t_value(
    verifier_result: Optional[Dict[str, Any]],
    f2p_ratio: float,
    scope_coverage: float,
) -> float:
    """Weighted average over the applicable verifier components."""
    reported = (verifier_result or {}).get("t_value")
    if isinstance(reported, (int, float)):
        return float(reported)
    entries = verifier_score_entries(verifier_result, f2p_ratio, scope_coverage)
    applicable = [entry for entry in entries if entry["applicable"] and entry["score"] is not None]
    total_weight = sum(entry["weight"] for entry in applicable)
    if not applicable or total_weight <= 0:
        return 0.0
    return sum(entry["score"] * entry["weight"] for entry in applicable) / total_weight


def compute_gate(args: argparse.Namespace) -> int:
    config = read_json(args.config)
    if config is None:
        write_json(args.output, {
            "schema_version": GATE_SCHEMA,
            "evaluation_valid": False,
            "hard_gate_success": 0,
            "verifier_success": False,
            "termination_reason": "infrastructure_error",
            "error": "verifier config is missing or unparsable",
        })
        return 2

    f2p_nodes = config_nodes(config, "f2p")
    p2p_nodes = config_nodes(config, "p2p")

    hard_gate = read_json(args.hard_gate)
    if hard_gate is None:
        write_json(args.output, {
            "schema_version": GATE_SCHEMA,
            "task_id": config.get("task_id"),
            "evaluation_valid": False,
            "errors": ["hard gate report missing or unparsable"],
            "hard_gate_success": 0,
            "hard_gate_present": False,
            "cheating_detected": False,
            "verifier_success": False,
            "termination_reason": "infrastructure_error",
        })
        return 2

    hard_gate_infrastructure_error = bool(hard_gate.get("infrastructure_error"))
    hard_gate_success = bool(
        hard_gate.get("success") is True
        and hard_gate.get("status") == "pass"
        and not hard_gate_infrastructure_error
    )
    cheating = bool(hard_gate.get("cheating_detected") is True)
    apply_failed = bool(hard_gate_infrastructure_error or patch_apply_failed(hard_gate))

    # F2P only runs after the hard gate passes. P2P may already have run inside
    # the gate even when another atom failed; retain that measured evidence.
    # It remains informational and cannot change the gate's reward branch.
    if not hard_gate_success:
        if hard_gate_infrastructure_error:
            evaluation_valid = False
            termination = "infrastructure_error"
            errors = [str(hard_gate.get("infrastructure_error"))]
        elif cheating:
            evaluation_valid = True
            termination = "cheating"
            errors = []
        else:
            evaluation_valid = True
            termination = "hard_gate_failed"
            errors = []
        verifier_result = read_json(args.verifier_result)
        node_report = read_json(args.node_report)
        ctrf = read_json(args.ctrf)
        all_nodes = [*f2p_nodes, *p2p_nodes]
        evidence = hard_gate.get("p2p_nodes")
        evidence = evidence if isinstance(evidence, dict) else {}
        p2p_statuses = {node: entry for node, entry in evidence.items()
                        if node in p2p_nodes and isinstance(entry, dict)
                        and entry.get("status") in VALID_STATUSES}
        p2p_passed, p2p_total, p2p_ratio = ratios(p2p_nodes, p2p_statuses)
        failed_nodes = [node for node in all_nodes
                        if (p2p_statuses.get(node) or {}).get("status") != "passed"]
        not_run_nodes = [node for node in all_nodes
                         if node in f2p_nodes or (p2p_statuses.get(node) or {}).get("status", "not_run") == "not_run"]
        gate = {
            "schema_version": GATE_SCHEMA,
            "task_id": config.get("task_id"),
            "evaluation_valid": evaluation_valid,
            "errors": errors,
            "hard_gate_success": 0,
            "hard_gate_status": hard_gate.get("status"),
            "hard_gate_present": True,
            "cheating_detected": cheating,
            "apply_failed": apply_failed,
            "verifier_declared_success": False,
            "verifier_success": False,
            "verifier_status": (verifier_result or {}).get("status"),
            "f2p_pass_ratio": 0.0,
            "p2p_pass_ratio": p2p_ratio,
            "regression_pass_ratio": p2p_passed / len(all_nodes) if all_nodes else 0.0,
            "scope_coverage": 0.0,
            "t_value": 0.0,
            "verifier_scores": [],
            "missing_components": [],
            "f2p": {"passed": 0, "total": len(f2p_nodes)},
            "p2p": {"passed": p2p_passed, "total": p2p_total},
            "failed_nodes": failed_nodes,
            "failed_node_count": len(failed_nodes),
            "not_run_nodes": not_run_nodes,
            "missing_nodes": [node for node in p2p_nodes
                              if (p2p_statuses.get(node) or {}).get("status") == "missing"],
            "duplicate_nodes": [],
            "skipped_nodes": [node for node in p2p_nodes
                              if (p2p_statuses.get(node) or {}).get("status") == "skipped"],
            "p2p_requires_all": bool((config.get("hard_gate") or {}).get("p2p_requires_all", True)),
            "termination_reason": termination,
        }
        write_json(args.output, gate)
        return 0

    # Hard gate passed: the verifier must have produced complete evidence.
    verifier_result = read_json(args.verifier_result)
    node_report = read_json(args.node_report)
    ctrf = read_json(args.ctrf)
    statuses, duplicates = node_report_map(node_report)

    errors: List[str] = []
    if node_report is None:
        errors.append("node report missing or unparsable")
    if ctrf is None:
        errors.append("ctrf report missing or unparsable")
    if verifier_result is None:
        errors.append("verifier result missing or unparsable")
    elif verifier_result.get("infrastructure_error"):
        errors.append(f"verifier reported an infrastructure error: {verifier_result['infrastructure_error']}")

    missing_nodes = [node for node in [*f2p_nodes, *p2p_nodes] if node not in statuses]
    invalid_status = [
        node for node, entry in statuses.items()
        if entry.get("status") not in VALID_STATUSES
    ]
    if duplicates:
        errors.append("duplicate node entries in the node report")
    if missing_nodes:
        errors.append(f"{len(missing_nodes)} configured nodes are missing from the node report")
    if invalid_status:
        errors.append("node report contains invalid status values")

    f2p_passed, f2p_total, f2p_ratio = ratios(f2p_nodes, statuses)
    p2p_passed, p2p_total, p2p_ratio = ratios(p2p_nodes, statuses)
    regression_passed = f2p_passed + p2p_passed
    regression_total = f2p_total + p2p_total
    regression_ratio = regression_passed / regression_total if regression_total else 0.0

    scope = ((verifier_result or {}).get("checks") or {}).get("migration_scope_complete") or {}
    scope_coverage = scope.get("coverage")
    if not isinstance(scope_coverage, (int, float)):
        # Fall back to recomputing from units if the verifier omitted it.
        units = scope.get("units") or []
        scope_coverage = (sum(1 for unit in units if unit.get("status") == "pass") / len(units)) if units else 0.0

    # ``T`` is the verifier's weighted average over its applicable components
    # (scope coverage, F2P ratio, conditional state preservation). The verifier
    # computes it; here it is only re-derived when the report is too old to
    # carry it, and P2P is deliberately absent from both paths.
    t_value = verifier_t_value(verifier_result, f2p_ratio, float(scope_coverage))
    verifier_scores = verifier_score_entries(verifier_result, f2p_ratio, float(scope_coverage))

    # A component the verifier was supposed to report but did not is an
    # *omission*, not an explicit ``na``: an unreported check must not be able to
    # vanish from ``T`` (and from the audit trail) without failing the run.
    declared_components = [
        str(component)
        for component in ((config.get("verifier") or {}).get("components") or [])
    ]
    if not declared_components:
        declared_components = [entry["id"] for entry in verifier_scores]
    present_ids = {entry["id"] for entry in verifier_scores}
    missing_components = [
        component for component in declared_components if component not in present_ids
    ]
    if missing_components:
        errors.append("verifier omitted declared component(s): " + ", ".join(missing_components))

    # ``p2p_requires_all`` is a declaration, not documentation: it decides
    # whether a partial regression suite can still count as a completed
    # migration. It lives in the hard gate now, so the grader only checks that
    # the gate actually ran the atom.
    p2p_requires_all = bool((config.get("hard_gate") or {}).get("p2p_requires_all", True))
    verification_declared = bool(verifier_result and verifier_result.get("success") is True)
    verifier_consistent = (
        verifier_result is not None
        and not missing_nodes
        and not duplicates
        and not invalid_status
        and not missing_components
        and f2p_ratio == 1.0
        and float(scope_coverage) == 1.0
        # An ``indeterminate`` applicability is applicable-but-failed, so this
        # also rejects "we could not tell whether the check applies".
        and all(entry["score"] == 1.0 for entry in verifier_scores if entry["applicable"])
    )
    verifier_success = bool(verification_declared and verifier_consistent)

    # CTRF cross-check: the canonical report must agree with the node report.
    ctrf_consistent = True
    if ctrf is not None:
        summary = ((ctrf.get("results") or {}).get("summary") or {})
        if summary.get("tests") != f2p_total + p2p_total:
            ctrf_consistent = False
        if summary.get("passed") != regression_passed:
            ctrf_consistent = False
        if summary.get("failed") != (f2p_total + p2p_total - regression_passed):
            ctrf_consistent = False
    if not ctrf_consistent:
        errors.append("ctrf summary disagrees with the node report")

    evaluation_valid = not errors

    failed_nodes = [
        node for node in [*f2p_nodes, *p2p_nodes]
        if (statuses.get(node) or {}).get("status") != "passed"
    ]
    skipped_nodes = [
        node for node in [*f2p_nodes, *p2p_nodes]
        if (statuses.get(node) or {}).get("status") == "skipped"
    ]

    if cheating:
        termination = "cheating"
    elif not evaluation_valid:
        termination = "infrastructure_error"
    elif not hard_gate_success:
        termination = "hard_gate_failed"
    elif not verifier_success:
        termination = "verifier_failed"
    else:
        termination = "verifier_success"

    gate = {
        "schema_version": GATE_SCHEMA,
        "task_id": config.get("task_id"),
        "evaluation_valid": evaluation_valid,
        "errors": errors,
        "hard_gate_success": 1 if hard_gate_success else 0,
        "hard_gate_status": (hard_gate or {}).get("status"),
        "hard_gate_present": hard_gate is not None,
        "cheating_detected": cheating,
        "apply_failed": apply_failed,
        "verifier_declared_success": verification_declared,
        "verifier_success": verifier_success,
        "verifier_status": (verifier_result or {}).get("status"),
        "f2p_pass_ratio": f2p_ratio,
        "p2p_pass_ratio": p2p_ratio,
        "regression_pass_ratio": regression_ratio,
        "scope_coverage": float(scope_coverage),
        "t_value": t_value,
        "verifier_scores": verifier_scores,
        "missing_components": missing_components,
        "f2p": {"passed": f2p_passed, "total": f2p_total},
        "p2p": {"passed": p2p_passed, "total": p2p_total},
        "failed_nodes": failed_nodes,
        "failed_node_count": len(failed_nodes),
        "missing_nodes": missing_nodes,
        "duplicate_nodes": sorted(set(duplicates)),
        "skipped_nodes": skipped_nodes,
        "p2p_requires_all": p2p_requires_all,
        "termination_reason": termination,
    }
    write_json(args.output, gate)
    return 0


def sha256_file(path: Path) -> Optional[str]:
    """sha256 of a file, or None when it is absent/unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def collect_bindings(
    config: Dict[str, Any],
    hard_gate: Optional[Dict[str, Any]],
    model_patch: Optional[str],
) -> Dict[str, Any]:
    """Bind the report to the candidate, the image and the tool revision (VER-034).

    A reward that cannot be traced back to a concrete candidate patch, a concrete
    environment image and a concrete tool revision is not reproducible, so the
    four artefacts are hashed into the report itself.
    """
    tests_dir = Path(__file__).resolve().parent
    patch_path = Path(
        model_patch
        or os.environ.get("MIGRATION_MODEL_PATCH")
        or "/logs/artifacts/model.patch"
    )
    tool_files: Dict[str, Any] = {}
    for rel in (
        "grader.py",
        "test.sh",
        "config.json",
        "verifier/verifier.py",
        "verifier/hardgate.py",
        "verifier/semantic_scan.py",
        "verifier/js_module_scan.py",
        "verifier/test.patch",
        "verifier/runtime_probe.js",
        "assets/base_tree.sha256.json",
    ):
        target = tests_dir / rel
        tool_files[rel] = {
            "sha256": sha256_file(target),
            "bytes": target.stat().st_size if target.is_file() else None,
        }
    base_manifest = tests_dir / "assets" / "base_tree.sha256.json"
    identity = (hard_gate or {}).get("base_identity") or {}
    return {
        "task": {
            "task_id": config.get("task_id"),
            "base_commit": config.get("base_commit"),
        },
        "candidate": {
            "model_patch": str(patch_path),
            "sha256": sha256_file(patch_path),
            "bytes": patch_path.stat().st_size if patch_path.is_file() else None,
        },
        "environment": {
            "image": config.get("environment_image") or os.environ.get("MIGRATION_IMAGE"),
            "image_source": "config.environment_image",
            "runtime": {"node": os.environ.get("MIGRATION_NODE") or None, "python": sys.version.split()[0]},
        },
        "tool": {
            "files": tool_files,
            "base_manifest_sha256": sha256_file(base_manifest),
            "base_identity_method": identity.get("method"),
            "base_identity_checked_files": identity.get("checked_files"),
        },
        "evaluator": {
            "effective_user": os.environ.get("MIGRATION_EFFECTIVE_USER"),
        },
    }


def build_details(
    config: Dict[str, Any],
    hard_gate: Optional[Dict[str, Any]],
    *,
    evaluation_valid: bool,
    evaluation_success: bool,
    apply_failed: bool,
    termination: str,
    reward: float,
    reward_mode: str,
    components: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
    bindings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not evaluation_valid:
        status = "evaluation_invalid"
    elif evaluation_success:
        status = "passed"
    else:
        status = "candidate_failed"
    details: Dict[str, Any] = {
        "schema_version": DETAILS_SCHEMA,
        "task_id": config.get("task_id"),
        "status": status,
        "evaluation_valid": bool(evaluation_valid),
        "evaluation_success": bool(evaluation_success),
        "scoring_executed": False,
        "skip_reason": "verifier_only",
        "policy": VERIFIER_ONLY_POLICY,
        "policy_version": "v2.3",
        "reward_mode": reward_mode,
        "components": components,
        "reward": float(reward),
        "apply_failed": bool(apply_failed),
        "termination_reason": termination,
    }
    if extra:
        details.update(extra)
    if bindings:
        # VER-034: a reward without provenance is not reproducible. Bind the
        # report to the exact candidate patch, environment image and tool
        # revision that produced it.
        details["bindings"] = bindings
    # Record the identity the evaluator actually ran as. The report is the only
    # place a silent privilege change (or a wrong sandbox user) would show up.
    effective_user = os.environ.get("MIGRATION_EFFECTIVE_USER")
    if effective_user:
        details["effective_user"] = effective_user
    # Privilege separation is only meaningful if it is auditable: a silent
    # fallback to root must be visible in the report, not just in a warning.
    details["sandbox"] = {
        "requested_user": os.environ.get("MIGRATION_SANDBOX_USER"),
        "effective_user": effective_user,
        "hard_gate": (hard_gate or {}).get("sandbox"),
    }
    return details


def finalize(args: argparse.Namespace) -> int:
    """Write the platform report bundle: reward-details.json (+ reward.json).

    ``reward-details.json`` is always written so every run stays auditable.
    ``reward.json`` is only written for a valid evaluation: the harness forbids
    a valid ``reward.json`` when the evaluation is invalid, in which case the
    caller must write ``reward.txt`` = -1 and exit non-zero instead.
    """
    config = read_json(args.config) or {}
    gate = read_json(args.gate)
    hard_gate = read_json(args.hard_gate)

    reward_mode = str(config.get("reward_mode") or "VERIFIER_ONLY")
    weights = config.get("weights") or {}
    t_weight = float(config.get("t_weight", weights.get("test", 1.0)))

    if gate is None:
        details = build_details(
            config, hard_gate,
            evaluation_valid=False, evaluation_success=False, apply_failed=True,
            termination="infrastructure_error", reward=0.0, reward_mode=reward_mode,
            components={"test": {"score": 0.0, "weight": 1.0}},
            extra={
                "error": "gate result is missing or unparsable",
                "cheating_detected": False,
                "f2p_pass_ratio": 0.0,
                "p2p_pass_ratio": 0.0,
                "migration_scope_complete_ratio": 0.0,
            },
            bindings=collect_bindings(config, hard_gate, args.model_patch),
        )
        write_json(args.output_details, details)
        return 2

    evaluation_valid = gate.get("evaluation_valid") is True
    hard_gate_success = gate.get("hard_gate_success") == 1
    cheating = gate.get("cheating_detected") is True
    verifier_success = gate.get("verifier_success") is True
    t_value = float(gate.get("t_value") or 0.0)
    apply_failed = bool(gate.get("apply_failed"))

    if cheating:
        termination = "cheating"
    elif not evaluation_valid:
        termination = "infrastructure_error"
    elif not hard_gate_success:
        termination = "hard_gate_failed"
    elif not verifier_success:
        termination = "verifier_failed"
    else:
        termination = "verifier_success"

    # Four-branch business reward, quoted from the migration dataset spec

    #   cheating (integrity atom failed)      -> -1 (covers the whole formula)
    #   other hard gate failure               -> 0
    #   hard gate pass + verifier failed      -> T_weight * T
    #   hard gate pass + verifier success     -> 1.0 (VERIFIER_ONLY has no O/R)
    # Non-cheating branches keep the quality-control doc B5.1 value 0 for a
    # plain hard-gate failure; only the anti-cheating branch is -1.
    if cheating:
        business_reward = -1.0
    elif not hard_gate_success:
        business_reward = 0.0
    elif not verifier_success:
        business_reward = t_weight * t_value
    else:
        business_reward = 1.0

    if not evaluation_valid:
        reward = 0.0
        evaluation_success = False
        components: Dict[str, Any] = {"test": {"score": 0.0, "weight": 1.0}}
    else:
        evaluation_success = bool(hard_gate_success and verifier_success)
        # Four-branch reward, quoted from the V1.2 spec:
        #   cheating                     -> -1 (covers the whole formula)
        #   other hard gate failure      -> 0
        #   gate pass + verifier failed  -> T_weight * T (partial credit)
        #   gate pass + verifier success -> 1
        # T_weight = 1 here, so a partially-complete migration keeps its
        # continuous test_reward instead of collapsing to 0.
        reward = -1.0 if cheating else (1.0 if evaluation_success else business_reward)
        reward = max(-1.0, min(1.0, float(reward)))
        # ``items`` are the verifier's own sub-scores, so the report explains
        # exactly how ``T`` was produced. P2P is absent on purpose: it is the
        # hard gate's boolean floor and contributes no score.
        # Each item carries its own ``status`` inside the evidence string (the
        # ``components.test.items`` schema only allows id/score/evidence), so an
        # explicitly not-applicable check shows up as ``status na`` instead of a
        # silent 0.
        items = []
        for entry in gate_verifier_scores(gate):
            status = entry.get("status") or ("na" if not entry["applicable"] else "scored")
            if entry["applicable"] and entry["score"] is not None:
                evidence = (f"status {status}; weight {entry['weight']:g}, "
                            f"score {float(entry['score']):.4f}")
            else:
                evidence = f"status {status}; not applicable, excluded from T"
            items.append({
                "id": entry["id"],
                "score": float(entry["score"]) if entry["score"] is not None else 0.0,
                "evidence": evidence,
            })
        components = {
            "test": {
                # ``test`` is a plain [0, 1] progress score: -1 (cheating) has
                # no progress value, so the component floors at 0 while the
                # top-level ``reward`` keeps the -1 penalty.
                "score": max(0.0, reward),
                "weight": 1.0,
                "items": items,
            }
        }
    state_entry = next(
        (entry for entry in gate_verifier_scores(gate)
         if entry["id"] == "required_state_preservation_complete"),
        None,
    )
    required_state_summary = {
        "status": (state_entry or {}).get("status") or "indeterminate",
        "applicability": (state_entry or {}).get("applicability") or "indeterminate",
        "applicable": bool((state_entry or {}).get("applicable")),
        "score": (state_entry or {}).get("score"),
        "weight": (state_entry or {}).get("weight"),
    }

    details = build_details(
        config, hard_gate,
        evaluation_valid=evaluation_valid, evaluation_success=evaluation_success,
        apply_failed=apply_failed, termination=termination, reward=reward,
        reward_mode=reward_mode, components=components,
        extra={
            "business_reward": business_reward,
            "business_reward_penalty": "cheating" if cheating else None,
            "t_value": t_value,
            "cheating_detected": cheating,
            "f2p_pass_ratio": float(gate.get("f2p_pass_ratio") or 0.0),
            "p2p_pass_ratio": float(gate.get("p2p_pass_ratio") or 0.0),
            # Verifier ``migration_scope_complete`` pass ratio (passed units /
            # total units), surfaced verbatim so the report is auditable
            # without recomputing it from components.test.items.
            "migration_scope_complete_ratio": float(gate.get("scope_coverage") or 0.0),
            # The conditional component is surfaced verbatim, including the
            # explicit ``na`` / ``indeterminate`` distinction, so a reader never
            # has to infer "not applicable" from a missing entry.
            "required_state_preservation_complete": required_state_summary,
            "hard_gate": {
                "success": hard_gate_success,
                "status": gate.get("hard_gate_status"),
                "cheating_detected": cheating,
                "failed_atoms": (hard_gate or {}).get("failed_atoms", []),
                "not_run_atoms": (hard_gate or {}).get("not_run_atoms", []),
            },
            "gate": gate,
            "failed_nodes": gate.get("failed_nodes", []),
            "failed_node_count": gate.get("failed_node_count", 0),
            "errors": gate.get("errors", []),
        },
        bindings=collect_bindings(config, hard_gate, args.model_patch),
    )
    write_json(args.output_details, details)

    if not evaluation_valid:
        return 2

    # VERIFIER_ONLY reward.json carries the deterministic scores. ``reward`` is
    # the four-branch value (may be -1 on the anti-cheating branch, the migration
    # spec allows (-inf, 1]); ``test`` is the [0, 1] progress score, i.e. the
    # continuous ``T_weight * T`` for a partially-complete migration and 0 when
    # the run was penalised or never left the hard gate.
    public_test = max(0.0, min(1.0, float(reward)))
    write_json(args.output_reward, {"reward": float(reward), "test": float(public_test)})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration reward aggregation")
    sub = parser.add_subparsers(required=True)

    gate = sub.add_parser("gate")
    gate.add_argument("--config", required=True)
    gate.add_argument("--ctrf", required=True)
    gate.add_argument("--hard-gate", required=True)
    gate.add_argument("--verifier-result", required=True)
    gate.add_argument("--node-report", required=True)
    gate.add_argument("--output", required=True)
    gate.set_defaults(handler=compute_gate)

    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("--config", required=True)
    finalize_parser.add_argument("--gate", required=True)
    finalize_parser.add_argument("--hard-gate", default=None)
    finalize_parser.add_argument("--output-reward", required=True)
    finalize_parser.add_argument("--output-details", required=True)
    finalize_parser.add_argument("--model-patch", default=None,
                                 help="candidate patch to bind into reward-details (VER-034)")
    finalize_parser.set_defaults(handler=finalize)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
