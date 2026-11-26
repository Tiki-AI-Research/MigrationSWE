from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Mapping


# §9.1 requires the ``reward-details.json`` ``errors`` object to always carry the
# four classification keys.  ``build error``/``compile error`` are derived from
# the verifier result (candidate build commands and their diagnostics); the two
# flags stay present-and-false when the candidate never reached a failing build.
_BUILD_RECEIPT_KINDS = frozenset({"target-build", "target-native-test", "environment-build"})
_COMPILE_ERROR_MARKERS = ("error[", "could not compile", "error:")


def _reward_error_flags(result: Mapping[str, object]) -> dict[str, bool]:
    """Derive the §9.1 ``errors`` classification from a verifier result."""

    build_error = False
    blobs: list[str] = []
    failure = result.get("failure")
    failure = failure if isinstance(failure, Mapping) else {}
    receipts = result.get("receipts")
    commands = receipts.get("commands") if isinstance(receipts, Mapping) else None
    commands = commands if isinstance(commands, list) else []
    for row in [failure, *[item for item in commands if isinstance(item, Mapping)]]:
        kind = str(row.get("kind") or "")
        returncode = row.get("returncode")
        if (
            kind in _BUILD_RECEIPT_KINDS
            and isinstance(returncode, int)
            and not isinstance(returncode, bool)
            and returncode != 0
        ):
            build_error = True
            blobs.append(str(row.get("stdout") or ""))
            blobs.append(str(row.get("stderr") or ""))
    detail = result.get("hardgate_detail")
    detail = detail if isinstance(detail, Mapping) else {}
    gates = detail.get("compound_gates")
    gates = gates if isinstance(gates, Mapping) else {}
    target_gate = gates.get("target_build_valid")
    if isinstance(target_gate, Mapping) and str(target_gate.get("status")) == "FAIL":
        build_error = True
    blobs.append(str(failure.get("stdout_tail") or ""))
    blobs.append(str(failure.get("stderr_tail") or ""))
    blob = "\n".join(blobs).casefold()
    compile_error = bool(
        build_error and any(marker in blob for marker in _COMPILE_ERROR_MARKERS)
    )
    return {
        "infrastructure_error": str(result.get("status")) == "infra_error",
        "agent_time_out": False,
        "build error": build_error,
        "compile error": compile_error,
    }

from verifier.harbor_entry import run_harbor_verifier


def _apply_model_patch(candidate_root: Path, patch_path: Path) -> tuple[bool, str]:
    if patch_path.is_symlink() or not patch_path.is_file():
        return True, "model patch is missing"
    if patch_path.stat().st_size == 0:
        return False, "model patch is empty"
    # Ignore context whitespace so patches authored with CRLF can apply to the
    # LF checkout. Preserve the patch bytes: CR/CRLF inside added fixture lines
    # is file content, and rewriting it can corrupt hunk counts or binary data.
    # git apply buffers its entire input. Large, valid repository diffs can
    # include development corpora/build artifacts, so do not feed the whole
    # archive to Git or keep multiple full-size Python copies in memory.
    # File deltas are independent Git patch units, including binary patches.
    # Apply every delta, in order, to the disposable candidate checkout; any
    # failed unit still rejects the candidate before any build/test is run.
    flags = ["--binary", "--ignore-whitespace"]
    with tempfile.TemporaryDirectory(prefix="harbor-patch-", dir="/tmp") as directory:
        part = Path(directory) / "part.patch"

        def apply_part() -> tuple[bool, str]:
            for checking in (True, False):
                result = subprocess.run(
                    ["git", "-C", str(candidate_root), "apply",
                     *(["--check"] if checking else []), *flags, str(part)],
                    text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
                )
                if result.returncode != 0:
                    message = (result.stderr or result.stdout or "model patch is not applicable").strip()[-2000:]
                    # An evaluator I/O failure is not a malformed candidate.
                    if "git apply: failed to read:" in message:
                        raise OSError(message)
                    return True, message
            return False, ""

        seen_header = False
        nonempty = False
        with patch_path.open("rb") as source, part.open("wb") as output:
            for line in source:
                if line.startswith(b"diff --git "):
                    if seen_header:
                        output.flush()
                        failed, message = apply_part()
                        if failed:
                            return failed, message
                        output.seek(0)
                        output.truncate()
                    seen_header = True
                nonempty = nonempty or bool(line.strip())
                output.write(line)
            output.flush()
            if not nonempty:
                return False, "model patch is empty"
            failed, message = apply_part()
            if failed:
                return failed, message
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
                "errors": _reward_error_flags(result),
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
    try:
        source_repo = _candidate_source(config)
        patch_path = Path("/logs/artifacts/model.patch")
        if patch_path.is_file() and patch_path.stat().st_size:
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
