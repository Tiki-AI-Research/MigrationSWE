from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Mapping

from verifier.harbor_entry import run_harbor_verifier


def _run_security_selfcheck(bundle: Path) -> str | None:
    """Run the verifier anti-cheating/regression self-check before scoring.

    The self-check is the executable contract for the integrity detectors
    (test-tamper audit, Django-version-fallback detector, residue rules, ...).
    Running it here -- as part of the graded entry point rather than merely
    shipping it -- turns those controls into a real gate: a detector regression
    fails the run closed instead of silently shipping.

    Returns an error string when the self-check cannot run or fails, else
    ``None``.
    """

    script = bundle / "verifier" / "selfcheck_integrity.py"
    if script.is_symlink() or not script.is_file():
        return "verifier security self-check script is missing"
    try:
        completed = subprocess.run(
            [sys.executable or "python3", str(script)],
            capture_output=True,
            text=True,
            cwd=str(bundle),
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"verifier security self-check could not run: {exc}"
    if completed.returncode != 0:
        tail = (completed.stdout or "") + (completed.stderr or "")
        return "verifier security self-check failed: " + tail.strip()[-2000:]
    return None


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


_COMPILE_ERROR_MARKERS = (
    "syntaxerror",
    "invalid syntax",
    "compile error",
    "compilation error",
    "compilation failed",
    "error compiling",
)


def _error_flags(result: Mapping[str, object]) -> dict[str, bool]:
    """Classify the error keys every ``reward-details.json`` must expose.

    The keys are part of the frozen artifact contract; their values are derived
    best-effort so downstream consumers can distinguish an infrastructure
    failure, an agent timeout, a build failure and a compile failure instead of
    receiving two permanently-false buckets.
    """

    status = str(result.get("status", ""))
    infrastructure_error = status == "infra_error"
    hardgate = result.get("hardgate_detail")
    hardgate = hardgate if isinstance(hardgate, Mapping) else {}
    compound = hardgate.get("compound_gates")
    compound = compound if isinstance(compound, Mapping) else {}
    build_gate = compound.get("target_build_valid")
    build_gate = build_gate if isinstance(build_gate, Mapping) else {}
    summary = hardgate.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    failed_atoms = summary.get("failed_atoms")
    failed_atoms = failed_atoms if isinstance(failed_atoms, (list, tuple)) else ()
    failure = result.get("failure")
    failure = failure if isinstance(failure, Mapping) else {}
    failure_kind = str(failure.get("kind") or "")
    tail = " ".join(
        str(failure.get(key) or "") for key in ("stdout_tail", "stderr_tail")
    ).casefold()
    build_error = (
        build_gate.get("status") == "FAIL"
        or "build_pass" in failed_atoms
        or failure_kind in {"target-build", "build"}
    )
    compile_error = bool(build_error) and any(
        marker in tail for marker in _COMPILE_ERROR_MARKERS
    )
    return {
        "infrastructure_error": infrastructure_error,
        "agent_time_out": False,
        "build error": bool(build_error),
        "compile error": bool(compile_error),
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
                    **_error_flags(result),
                },
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
    selfcheck_error = _run_security_selfcheck(bundle)
    if selfcheck_error is not None:
        result = _infrastructure_result(selfcheck_error)
        try:
            _write_verifier_result(output, result)
            _write_reward_artifacts(output, result)
            _write_ctrf(
                output,
                task_id=str(harbor.get("task_id", "unknown")),
                base_commit=str(config.get("base_commit", "")),
                verifier_result=result,
            )
        except Exception:
            return 1
        return 0
    try:
        source_repo = _candidate_source(config)
        patch_path = Path("/logs/artifacts/model.patch")
        if patch_path.is_file() and patch_path.read_bytes().strip():
            candidate = Path(tempfile.mkdtemp(prefix="harbor-candidate-", dir="/tmp"))
            shutil.copytree(source_repo, candidate, dirs_exist_ok=True, symlinks=True)
        else:
            candidate = source_repo
        patch_failed, patch_message = _apply_model_patch(candidate, patch_path)
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
