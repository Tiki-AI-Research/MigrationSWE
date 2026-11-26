from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Mapping

from verifier.component_result import write_verifier_component
from verifier.harbor_entry import InfrastructureError, run_harbor_verifier


def _apply_model_patch(candidate_root: Path, patch_path: Path) -> tuple[bool, str]:
    if patch_path.is_symlink() or not patch_path.is_file():
        return True, "model patch is missing"
    if not patch_path.read_bytes().strip():
        return False, "model patch is empty"
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
    status = str(verifier_result.get("status", "inconclusive"))
    hardgate = str(verifier_result.get("hardgate", "INCONCLUSIVE"))
    contract_errors = verifier_result.get("observation_contract_errors", [])
    if not isinstance(contract_errors, list):
        contract_errors = [str(contract_errors)]
    diagnostic = verifier_result.get("diagnostic")
    diagnostic_summary = None
    if isinstance(diagnostic, Mapping):
        diagnostic_summary = {
            key: diagnostic.get(key)
            for key in (
                "status",
                "diagnostic_only",
                "eligible_for_graded",
                "f2p_status",
                "p2p_status",
                "summary",
                "message",
            )
            if key in diagnostic
        }
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
            "extra": {
                "deepswe": {"taskId": str(task_id), "baseCommit": str(base_commit)},
                "migration": {
                    "status": status,
                    "hardgate": hardgate,
                    "cheating": bool(verifier_result.get("cheating")),
                    "cheat_level": str(verifier_result.get("cheat_level", "L0")),
                    "reward_override": verifier_result.get("reward_override"),
                    "infrastructure_error": status == "infra_error"
                    or hardgate == "INFRA_ERROR",
                    "f2p_status": verifier_result.get("f2p_status"),
                    "p2p_status": verifier_result.get("p2p_status"),
                    "not_run_reason": verifier_result.get("not_run_reason"),
                    "diagnostic_only": bool(
                        verifier_result.get("diagnostic_only")
                    ),
                    "diagnostic": diagnostic_summary,
                    "candidate_execution_error": verifier_result.get(
                        "candidate_execution_error"
                    ),
                    "observation_contract_errors": [
                        str(item) for item in contract_errors
                    ],
                },
            },
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
    }


def _write_verifier_result(output: Path, result: Mapping[str, object]) -> Path:
    path = output / "verifier-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _component(result: Mapping[str, object], name: str) -> Mapping[str, object]:
    components = result.get("components")
    if not isinstance(components, Mapping):
        return {}
    value = components.get(name)
    return value if isinstance(value, Mapping) else {}


def _ratio(component: Mapping[str, object]) -> float:
    passed = _number(component.get("passed"))
    total = _number(component.get("total"))
    if passed is None or total is None or total <= 0:
        return 0.0
    return max(0.0, min(1.0, passed / total))


