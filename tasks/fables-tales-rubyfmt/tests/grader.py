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
        },
    }


def _is_infrastructure(result: Mapping[str, object]) -> bool:
    return (
        str(result.get("status", "")) == "infra_error"
        or str(result.get("hardgate", "")) == "INFRA_ERROR"
    )


def _ratio(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


def _write_reward_artifacts(output: Path, result: Mapping[str, object]) -> None:
    """Write the verifier-only reward contract consumed by Harbor.

    A startup security-self-check failure is an infrastructure outcome: the run
    is unmeasurable, so ``reward.json`` is an *empty* map (Harbor records no
    candidate reward) and the honest ``null``/error distinction stays in
    ``reward-details.json``.  Only a genuinely measured candidate emits the four
    verifier-only keys, with unmeasured ratios carried as ``0.0`` because Harbor
    rejects a reward map containing non-numeric values.
    """

    reward = result.get("reward")
    reward = reward if isinstance(reward, Mapping) else {}
    value = float(reward.get("value", 0.0))
    infrastructure = _is_infrastructure(result)
    test = _ratio(reward.get("T"))
    scope = _ratio(reward.get("migration_scope_complete_ratio"))
    f2p = _ratio(reward.get("f2p_pass_ratio"))
    p2p = _ratio(reward.get("p2p_pass_ratio"))
    output.mkdir(parents=True, exist_ok=True)
    reward_path = output / "reward.json"
    reward_tmp = output / "reward.json.tmp"
    public_reward: dict[str, object] = {}
    if not infrastructure:
        public_reward = {
            "test": test if test is not None else 0.0,
            "migration_scope_complete_ratio": scope if scope is not None else 0.0,
            "f2p_pass_ratio": f2p if f2p is not None else 0.0,
            "p2p_pass_ratio": p2p if p2p is not None else 0.0,
            "reward": value,
        }
    reward_tmp.write_text(
        json.dumps(public_reward, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reward_tmp.replace(reward_path)
    details_path = output / "reward-details.json"
    details_tmp = output / "reward-details.json.tmp"
    details_tmp.write_text(
        json.dumps(
            {
                "cheating_detected": bool(result.get("cheating")),
                "migration_scope_complete_ratio": scope,
                "f2p_pass_ratio": f2p,
                "p2p_pass_ratio": p2p,
                "reward": None if infrastructure else value,
                "reward_mode": reward.get("reward_mode", "VERIFIER_ONLY"),
                "scoring_version": reward.get("scoring_version", "migration-verifier-reward:v2"),
                "branch": reward.get("branch"),
                "test": test if test is not None else (None if infrastructure else 0.0),
                "status": result.get("status"),
                "hardgate": result.get("hardgate"),
                "weights": {
                    "test": (reward.get("weights") or {}).get("T", 1.0),
                },
                "errors": {
                    "infrastructure_error": infrastructure,
                    "agent_time_out": False,
                    "build error": False,
                    "compile error": False,
                },
                "observation_contract_errors": list(
                    result.get("observation_contract_errors", [])
                    if isinstance(result.get("observation_contract_errors"), list)
                    else []
                ),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    details_tmp.replace(details_path)


def _write_verifier_result(output: Path, result: Mapping[str, object]) -> Path:
    path = output / "verifier-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _exit_code(result: Mapping[str, object]) -> int:
    """SOP exit contract: 0 measured, 1 candidate failure, 2 infrastructure."""

    if _is_infrastructure(result):
        return 2
    if bool(result.get("cheating")) or str(result.get("hardgate", "")) != "PASS":
        return 1
    return 0


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


def _run_startup_selfcheck() -> dict[str, object]:
    """Run the lightweight security self-check, never letting it crash."""

    try:
        from verifier.security_selfcheck import run_selfcheck

        return run_selfcheck()
    except Exception as exc:  # noqa: BLE001 - a broken self-check is infra failure
        return {
            "ok": False,
            "checks": [{"name": "run_selfcheck", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}],
            "summary": "security self-check raised",
        }


def main() -> int:
    bundle = Path("/tests")
    output = Path("/logs/verifier")
    config = json.loads((bundle / "config.json").read_text(encoding="utf-8"))
    harbor = config.get("harbor", config)
    # Fail closed before the candidate repository is copied, patched or scored:
    # if the verifier's own detectors have regressed the run is an infrastructure
    # error and must not produce a candidate reward.
    selfcheck = _run_startup_selfcheck()
    try:
        output.mkdir(parents=True, exist_ok=True)
        (output / "security-selfcheck.json").write_text(
            json.dumps(selfcheck, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    if not selfcheck.get("ok"):
        result = _infrastructure_result(
            "verifier security self-check failed: " + str(selfcheck.get("summary", "unknown failure"))
        )
    else:
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
    try:
        _write_verifier_result(output, result)
    except Exception:
        return 2
    try:
        _write_reward_artifacts(
            output,
            result,
        )
        _write_ctrf(
            output,
            task_id=str(harbor.get("task_id", "unknown")),
            base_commit=str(config.get("base_commit", "")),
            verifier_result=result,
        )
    except Exception:
        return 2
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
