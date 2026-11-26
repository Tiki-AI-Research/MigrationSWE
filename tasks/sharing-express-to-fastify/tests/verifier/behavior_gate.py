"""Behavior-regression hard-gate stage for sharing-express-to-fastify.

Reuses the verifier's hidden-patch harness (root-only evaluator workspace,
per-node isolated candidate copies, canonical node reports, baseline evidence)
to run the configured P2P nodes and the differential behavior oracle and
verdict the ``behavior_regression`` gate entry.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# hardgate.py already puts this directory on sys.path; the CLI path needs the
# same guarantee for the sibling harness imports below.
_VERIFIER_DIR = Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

from sandbox import cleanup_candidate_workspace, prepare_candidate_workspace  # noqa: E402
from verifier import (  # noqa: E402
    _canonical_controller_node,
    _load_baseline_evidence,
    _prepare_test_workspace,
    _sha256,
    node_status_counts,
    run_node,
)

GATE_ID = "behavior_regression"
NODE_TIMEOUT_SECONDS = 180
DEFAULT_EVALUATOR_ROOT = Path("/var/lib/harbor-evaluator")
DIFFERENTIAL_SCRIPT_NAME = "behavior-oracle.js"
DIFFERENTIAL_ENV = {"SHARING_BASE_DIR": "/opt/sharing-base"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class BehaviorPlan:
    """Resolved behavior node ids and harness locations."""

    p2p: tuple[str, ...]
    differential: tuple[str, ...]
    config_path: Path
    evaluator_root: Path


def _empty_group(total: int = 0) -> dict[str, Any]:
    return {
        "results": [],
        "passed": 0,
        "total": total,
        "pass_rate": None,
        "status_counts": node_status_counts([]),
    }


def _empty_groups(p2p_total: int = 0, differential_total: int = 0) -> dict[str, Any]:
    return {"p2p": _empty_group(p2p_total), "differential": _empty_group(differential_total)}


def _group_result(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(1 for node in nodes if node["pass"])
    total = len(nodes)
    return {
        "results": nodes,
        "passed": passed,
        "total": total,
        "pass_rate": passed / total if total else None,
        "status_counts": node_status_counts(nodes),
    }


def _gate(status: str, groups: dict[str, Any], summary: str) -> dict[str, Any]:
    return {
        "schema_version": "harbor-migration-check/v1",
        "id": GATE_ID,
        "applicable": True,
        "success": status == "passed",
        "status": status,
        "summary": summary,
        "groups": groups,
        "checks": [],
    }


def _read_behavior_nodes(config: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    behavior = config.get("hard_gate_behavior")
    if not isinstance(behavior, dict):
        raise ValueError("config.json hard_gate_behavior must be an object")
    groups: dict[str, tuple[str, ...]] = {}
    for key, prefix in (("p2p", "p2p::"), ("differential", "differential::")):
        value = behavior.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"config.json hard_gate_behavior.{key} must be a string array")
        if any(not item.startswith(prefix) for item in value):
            raise ValueError(f"hard_gate_behavior.{key} node ids must start with {prefix}")
        groups[key] = tuple(value)
    configured = [*groups["p2p"], *groups["differential"]]
    if len(configured) != len(set(configured)):
        raise ValueError("hard_gate_behavior contains duplicate execution nodes")
    return groups["p2p"], groups["differential"]


def _resolve_test_patch(config: dict[str, Any], tests_root: Path) -> Path:
    configured = Path(str(config.get("test_patch", "")))
    test_patch = configured if configured.is_file() else tests_root / "verifier" / "test.patch"
    if not test_patch.is_file():
        raise FileNotFoundError(f"evaluator test patch is missing: {test_patch}")
    expected_sha = str(config.get("test_patch_sha256", ""))
    actual_sha = _sha256(test_patch)
    if not _SHA256_RE.fullmatch(expected_sha) or actual_sha != expected_sha:
        raise ValueError("tests/verifier/test.patch SHA-256 mismatch")
    return test_patch


def _node_specs(workspace: Path, node_ids: list[str]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for node_id in node_ids:
        group, file_stem = node_id.split("::", 1)
        script_group = "differential" if group == "differential" else group
        script_name = DIFFERENTIAL_SCRIPT_NAME if group == "differential" else f"{file_stem}.js"
        specs.append({
            "node_id": node_id,
            "group": group,
            "script": workspace / "suite" / script_group / script_name,
            "script_display": f"suite/{script_group}/{script_name}",
            "env": dict(DIFFERENTIAL_ENV) if group == "differential" else {},
        })
    return specs


def _run_behavior_regression(repo: Path, config: dict[str, Any], plan: BehaviorPlan) -> dict[str, Any]:
    tests_root = plan.config_path.resolve().parent
    test_patch = _resolve_test_patch(config, tests_root)
    node_ids = [*plan.p2p, *plan.differential]
    baseline_evidence = _load_baseline_evidence(config, tests_root, node_ids)
    stale_baseline = any(item.get("provenance") != "current" for item in baseline_evidence.values())
    if stale_baseline:
        gate = _gate(
            "infrastructure_error",
            _empty_groups(len(plan.p2p), len(plan.differential)),
            "behavior regression cannot be evaluated: historical baseline evidence is stale",
        )
        gate["baseline_evidence_status"] = "stale"
        return gate

    plan.evaluator_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    plan.evaluator_root.chmod(0o700)
    workspace = Path(tempfile.mkdtemp(prefix="behavior-regression-", dir=plan.evaluator_root))
    workspace.chmod(0o700)
    nodes: list[dict[str, Any]] = []
    try:
        prepared = _prepare_test_workspace(test_patch, workspace)
        if prepared.get("success") is not True:
            raise RuntimeError(str(prepared.get("error", "unable to prepare evaluator tests")))
        specs = _node_specs(workspace, node_ids)
        required = [
            ("suite/_harness.js", workspace / "suite" / "_harness.js"),
            *((spec["script_display"], spec["script"]) for spec in specs),
        ]
        missing = [label for label, path in required if not path.is_file()]
        if missing:
            raise RuntimeError(f"test.patch does not contain configured behavior suite files: {missing}")
        for spec in specs:
            candidate_scratch: Path | None = None
            try:
                candidate_scratch, candidate_workspace = prepare_candidate_workspace(repo)
                result = run_node(
                    workspace,
                    spec["script"],
                    timeout=NODE_TIMEOUT_SECONDS,
                    env_extra=spec["env"],
                    candidate_repo=candidate_workspace,
                )
            finally:
                if candidate_scratch is not None:
                    cleanup_candidate_workspace(candidate_scratch)
            result["command"] = ["node", spec["script_display"]]
            nodes.append(_canonical_controller_node(
                spec["node_id"],
                spec["group"],
                baseline_evidence[spec["node_id"]],
                result,
            ))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    groups = {
        "p2p": _group_result([node for node in nodes if node["group"] == "p2p"]),
        "differential": _group_result([node for node in nodes if node["group"] == "differential"]),
    }
    failing = [node["node_id"] for node in nodes if not node["pass"]]
    if any(node["candidate_status"] == "infrastructure_error" for node in nodes):
        status = "infrastructure_error"
    elif failing:
        status = "candidate_failure"
    else:
        status = "passed"
    if status == "passed":
        summary = (
            "behavior regression passed: "
            f"p2p {groups['p2p']['passed']}/{groups['p2p']['total']}, "
            f"differential {groups['differential']['passed']}/{groups['differential']['total']}"
        )
    else:
        shown = ", ".join(failing[:8]) + (", ..." if len(failing) > 8 else "")
        summary = f"behavior regression {status}: {len(failing)}/{len(nodes)} node(s) not passed: {shown}"
    gate = _gate(status, groups, summary)
    gate["baseline_evidence_status"] = "current"
    return gate


def evaluate_behavior_regression(
    repo: Path,
    config: dict[str, Any],
    *,
    config_path: Path | None = None,
    evaluator_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate the behavior_regression stage; returns a gate entry, never raises."""
    try:
        p2p_ids, differential_ids = _read_behavior_nodes(config)
    except (KeyError, TypeError, ValueError) as exc:
        return _gate("infrastructure_error", _empty_groups(), f"behavior regression configuration error: {exc}")
    if not p2p_ids and not differential_ids:
        return _gate("passed", _empty_groups(), "behavior regression passed: no behavior nodes configured")
    plan = BehaviorPlan(
        p2p=p2p_ids,
        differential=differential_ids,
        config_path=Path(config_path) if config_path is not None else Path("."),
        evaluator_root=Path(evaluator_root) if evaluator_root is not None else DEFAULT_EVALUATOR_ROOT,
    )
    try:
        return _run_behavior_regression(Path(repo), config, plan)
    except ValueError as exc:
        return _gate(
            "configuration_error",
            _empty_groups(len(p2p_ids), len(differential_ids)),
            f"behavior regression configuration error: {exc}",
        )
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK - the hard gate must survive any stage failure
        return _gate(
            "infrastructure_error",
            _empty_groups(len(p2p_ids), len(differential_ids)),
            f"behavior regression infrastructure failure: {type(exc).__name__}: {exc}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Behavior regression hard-gate stage")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        report = _gate("infrastructure_error", _empty_groups(), f"unable to read config.json: {exc}")
    else:
        report = evaluate_behavior_regression(args.repo, config, config_path=args.config)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
