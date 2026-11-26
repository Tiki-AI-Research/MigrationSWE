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
    scope_error = _validate_change_scope(patch)
    if scope_error:
        return True, scope_error
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


def _validate_change_scope(patch: Path) -> str | None:
    """Enforce the instruction's added-Go-files-only boundary on the collected diff.

    The collected patch contains every staged change, including files ignored by
    later runtime scans.  Git parses quoted names and binary diffs for us.
    """
    numstat = subprocess.run(
        ["git", "apply", "--numstat", "-z", str(patch)],
        capture_output=True, check=False,
    )
    summary = subprocess.run(
        ["git", "apply", "--summary", str(patch)],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    if numstat.returncode or summary.returncode:
        return "model patch change inventory could not be parsed"
    paths: list[str] = []
    for record in numstat.stdout.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            return "model patch change inventory is malformed"
        try:
            paths.append(fields[2].decode("utf-8"))
        except UnicodeDecodeError:
            return "model patch contains a non-UTF-8 path"
    created = sum(
        line.startswith((" create mode 100644 ", " create mode 100755 "))
        for line in summary.stdout.splitlines()
    )
    if created != len(paths):
        return "model patch modifies existing files or adds non-regular files"
    for name in paths:
        parts = Path(name).parts
        if not parts or parts[0] in {"data-url", "data_url", "url_debug_tests", ".github"}:
            return f"out-of-scope model change: {name}"
        if name == "go.mod" or name.endswith(".go"):
            continue
        if len(parts) >= 2 and parts[0] == "testdata":
            continue
        if len(parts) >= 3 and parts[0] in {
            "url", "idna", "percentencoding", "formurlencoded",
        } and any(part in {"testdata", "tests", "fixtures"} for part in parts[1:-1]):
            continue
        return f"non-Go model change outside Go test assets: {name}"
    return None


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


def _candidate_error_flags(result: Mapping[str, object]) -> tuple[bool, bool]:
    """Classify candidate build/compile failures from the execution receipts.

    ``reward-details.json.errors`` is the shared transport for the error
    taxonomy (``infrastructure_error`` / ``agent_time_out`` / ``build error`` /
    ``compile error``).  A non-zero ``target-build`` receipt (``go build ./...``)
    is both a build failure and a compilation failure for a compiled-language
    migration, so it lights up both keys; a pristine/absent receipt keeps them
    ``false`` rather than inventing a category.
    """

    receipts = result.get("receipts")
    commands = receipts.get("commands") if isinstance(receipts, Mapping) else None
    failed_kinds: set[str] = set()
    if isinstance(commands, list):
        for row in commands:
            if not isinstance(row, Mapping):
                continue
            returncode = row.get("returncode")
            if isinstance(returncode, int) and not isinstance(returncode, bool) and returncode != 0:
                failed_kinds.add(str(row.get("kind") or ""))
    failed_build = "target-build" in failed_kinds
    return failed_build, failed_build


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
    build_error, compile_error = _candidate_error_flags(result)
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
                "weights": {
                    "test": (reward.get("weights") or {}).get("T", 1.0),
                },
                "errors": {
                    "infrastructure_error": str(result.get("status")) == "infra_error",
                    "agent_time_out": False,
                    "build error": build_error,
                    "compile error": compile_error,
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