def _f2p_component(result: Mapping[str, object]) -> Mapping[str, object]:
    regression = _component(result, "regression_test")
    value = regression.get("f2p") if isinstance(regression, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _is_infrastructure(result: Mapping[str, object]) -> bool:
    return (
        str(result.get("status", "")) == "infra_error"
        or str(result.get("hardgate", "")) == "INFRA_ERROR"
    )


def _cheating(result: Mapping[str, object]) -> bool:
    return bool(result.get("cheating")) or str(result.get("cheat_level", "L0")) in {"L1", "L2"}


def _score(result: Mapping[str, object]) -> dict[str, object]:
    cheating = _cheating(result)
    infrastructure = _is_infrastructure(result)
    hardgate = str(result.get("hardgate", ""))
    f2p_ratio = _ratio(_f2p_component(result))
    scope_ratio = _ratio(_component(result, "migration_scope_complete"))
    p2p_ratio = _ratio(_component(result, "p2p_regression_preserved"))
    progress = result.get("test_progress")
    test = _number(progress.get("T")) if isinstance(progress, Mapping) else None
    if test is None:
        test = 0.0
    test = max(0.0, min(1.0, test))
    override = result.get("reward_override")
    if cheating:
        reward = -1.0
        branch = "cheating_detected"
    elif infrastructure:
        reward = 0.0
        branch = "infrastructure_error"
    elif hardgate != "PASS":
        reward = 0.0
        branch = "hard_gate_failed"
    elif isinstance(override, (int, float)) and not isinstance(override, bool):
        reward = float(override)
        branch = "verifier_override"
    else:
        reward = test
        branch = "verifier_success" if str(result.get("status")) == "success" else "verifier_failed"
    reward = max(-1.0, min(1.0, reward))
    return {
        "test": test,
        "migration_scope_complete_ratio": scope_ratio,
        "f2p_pass_ratio": f2p_ratio,
        "p2p_pass_ratio": p2p_ratio,
        "reward": reward,
        "cheating_detected": cheating,
        "infrastructure_error": infrastructure,
        "branch": branch,
    }


def _write_reward(output: Path, result: Mapping[str, object]) -> None:
    """Write the verifier-only reward contract consumed by Harbor.

    Reward branches follow the migration verifier spec: anti-cheating L1/L2
    receives -1, ordinary hard-gate failures receive 0, and a passing hard gate
    receives ``T`` (``weights["test"] == 1`` for verifier-only tasks).
    """
    score = _score(result)
    payload = {
        "test": score["test"],
        "migration_scope_complete_ratio": score["migration_scope_complete_ratio"],
        "f2p_pass_ratio": score["f2p_pass_ratio"],
        "reward": score["reward"],
    }
    (output / "reward.json").write_text(
        json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _build_error_flags(result: Mapping[str, object]) -> dict[str, bool]:
    infrastructure = bool(_is_infrastructure(result))
    build_failed = False
    compile_failed = False
    test_failed = False
    dependency_failed = False
    receipts = result.get("receipts")
    commands = receipts.get("commands") if isinstance(receipts, Mapping) else None
    if isinstance(commands, list):
        for receipt in commands:
            if not isinstance(receipt, Mapping):
                continue
            # Only evaluator-owned launch faults are infrastructure.  Candidate
            # command timeouts are scored as candidate failures by the verifier
            # and must not silently turn a trial into an infrastructure error.
            if receipt.get("infrastructure_error") is True:
                infrastructure = True
            kind = str(receipt.get("kind", ""))
            returncode = receipt.get("returncode")
            failed = isinstance(returncode, int) and not isinstance(returncode, bool) and returncode != 0
            if failed and kind in {"target-build", "build"}:
                build_failed = True
                compile_failed = True
            if (failed or receipt.get("timed_out") is True) and kind in {"target-native-test", "test-execution"}:
                test_failed = True
            if (failed or receipt.get("timed_out") is True) and kind in {"dependency-resolution", "dependency-graph", "import-probe", "version-probe"}:
                dependency_failed = True
    # target_build_valid also includes native test and dependency receipts.
    # Its aggregate FAIL cannot identify a compiler or build failure. Keep
    # scoring unchanged and classify errors by their actual command phase.
    return {
        "infrastructure_error": infrastructure,
        "agent_time_out": False,
        "build error": build_failed,
        "compile error": compile_failed,
        "test error": test_failed,
        "dependency error": dependency_failed,
    }


def _write_reward_details(output: Path, result: Mapping[str, object]) -> None:
    score = _score(result)
    errors = _build_error_flags(result)
    if errors["infrastructure_error"]:
        score = {
            **score,
            "test": 0.0,
            "migration_scope_complete_ratio": 0.0,
            "f2p_pass_ratio": 0.0,
            "p2p_pass_ratio": 0.0,
            "reward": 0.0,
        }
    payload = {
        "schema_version": "deepswe-reward-details/v2",
        "reward_mode": "VERIFIER_ONLY",
        "status": score["branch"],
        "cheating_detected": bool(score["cheating_detected"]),
        "test": score["test"],
        "migration_scope_complete_ratio": score["migration_scope_complete_ratio"],
        "f2p_pass_ratio": score["f2p_pass_ratio"],
        "p2p_pass_ratio": score["p2p_pass_ratio"],
        "reward": score["reward"],
        "errors": errors,
        "verifier_status": str(result.get("status", "inconclusive")),
        "hardgate": str(result.get("hardgate", "INCONCLUSIVE")),
        "cheat_level": str(result.get("cheat_level", "L0")),
        "eligible_for_graded": bool(result.get("eligible_for_graded", False)),
        "f2p_status": result.get("f2p_status"),
        "p2p_status": result.get("p2p_status"),
        "not_run_reason": result.get("not_run_reason"),
        "diagnostic_only": bool(result.get("diagnostic_only", False)),
        "diagnostic": result.get("diagnostic"),
        "candidate_execution_error": result.get("candidate_execution_error"),
        "observation_contract_errors": list(
            result.get("observation_contract_errors", [])
            if isinstance(result.get("observation_contract_errors"), list)
            else []
        ),
        "test_progress": result.get("test_progress"),
        "components": result.get("components", {}),
    }
    (output / "reward-details.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_infrastructure_reports(output: Path, message: str) -> None:
    """Infrastructure outcomes are not measurable: emit an empty reward map."""

    (output / "reward.json").write_text("{}\n", encoding="utf-8")
    payload = {
        "schema_version": "deepswe-reward-details/v2",
        "reward_mode": "VERIFIER_ONLY",
        "status": "infrastructure_error",
        "cheating_detected": False,
        "test": None,
        "migration_scope_complete_ratio": None,
        "f2p_pass_ratio": None,
        "p2p_pass_ratio": None,
        "reward": None,
        "errors": {
            "infrastructure_error": True,
            "agent_time_out": False,
            "build error": False,
            "compile error": False,
            "test error": False,
            "dependency error": False,
        },
        "message": message,
    }
    (output / "reward-details.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _exit_code(result: Mapping[str, object]) -> int:
    if _is_infrastructure(result):
        return 2
    if _cheating(result) or str(result.get("hardgate", "")) != "PASS":
        return 1
    return 0


def _security_selfcheck(bundle: Path) -> str | None:
    """Re-run the evaluator's own safety tests before touching a candidate.

    The Docker build runs the same scripts, but a build-time check cannot detect
    a verifier bundle that was altered or assembled incorrectly before launch.
    A failure here is an evaluator fault: return an infrastructure error rather
    than letting a candidate receive a numeric score under a broken verifier.
    """

    bundle = bundle.resolve()
    # The acceptance suite exercises real candidate runs, including the
    # TypeScript fixture batch.  Budget is per script: the acceptance sweep
    # needs several minutes on a loaded host, while the adversarial sweep is
    # sub-second and should fail fast.
    scripts = (
        (bundle / "verifier" / "acceptance_checks.py", 600),
        (bundle / "verifier" / "adversarial_checks.py", 120),
    )
    for script, timeout in scripts:
        if not script.is_file() or script.is_symlink():
            return f"security self-check is missing: {script.name}"
        completed = subprocess.run(
            [sys.executable, str(script)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout,
            cwd=str(bundle),
        )
        if completed.returncode != 0:
            diagnostic = (completed.stderr or completed.stdout or "").strip()
            return (
                f"security self-check failed: {script.name} exited "
                f"{completed.returncode}: {diagnostic[-2000:]}"
            )
    return None


def main() -> int:
    bundle = Path("/tests")
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    config = json.loads((bundle / "config.json").read_text(encoding="utf-8"))
    harbor = config.get("harbor")
    harbor = harbor if isinstance(harbor, Mapping) else {}
    try:
        selfcheck_error = _security_selfcheck(bundle)
    except Exception as exc:
        selfcheck_error = f"security self-check could not run: {exc}"
    if selfcheck_error is not None:
        _write_infrastructure_reports(output, selfcheck_error)
        return 2
    try:
        candidate = Path("/tmp/harbor-candidate")
        if candidate.exists():
            shutil.rmtree(candidate)
        shutil.copytree("/app", candidate, symlinks=True)
        patch_failed, patch_message = _apply_model_patch(candidate, Path("/logs/artifacts/model.patch"))
        result = run_harbor_verifier(
            candidate_root=candidate,
            bundle_root=bundle,
            logs_root=output,
            apply_failed=patch_failed,
            patch_message=patch_message,
        )
        verifier_path = output / "verifier-result.json"
    except InfrastructureError as exc:
        result = _infrastructure_result(str(exc))
        verifier_path = _write_verifier_result(output, result)
    except Exception as exc:
        # Candidate-evidence failures are classified inside the verifier.  An
        # unexpected exception therefore identifies an evaluator fault.
        result = _infrastructure_result(str(exc))
        verifier_path = _write_verifier_result(output, result)

    try:
        write_verifier_component(
            result,
            verifier_result_path=verifier_path,
            output_path=output / "verifier-component.json",
        )
        _write_ctrf(
            output,
            task_id=str(harbor.get("task_id") or config.get("task_id") or "unknown"),
            base_commit=str(config.get("base_commit", "")),
            verifier_result=result,
        )
    except Exception as exc:
        _write_infrastructure_reports(output, f"cannot materialize verifier reports: {exc}")
        return 2

    if _is_infrastructure(result):
        message = str(result.get("candidate_execution_error") or "verifier infrastructure error")
        errors = result.get("observation_contract_errors")
        if isinstance(errors, list) and errors:
            message = f"{message}: {errors[0]}"
        _write_infrastructure_reports(output, message)
        return 2

    try:
        _write_reward(output, result)
        _write_reward_details(output, result)
    except Exception as exc:
        _write_infrastructure_reports(output, f"cannot materialize reward reports: {exc}")
        return 2
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
