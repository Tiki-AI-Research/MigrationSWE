from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Mapping

from verifier.harbor_entry import run_harbor_verifier
from verifier.security_selftest import run_startup_selftest

# The normative reward-details.json error taxonomy.  The public spec requires all
# four keys on every artifact (``infrastructure_error``, ``agent_time_out``,
# ``build error``, ``compile error``); build_reports.py surfaces the truthy ones.
REWARD_DETAIL_ERROR_KEYS = (
    "infrastructure_error",
    "agent_time_out",
    "build error",
    "compile error",
)

# rustc/cargo compile diagnostics (the build failed *because the code did not
# compile*), as opposed to a toolchain/network failure or a test assertion.
_COMPILE_ERROR_RE = re.compile(
    r"error\[E\d{3,4}\]"
    r"|error: could not compile"
    r"|could not compile `[^`]+`"
    r"|^error: expected ",
    re.MULTILINE,
)


def _error_flags(result: Mapping[str, object]) -> dict[str, bool]:
    """Derive the four normative error flags from a verifier result.

    Only the structure is contractual; the values are evidence-backed booleans.
    ``build error`` marks a failed target build/compile phase and ``compile
    error`` a rustc compile diagnostic.  Deriving them from the execution
    receipts (rather than hardcoding ``False``) keeps the artifact diagnostic
    for build/compile failures while never inventing one for a clean run.
    """

    infra = str(result.get("status")) == "infra_error"
    failure = result.get("failure")
    failure = failure if isinstance(failure, Mapping) else {}
    receipts = result.get("receipts")
    commands = receipts.get("commands") if isinstance(receipts, Mapping) else None

    texts: list[str] = []
    failed_kinds: set[str] = set()
    failure_kind = str(failure.get("kind") or "")
    if failure:
        failed_kinds.add(failure_kind)
        texts.append(str(failure.get("stderr_tail") or ""))
        texts.append(str(failure.get("stdout_tail") or ""))
    if isinstance(commands, list):
        for receipt in commands:
            if not isinstance(receipt, Mapping):
                continue
            returncode = receipt.get("returncode")
            if not isinstance(returncode, int) or isinstance(returncode, bool):
                continue
            texts.append(str(receipt.get("stderr") or ""))
            texts.append(str(receipt.get("stdout") or ""))
            if returncode != 0:
                failed_kinds.add(str(receipt.get("kind") or ""))
    blob = "\n".join(texts)

    compile_error = bool(_COMPILE_ERROR_RE.search(blob))
    build_error = "target-build" in failed_kinds or compile_error
    return {
        "infrastructure_error": infra,
        "agent_time_out": False,
        "build error": build_error,
        "compile error": compile_error,
    }


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
    generated_artifacts = _generated_build_artifact_paths(patch)
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
    if generated_artifacts:
        preview = ", ".join(generated_artifacts[:8])
        if len(generated_artifacts) > 8:
            preview += f", ... ({len(generated_artifacts)} paths)"
        return True, f"model patch contains generated Cargo build artifacts: {preview}"
    return False, "model patch applied"


def _generated_build_artifact_paths(patch_path: Path) -> list[str]:
    """Return Cargo build outputs embedded in a candidate patch.

    The candidate is built before hard-gate evidence is collected, so scanning
    the resulting tree cannot distinguish submitted ``target`` contents from
    verifier-created outputs.  ``git apply --numstat`` gives us the submitted
    path inventory before that ambiguity is introduced.
    """

    listed = subprocess.run(
        ["git", "apply", "--numstat", "-z", "--binary", str(patch_path)],
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        return []

    generated: list[str] = []
    for record in listed.stdout.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            continue
        path = fields[2].decode("utf-8", errors="surrogateescape")
        parts = PurePosixPath(path).parts
        try:
            target_index = parts.index("target")
        except ValueError:
            continue
        tail = parts[target_index + 1 :]
        if not tail:
            continue
        if (
            tail[0] in {"debug", "release", "doc"}
            or tail[0].startswith(".rust")
            or tail[0] == "CACHEDIR.TAG"
        ):
            generated.append(path)
    return sorted(set(generated))


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
    errors = _error_flags(result)
    # Regression guard: the normative artifact must always carry every key of the
    # spec error taxonomy, whatever the failure branch is.
    assert set(errors) == set(REWARD_DETAIL_ERROR_KEYS), (
        f"reward-details errors must carry {REWARD_DETAIL_ERROR_KEYS}, got {sorted(errors)}"
    )
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
                "errors": errors,
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
    # Fail-closed security self-check: re-run a lightweight regression on every
    # scoring start.  If the anti-cheat / runtime-bridge detection has been
    # degraded, the verifier must stop as an infrastructure error *before*
    # running the candidate and before producing any candidate reward -- never
    # silently downgrade to weaker detection and never skip the check.
    try:
        selftest_ok, selftest_message = run_startup_selftest(config)
    except Exception as exc:  # pragma: no cover - defensive
        selftest_ok, selftest_message = False, f"{type(exc).__name__}: {exc}"
    if not selftest_ok:
        result = _infrastructure_result(
            f"verifier security self-check failed: {selftest_message}"
        )
        _write_verifier_result(output, result)
        try:
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
