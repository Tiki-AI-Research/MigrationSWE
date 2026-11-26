#!/usr/bin/env python3
"""Collect and validate the candidate delta against the packaged replay tree."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


class CandidatePolicyError(RuntimeError):
    pass


def run(
    repo: Path, *args: str, env: dict[str, str] | None = None, input: bytes | None = None
) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        env=env,
        input=input,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise CandidatePolicyError(f"git {' '.join(args)} failed: {message}")
    return completed.stdout


def candidate_untracked(repo: Path, *, env: dict[str, str] | None = None) -> bytes:
    # Do not use --exclude-standard: candidate-controlled ignore rules must
    # not hide additions. Only platform metadata, dependencies and generated
    # distribution files are outside the source patch, at the repository root.
    return run(
        repo, "ls-files", "--others", "-z",
        "--exclude=/.base_commit", "--exclude=/node_modules/", "--exclude=/build/",
        env=env,
    )


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise CandidatePolicyError(f"unsafe candidate path: {value!r}")
    if value == ".git" or value.startswith(".git/"):
        raise CandidatePolicyError(f"candidate may not modify Git metadata: {value}")
    return value


def parse_raw(raw: bytes) -> list[dict[str, str]]:
    fields = raw.split(b"\0")
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(fields) and fields[index]:
        header = fields[index].decode("ascii")
        index += 1
        path = safe_path(fields[index].decode("utf-8", "strict"))
        index += 1
        parts = header.split()
        if len(parts) != 5 or not parts[4]:
            raise CandidatePolicyError(f"unexpected raw diff record: {header!r}")
        old_mode, new_mode, old_oid, new_oid, status = parts
        old_mode = old_mode.removeprefix(":")
        if status[0] not in {"A", "M", "D"} or len(status) != 1:
            raise CandidatePolicyError(f"rename/copy/unmerged changes are forbidden: {status} {path}")
        operation = {"A": "add", "M": "modify", "D": "delete"}[status]
        entries.append(
            {
                "path": path,
                "operation": operation,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "old_oid": old_oid,
                "new_oid": new_oid,
            }
        )
    return entries


def rejected_manifest(args: argparse.Namespace, message: str) -> None:
    try:
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        replay_base = contract["replay_base"]
        base_commit = ""
        base_file = args.repo / ".base_commit"
        if base_file.is_file():
            base_commit = base_file.read_text(encoding="ascii").strip()
        args.patch.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        if not args.patch.exists():
            args.patch.write_bytes(b"")
        patch = args.patch.read_bytes()
        payload = {
            "schema_version": "candidate-patch-manifest/v1",
            "task_id": contract.get("task_id"),
            "suite_id": contract.get("suite_id"),
            "base": {
                "runtime_commit": base_commit,
                "tree": replay_base["tree"],
                "packaged_commit": replay_base.get("commit"),
            },
            "patch": {
                "path": str(args.patch),
                "sha256": hashlib.sha256(patch).hexdigest(),
                "size": len(patch),
                "empty": not patch,
            },
            "entries": [],
            "policy": {"passed": False, "violations": [message]},
        }
        args.manifest.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def collect(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    expected_tree = contract["replay_base"]["tree"]
    base_file = repo / ".base_commit"
    if not base_file.is_file():
        raise CandidatePolicyError("runtime replay anchor .base_commit is missing")
    base_commit = base_file.read_text(encoding="ascii").strip()
    actual_tree = run(repo, "rev-parse", f"{base_commit}^{{tree}}").decode().strip()
    if actual_tree != expected_tree:
        raise CandidatePolicyError(
            f"runtime replay tree mismatch: expected {expected_tree}, got {actual_tree}"
        )

    args.patch.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="candidate-index-") as tmp:
        index_path = Path(tmp) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index_path)
        run(repo, "read-tree", base_commit, env=env)
        run(repo, "add", "-u", "--", ".", env=env)
        additions = candidate_untracked(repo, env=env)
        if additions:
            # NUL-delimited literal pathspecs preserve spaces, newlines and Git
            # pathspec metacharacters without consulting candidate ignore rules.
            run(
                repo, "--literal-pathspecs", "add", "--force",
                "--pathspec-from-file=-", "--pathspec-file-nul",
                env=env, input=additions,
            )
        # The replay anchor is platform metadata. Drop it explicitly even if
        # an agent staged it; it must never become part of the source patch.
        run(
            repo,
            "rm",
            "--cached",
            "--ignore-unmatch",
            "-q",
            "--",
            ".base_commit",
            env=env,
        )
        rename_scan = run(
            repo,
            "diff",
            "--cached",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies",
            base_commit,
            "--",
            env=env,
        )
        if any(record.startswith((b"R", b"C")) for record in rename_scan.split(b"\0")):
            raise CandidatePolicyError("candidate renames and copies are forbidden")
        raw = run(repo, "diff", "--cached", "--raw", "-z", "--no-renames", base_commit, "--", env=env)
        entries = parse_raw(raw)
        patch = run(
            repo,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-renames",
            base_commit,
            "--",
            env=env,
        )

    allowed: dict[str, list[str]] = contract["allowed_changes"]
    violations: list[str] = []
    for entry in entries:
        path = entry["path"]
        operation = entry["operation"]
        operations = allowed.get(path, [])
        if operation not in operations:
            for pattern, pattern_operations in contract.get("allowed_change_patterns", {}).items():
                if fnmatch.fnmatch(path, pattern):
                    operations = pattern_operations
                    break
        if operation not in operations:
            violations.append(f"{path}: operation {operation} is not allowed")
        expected_modes = {
            "add": ("000000", "100644"),
            "modify": ("100644", "100644"),
            "delete": ("100644", "000000"),
        }[operation]
        # The build driver is a new executable-by-convention Node script. An
        # agent may legitimately create it with the executable bit set before
        # discovering that package scripts invoke it via `node`; the bit is not
        # part of the task contract, so accept either regular-file mode.
        if (
            operation == "add"
            and path in ("scripts/build-vite.mjs", "scripts/watch-vite.mjs")
            and entry["new_mode"] == "100755"
        ):
            expected_modes = ("000000", "100755")
        if (entry["old_mode"], entry["new_mode"]) != expected_modes:
            violations.append(
                f"{path}: forbidden mode/type transition "
                f"{entry['old_mode']}->{entry['new_mode']}"
            )
    if violations:
        raise CandidatePolicyError("; ".join(violations))

    args.patch.write_bytes(patch)
    patch_sha = hashlib.sha256(patch).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": "candidate-patch-manifest/v1",
        "task_id": contract["task_id"],
        "suite_id": contract["suite_id"],
        "base": {
            "runtime_commit": base_commit,
            "tree": actual_tree,
            "packaged_commit": contract["replay_base"]["commit"],
        },
        "collector": {
            "argv": [
                "collect_candidate.py",
                "--repo",
                str(repo),
                "--contract",
                str(args.contract),
            ],
            "diff_flags": ["--binary", "--full-index", "--no-renames"],
        },
        "patch": {
            "path": str(args.patch),
            "sha256": patch_sha,
            "size": len(patch),
            "empty": not entries,
        },
        "entries": entries,
        "policy": {"passed": True, "violations": []},
    }
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        return collect(args)
    except CandidatePolicyError as error:
        rejected_manifest(args, str(error))
        print(f"candidate patch rejected: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
