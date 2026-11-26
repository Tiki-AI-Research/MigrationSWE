from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Mapping

sys.dont_write_bytecode = True

from verifier.component_result import write_verifier_component
from verifier.harbor_entry import run_harbor_verifier
from verifier.reward_policy import reward_decision


def _apply_model_patch(candidate_root: Path, patch_path: Path) -> tuple[bool, str]:
    if patch_path.is_symlink() or not patch_path.is_file():
        return True, "model patch is missing"
    if not patch_path.read_bytes().strip():
        # An empty artifact means the candidate produced no patch. Treat it
        # as an ordinary candidate failure; do not evaluate the untouched
        # baseline as though it were a submitted solution, which can turn
        # expected migration residue into a false anti-cheating result.
        return True, "model patch is empty"
    check = subprocess.run(
        ["git", "-C", str(candidate_root), "apply", "--check", "--binary", str(patch_path)],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if check.returncode != 0:
        return True, (check.stderr or check.stdout or "model patch is not applicable").strip()[-2000:]
    applied = subprocess.run(
        ["git", "-C", str(candidate_root), "apply", "--binary", str(patch_path)],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if applied.returncode != 0:
        return True, (applied.stderr or applied.stdout or "model patch failed").strip()[-2000:]
    return False, "model patch applied"


def _write_ctrf(
    output_root: Path,
    *,
    task_id: str,
    base_commit: str,
    verifier_result: Mapping[str, object],
    config: Mapping[str, object],
) -> None:
    rows = []
    observations = verifier_result.get("observations", [])
    if isinstance(observations, list):
        for index, row in enumerate(observations):
            if not isinstance(row, Mapping):
                continue
            status = str(row.get("status", "INCONCLUSIVE"))
            ctrf_status = "passed" if status == "PASS" else "failed" if status == "FAIL" else "other"
            kind = str(row.get("kind", "UNKNOWN"))
            group = "P2P" if kind == "P2P" else "F2P" if kind == "F2P" else "diagnostic"
            identifier = str(row.get("id", f"observation-{index + 1}"))
            rows.append({
                "testId": identifier,
                "name": identifier,
                "status": ctrf_status,
                "duration": 0,
                "message": str(row.get("reason", "")),
                "extra": {"deepswe": {"group": group, "required": group in {"P2P", "F2P"}}, "migration": {"kind": kind, "status": status}},
            })
    expected: dict[str, str] = {}
    tests = config.get("tests", {})
    if isinstance(tests, Mapping):
        for key, kind in (("p2p", "P2P"), ("f2p", "F2P"), ("differential", "DIFFERENTIAL"), ("state", "STATE")):
            values = tests.get(key, [])
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                        expected[item["id"]] = kind
    identifiers = [row["testId"] for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("CTRF observation set contains duplicate test IDs")
    actual = {row["testId"]: row for row in rows}
    missing = sorted(set(expected) - set(actual))
    if missing and str(verifier_result.get("status")) in {"failed", "inconclusive"}:
        message = str(
            verifier_result.get("candidate_patch_error")
            or verifier_result.get("candidate_execution_error")
            or verifier_result.get("candidate_harness_error")
            or "candidate failed before all deterministic checks ran"
        )
        for identifier in missing:
            kind = expected[identifier]
            group = kind if kind in {"P2P", "F2P"} else "diagnostic"
            rows.append({
                "testId": identifier,
                "name": identifier,
                "status": "failed",
                "duration": 0,
                "message": message,
                "extra": {"deepswe": {"group": group, "required": group in {"P2P", "F2P"}}, "migration": {"kind": kind, "status": "FAIL"}},
            })
        actual = {row["testId"]: row for row in rows}
    if set(actual) != set(expected) or any(
        row["extra"]["migration"]["kind"] != expected[identifier]
        for identifier, row in actual.items()
    ):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ValueError(f"CTRF observation set mismatch: missing={missing}, extra={extra}")
    statuses = ("passed", "failed", "skipped", "pending", "other")
    counts = {status: sum(row["status"] == status for row in rows) for status in statuses}
    payload = {
        "reportFormat": "CTRF",
        "specVersion": "0.1.0",
        "reportId": str(uuid.uuid4()),
        "runId": str(task_id),
        "generatedBy": "migration-grader",
        "results": {
            "tool": {"name": "migration-grader"},
            "summary": {"tests": len(rows), **counts, "start": 0, "stop": 0},
            "tests": rows,
            "extra": {"deepswe": {"taskId": str(task_id), "baseCommit": str(base_commit)}},
        },
    }
    (output_root / "ctrf.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _component_ratio(value: object, *, empty: float | None = None) -> float | None:
    """Return a real pass ratio, or ``None`` when no observation ran.

    A hard-gate short circuit can leave P2P/F2P components with zero total or
    only NA rows.  Reporting that as ``0.0`` falsely says every test failed;
    ``null`` preserves the distinction between "not executed" and "executed and
    failed".
    """

    if not isinstance(value, Mapping):
        return empty
    status = str(value.get("status", "")).upper()
    if status in {"NA", "INCONCLUSIVE", "INFRA_ERROR", "TIMEOUT"}:
        return empty
    passed = value.get("passed")
    total = value.get("total")
    if (
        isinstance(passed, bool)
        or not isinstance(passed, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or passed < 0
        or total <= 0
        or passed > total
    ):
        return empty
    return max(0.0, min(1.0, passed / total))


def _test_reward(value: object) -> float:
    """Read the verifier's aggregate T score without rewarding malformed data."""
    if not isinstance(value, Mapping):
        return 0.0
    score = value.get("T")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(score)))


def _node_status(row: Mapping[str, object] | None) -> str:
    """Project an observed test status without collapsing diagnostics to fail.

    ``passed``/``failed`` remain the machine-actionable result classes.  A
    missing observation and every non-passing diagnostic status keep their
    original meaning so a not-run or infrastructure result is not reported as
    a candidate assertion failure.
    """

    if row is None:
        return "NA"
    raw = str(row.get("status", "INCONCLUSIVE")).upper()
    if raw == "PASS":
        return "passed"
    if raw == "FAIL":
        return "failed"
    if raw == "TIMEOUT":
        return "INFRA_ERROR"
    if raw in {"NA", "INCONCLUSIVE", "INFRA_ERROR"}:
        return raw
    return "INCONCLUSIVE"


def _write_hard_gate(
    output: Path,
    result: Mapping[str, object],
    *,
    hard_gate_passed: bool,
) -> None:
    """Publish the compact hard-gate verdict the reporting workflow reads.

    Only the aggregate status and the per-compound status/applicability are
    carried here; the full evidence payload stays in ``verifier-result.json``
    so this artifact remains small and safe to collect.
    """

    payload: dict[str, object] = {
        "schema_version": "migration-hardgate-result:v2",
        "hard_gate_passed": hard_gate_passed,
        "status": str(result.get("hardgate", "INCONCLUSIVE")).upper(),
        "cheating": bool(result.get("cheating")),
        "cheat_level": str(result.get("hardgate_cheat_level", "L0")),
    }
    compound = result.get("hardgate_compound_gates")
    if isinstance(compound, Mapping):
        payload["compound_gates"] = {
            str(name): {
                "status": str(gate.get("status", "INCONCLUSIVE")),
                "applicable": bool(gate.get("applicable")),
            }
            for name, gate in compound.items()
            if isinstance(gate, Mapping)
        }
    (output / "hard-gate.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_reward(
    output: Path,
    result: Mapping[str, object],
    *,
    config: Mapping[str, object],
) -> None:
    """Write the reward artifact Harbor reads after the verifier exits."""

    output.mkdir(parents=True, exist_ok=True)
    reward_path = output / "reward.json"
    details_path = output / "reward-details.json"
    reward_text_path = output / "reward.txt"
    reward_text_path.unlink(missing_ok=True)

    status = str(result.get("status", "inconclusive"))
    hardgate = str(result.get("hardgate", "INCONCLUSIVE")).upper()
    cheating = bool(result.get("cheating"))
    if (status == "infra_error" or hardgate == "INFRA_ERROR") and not cheating:
        reward_path.write_text('{"reward": 0}\n', encoding="utf-8")
        reward_text_path.write_text("0\n", encoding="utf-8")
        details_path.unlink(missing_ok=True)
        return
    # A confirmed cheat is never downgraded to an infrastructure zero, even if
    # another gate could not run.  Fall through so the -1 reward and its
    # diagnostic details are emitted.

    components = result.get("components")
    components = components if isinstance(components, Mapping) else {}
    regression = components.get("regression_test")
    regression = regression if isinstance(regression, Mapping) else {}
    p2p_component = regression.get("p2p")
    f2p_component = regression.get("f2p")
    p2p_status = str(p2p_component.get("status", "INCONCLUSIVE")).upper() if isinstance(p2p_component, Mapping) else "INCONCLUSIVE"
    p2p = _component_ratio(p2p_component)
    f2p = _component_ratio(f2p_component)
    # Match the verifier's published component score: ``migration_scope_complete``
    # is emitted as ``round(passed/total, 7)``.  Keeping the same precision makes
    # the ``test`` transport value equal the verifier's aggregate T here.
    scope_raw = _component_ratio(components.get("migration_scope_complete"))
    scope_ratio = round(scope_raw, 7) if scope_raw is not None else None
    test_reward = _test_reward(result.get("test_progress"))
    # ``test`` is a transport value, not a second scoring formula.  The
    # verifier publishes the single test reward T; use it directly so the
    # config's one-component test weight and this artifact cannot drift into
    # separate 0.5/0.5 encodings.
    test_score = test_reward
    reward, hard_gate_passed = reward_decision(
        hardgate=hardgate,
        p2p_status=p2p_status,
        cheating=bool(result.get("cheating")),
        test_reward=test_reward,
    )
    eligible = status == "success" and hard_gate_passed and result.get("eligible_for_graded") is True

    observations = result.get("observations", [])
    observations = observations if isinstance(observations, list) else []
    observed = {
        str(row.get("id")): row
        for row in observations
        if isinstance(row, Mapping) and str(row.get("kind")) in {"P2P", "F2P"}
    }
    expected: dict[str, str] = {}
    tests = config.get("tests", {})
    if isinstance(tests, Mapping):
        for key, kind in (("p2p", "P2P"), ("f2p", "F2P")):
            values = tests.get(key, [])
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                        expected[item["id"]] = kind
    nodes = []
    for identifier, kind in sorted(expected.items()):
        row = observed.get(identifier)
        node_status = _node_status(row if isinstance(row, Mapping) else None)
        nodes.append({"testId": identifier, "group": kind, "status": node_status})
    failed_nodes = sorted(node["testId"] for node in nodes if node["status"] == "failed")
    details = {
        "schema_version": "deepswe-reward-details/v1",
        "reward_mode": "VERIFIER_ONLY",
        "status": "passed" if eligible else "candidate_failed",
        "evaluation_valid": True,
        "evaluation_success": eligible,
        "scoring_executed": False,
        "skip_reason": None if eligible else "deterministic_gate_failed",
        "policy": "deterministic_verifier_only",
        "policy_version": "3",
        "reward": reward,
        "cheating_detected": bool(result.get("cheating")),
        "gate": {
            "passed": hard_gate_passed,
            "hardgate": hardgate,
            "p2p_passed": p2p_status in {"PASS", "NA"},
        },
        "components": {
            "hard_gate": {"score": 1.0 if hard_gate_passed else 0.0, "weight": 0.0},
            "p2p": {"score": p2p, "weight": 0.0},
            "f2p": {"score": f2p, "weight": 0.0},
            "test_reward": {"score": test_reward, "weight": 1.0},
        },
        "p2p_pass_ratio": p2p,
        "f2p_pass_ratio": f2p,
        "migration_scope_complete_ratio": scope_ratio,
        "test": test_score,
        "test_reward": test_reward,
        "errors": {},
        "failed_nodes": failed_nodes,
        "nodes": nodes,
    }
    # Harbor rejects a reward JSON containing any non-numeric value, including
    # an honest ``null`` for an unexecuted group.  Keep the nulls in
    # reward-details/component where they are diagnostic, and omit the
    # not-run group from the numeric transport.
    reward_transport: dict[str, float] = {
        "reward": reward,
        "test": test_score,
        "test_reward": test_reward,
    }
    if p2p is not None:
        reward_transport["p2p"] = p2p
    if f2p is not None:
        reward_transport["f2p"] = f2p
    reward_path.write_text(
        json.dumps(reward_transport, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    details_path.write_text(json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_hard_gate(output, result, hard_gate_passed=hard_gate_passed)


def _infrastructure_result(message: str) -> dict[str, object]:
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": "infra_error",
        "hardgate": "INFRA_ERROR",
        "cheating": False,
        "components": {},
        "test_progress": None,
        "eligible_for_graded": False,
        "observation_contract_errors": [message],
        "observations": [],
        "receipts": {"commands": [], "harness_returncode": None},
    }


def _write_verifier_result(output: Path, result: Mapping[str, object]) -> Path:
    path = output / "verifier-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    bundle = Path("/tests")
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    for stale in ("verifier-result.json", "verifier-component.json", "ctrf.json", "reward.json", "reward.txt", "reward-details.json", "hard-gate.json"):
        (output / stale).unlink(missing_ok=True)
    config: dict[str, object] = {}
    harbor: Mapping[str, object] = {}
    try:
        config = json.loads((bundle / "config.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("verifier config must be a JSON object")
        harbor_value = config.get("harbor", config)
        harbor = harbor_value if isinstance(harbor_value, Mapping) else {}
        candidate = Path("/tmp/harbor-candidate")
        if candidate.exists():
            shutil.rmtree(candidate)
        shutil.copytree("/app", candidate)
        patch_failed, patch_message = _apply_model_patch(candidate, Path("/logs/artifacts/model.patch"))
        result = run_harbor_verifier(
            candidate_root=candidate,
            bundle_root=bundle,
            logs_root=output,
            apply_failed=patch_failed,
            patch_message=patch_message,
        )
        verifier_path = output / "verifier-result.json"
    except Exception as exc:
        result = _infrastructure_result(str(exc))
        verifier_path = _write_verifier_result(output, result)
    try:
        _write_reward(output, result, config=config)
        write_verifier_component(
            result,
            verifier_result_path=verifier_path,
            output_path=output / "verifier-component.json",
        )
        if str(result.get("status")) != "infra_error":
            _write_ctrf(
                output,
                task_id=str(harbor.get("task_id", "unknown")),
                base_commit=str(config.get("base_commit", "")),
                verifier_result=result,
                config=config,
            )
        return 1 if str(result.get("status")) == "infra_error" else 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
