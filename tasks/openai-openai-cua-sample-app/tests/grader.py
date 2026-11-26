from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Mapping

from verifier.harbor_entry import run_harbor_verifier


def _apply_model_patch(candidate_root: Path, patch_path: Path) -> tuple[bool, str]:
    if patch_path.is_symlink() or not patch_path.is_file():
        return True, "model patch is missing"
    raw = patch_path.read_bytes()
    if not raw.strip():
        return False, "model patch is empty"
    # Candidates are often authored with CRLF while the verifier repository is LF
    # (.gitattributes: * text=auto eol=lf); normalize line endings and ignore
    # whitespace so an otherwise-identical patch still applies.
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    patch = patch_path
    if normalized != raw:
        patch = patch_path.with_name(patch_path.name + ".lf")
        patch.write_bytes(normalized)
    flags = ["--binary", "--ignore-whitespace"]
    check = subprocess.run(
        ["git", "-C", str(candidate_root), "apply", "--check", *flags, str(patch)],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if check.returncode != 0:
        return True, (check.stderr or check.stdout or "model patch is not applicable").strip()[-2000:]
    applied = subprocess.run(
        ["git", "-C", str(candidate_root), "apply", *flags, str(patch)],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if applied.returncode != 0:
        return True, (applied.stderr or applied.stdout or "model patch failed").strip()[-2000:]
    return False, "model patch applied"


def _write_ctrf(output_root: Path, *, task_id: str, base_commit: str, verifier_result: Mapping[str, object]) -> None:
    rows = []
    observations = verifier_result.get("observations", [])
    if isinstance(observations, list):
        for index, row in enumerate(observations):
            if not isinstance(row, Mapping):
                continue
            status = str(row.get("status", "INCONCLUSIVE"))
            ctrf_status = "passed" if status == "PASS" else "failed" if status == "FAIL" else "other"
            kind = str(row.get("kind", "UNKNOWN"))
            group = "p2p" if kind == "P2P" else "f2p" if kind == "F2P" else "diagnostic"
            identifier = str(row.get("id", f"observation-{index + 1}"))
            rows.append({
                "testId": identifier,
                "name": identifier,
                "status": ctrf_status,
                "duration": 0,
                "message": str(row.get("reason", "")),
                "extra": {"deepswe": {"group": group, "required": group in {"p2p", "f2p"}}, "migration": {"kind": kind, "status": status}},
            })
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
        "reward": {
            "branch": "inconclusive",
            "test_weight": 1.0,
            "T": None,
            "value": 0.0,
            "migration_scope_complete_ratio": None,
            "f2p_pass_ratio": None,
            "p2p_pass_ratio": None,
            "eligible_for_graded": False,
        },
    }


def _write_reward_artifacts(output: Path, result: Mapping[str, object]) -> None:
    reward = result.get("reward")
    reward = reward if isinstance(reward, Mapping) else {}
    value = float(reward.get("value", 0.0))
    output.mkdir(parents=True, exist_ok=True)
    reward_path = output / "reward.json"
    reward_tmp = output / "reward.json.tmp"
    reward_tmp.write_text(
        json.dumps({"reward": value}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reward_tmp.replace(reward_path)
    details_path = output / "reward-details.json"
    details_tmp = output / "reward-details.json.tmp"
    details_tmp.write_text(
        json.dumps(
            {
                "cheating_detected": bool(result.get("cheating")),
                "migration_scope_complete_ratio": reward.get("migration_scope_complete_ratio"),
                "f2p_pass_ratio": reward.get("f2p_pass_ratio"),
                "p2p_pass_ratio": reward.get("p2p_pass_ratio"),
                "reward": value,
                "reward_mode": reward.get("reward_mode", "VERIFIER_ONLY"),
                "scoring_version": reward.get("scoring_version", "migration-graded-reward:v1"),
                "branch": reward.get("branch"),
                "test": reward.get("T"),
                "objectivity": reward.get("O"),
                "rubrics": reward.get("R"),
                "judge": None,
                "weights": {
                    "test": (reward.get("weights") or {}).get("T", 0.0),
                    "objectivity": (reward.get("weights") or {}).get("O", 0.0),
                    "rubrics": (reward.get("weights") or {}).get("R", 0.0),
                },
                "errors": {
                    "infrastructure_error": str(result.get("status")) == "infra_error",
                    "agent_time_out": False,
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    details_tmp.replace(details_path)


def _write_harbor_checks(output: Path, result: Mapping[str, object]) -> None:
    """Emit the standard Harbor check artifacts (``gate-reward.json`` / ``hard-gate.json``).

    Two distinct facts are reported and never conflated:

    * ``verifier_success`` follows the Harbor QA contract exactly -- it is ``1``
      only when the *candidate* passed (``reward.branch == "verifier_success"``,
      i.e. the hard gate passed and progress reached 1.0), and ``0`` otherwise,
      so a candidate that failed its own checks is never presented as a passing
      trial.
    * ``verifier_ran`` records whether the verifier itself completed without an
      infrastructure error.

    ``branch`` is echoed from the reward calculator so consumers never have to
    re-derive it.
    """
    output.mkdir(parents=True, exist_ok=True)
    reward = result.get("reward")
    reward = reward if isinstance(reward, Mapping) else {}
    value = float(reward.get("value", 0.0))
    branch = str(reward.get("branch", ""))
    status = str(result.get("status", ""))
    hardgate = str(result.get("hardgate", ""))
    infrastructure = status == "infra_error" or hardgate == "INFRA_ERROR"
    cheating = bool(result.get("cheating")) or branch == "cheating"
    candidate_passed = branch == "verifier_success"
    if infrastructure:
        termination, failure_class = "infrastructure_error", "infrastructure_error"
    elif cheating:
        termination, failure_class = "cheating_detected", "cheating"
    elif hardgate == "FAIL" or branch == "hard_gate_failed":
        termination, failure_class = "hard_gate_failed", "hard_gate_failed"
    elif branch in {"verifier_failed", "inconclusive"}:
        termination, failure_class = "verifier_failed", branch
    else:
        termination, failure_class = "scored", None
    gate_reward = {
        # ``verifier_success`` = the *candidate* passed (the Harbor run-result QA
        # keys on this field); "the verifier ran" is reported separately below.
        "verifier_success": 1 if candidate_passed else 0,
        "verifier_ran": 0 if infrastructure else 1,
        "branch": branch,
        "failure_class": failure_class,
        "hard_gate_success": 1 if hardgate == "PASS" else 0,
        "reward": value,
        "test_reward": value,
        "termination_reason": termination,
        "cheating_detected": cheating,
        "infrastructure_error": infrastructure,
        "f2p_pass_ratio": reward.get("f2p_pass_ratio"),
        "p2p_pass_ratio": reward.get("p2p_pass_ratio"),
        "migration_scope_complete_ratio": reward.get("migration_scope_complete_ratio"),
    }
    (output / "gate-reward.json").write_text(
        json.dumps(gate_reward, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    detail = result.get("hardgate_detail")
    compounds = (detail or {}).get("compound_gates") if isinstance(detail, Mapping) else {}
    compounds = compounds if isinstance(compounds, Mapping) else {}
    failure = result.get("failure")
    failure = failure if isinstance(failure, Mapping) else {}
    execution_error = result.get("candidate_execution_error")
    # When a candidate prerequisite command fails, the run short-circuits before
    # the compound gates are evaluated and ``compound_gates`` is absent.  Record
    # *why* on every check and label them ``NOT_MEASURED`` (distinct from
    # ``INCONCLUSIVE``) so the gate artifact still carries the real failure
    # instead of silently reporting six unmeasured checks.
    short_circuit = not compounds and (bool(failure) or bool(execution_error))
    evidence: list[object] = []
    if short_circuit:
        evidence.append({
            "candidate_execution_error": execution_error,
            "failure": {
                "kind": failure.get("kind"),
                "command": failure.get("command"),
                "returncode": failure.get("returncode"),
                "timed_out": failure.get("timed_out"),
                "stderr_tail": failure.get("stderr_tail"),
            },
        })
    checks: dict[str, object] = {}
    for name in (
        "candidate_valid",
        "target_build_valid",
        "migration_integrity_preserved",
        "p2p_regression_preserved",
        "test_integrity",
        "evaluation_environment_integrity",
    ):
        if short_circuit:
            checks[name] = {
                "pass": False,
                "status": "NOT_MEASURED",
                "summary": str(execution_error or "hard gate was not reached"),
                "evidence": list(evidence),
            }
            continue
        entry = compounds.get(name) if isinstance(compounds.get(name), Mapping) else {}
        compound_status = str(entry.get("status", "INCONCLUSIVE"))
        checks[name] = {
            "pass": compound_status == "PASS",
            "status": compound_status,
            "summary": compound_status,
            "evidence": [],
        }
    hard_gate = {
        "all_pass": hardgate == "PASS",
        "cheating_detected": cheating,
        "cheating_level": None,
        "cheat_reasons": [],
        "measurement": "not_measured" if short_circuit else "measured",
        "failure": dict(failure) if short_circuit else {},
        "checks": checks,
    }
    (output / "hard-gate.json").write_text(
        json.dumps(hard_gate, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_verifier_result(output: Path, result: Mapping[str, object]) -> Path:
    path = output / "verifier-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _candidate_source(config: Mapping[str, object]) -> Path:
    runtime = config.get("runtime")
    if not isinstance(runtime, Mapping):
        target = config.get("target_environment")
        runtime = target.get("runtime") if isinstance(target, Mapping) else None
    workdir = runtime.get("workdir") if isinstance(runtime, Mapping) else None
    for raw in (workdir, "/workspace/repo", "/app"):
        if not isinstance(raw, str) or not raw.startswith("/"):
            continue
        path = Path(raw)
        if path.is_dir() and not path.is_symlink() and any(path.iterdir()):
            return path
    raise RuntimeError("Harbor candidate repository was not found")


def main() -> int:
    bundle = Path("/tests")
    output = Path("/logs/verifier")
    config = json.loads((bundle / "config.json").read_text(encoding="utf-8"))
    harbor = config.get("harbor", config)
    try:
        candidate = Path("/tmp/harbor-candidate")
        if candidate.exists():
            shutil.rmtree(candidate)
        shutil.copytree(_candidate_source(config), candidate, symlinks=True)
        patch_failed, patch_message = _apply_model_patch(candidate, Path("/logs/artifacts/model.patch"))
        result = run_harbor_verifier(
            candidate_root=candidate,
            bundle_root=bundle,
            logs_root=output,
            apply_failed=patch_failed,
            patch_message=patch_message,
        )
    except Exception as exc:
        result = _infrastructure_result(str(exc))
        _write_verifier_result(output, result)
    try:
        _write_reward_artifacts(
            output,
            result,
        )
        _write_harbor_checks(output, result)
        _write_ctrf(
            output,
            task_id=str(harbor.get("task_id", "unknown")),
            base_commit=str(config.get("base_commit", "")),
            verifier_result=result,
        )
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
