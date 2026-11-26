from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "harbor-task-eval/patch-v1"


def _run(
    repo: Path,
    args: list[str],
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a bounded, byte-preserving Git command in the candidate repository."""

    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def export_patch(repo: Path, logs: Path) -> dict[str, Any]:
    """Export all candidate changes without modifying the evaluator's Git index."""

    repo = repo.resolve()
    logs = logs.resolve()
    logs.mkdir(parents=True, exist_ok=True)
    patch_path = logs / "model.patch"
    metadata_path = logs / "model-patch.json"
    marker = repo / ".base_commit"

    patch_path.unlink(missing_ok=True)
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo": str(repo),
        "patch_path": str(patch_path),
        "metadata_path": str(metadata_path),
    }

    try:
        if not repo.is_dir():
            metadata.update(status="invalid_repo", error="candidate repository is missing")
            return metadata
        if not marker.is_file():
            metadata.update(status="missing_base_commit", error=".base_commit is missing")
            return metadata

        base = marker.read_text(encoding="utf-8").strip()
        if not base:
            metadata.update(status="invalid_base_commit", error=".base_commit is empty")
            return metadata
        if _run(repo, ["cat-file", "-e", f"{base}^{{commit}}" ]).returncode != 0:
            metadata.update(status="invalid_base_commit", base_commit=base)
            return metadata

        with tempfile.TemporaryDirectory(prefix="patch-export-", dir=logs) as temp_dir:
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")

            for command in (["read-tree", base], ["add", "-A", "--", "."]):
                result = _run(repo, list(command), env)
                if result.returncode != 0:
                    metadata.update(
                        status="git_error",
                        command=command,
                        exit_code=result.returncode,
                        error=_decode(result.stderr),
                    )
                    return metadata

            remove_marker = _run(
                repo,
                ["rm", "--cached", "--ignore-unmatch", ".base_commit"],
                env,
            )
            if remove_marker.returncode != 0:
                metadata.update(
                    status="git_error",
                    command=["rm", "--cached", "--ignore-unmatch", ".base_commit"],
                    exit_code=remove_marker.returncode,
                    error=_decode(remove_marker.stderr),
                )
                return metadata

            names = _run(
                repo,
                ["diff", "--cached", "--name-only", "--find-renames", base, "--", "."],
                env,
            )
            diff = _run(
                repo,
                [
                    "diff",
                    "--cached",
                    "--binary",
                    "--full-index",
                    "--find-renames",
                    "--no-ext-diff",
                    base,
                    "--",
                    ".",
                ],
                env,
            )
            if names.returncode != 0 or diff.returncode != 0:
                metadata.update(
                    status="git_error",
                    command="git diff",
                    exit_code=max(names.returncode, diff.returncode),
                    error=_decode(names.stderr + diff.stderr),
                )
                return metadata

            patch_bytes = diff.stdout
            patch_path.write_bytes(patch_bytes)
            metadata.update(
                status="ok" if patch_bytes else "empty",
                base_commit=base,
                changed_files=[
                    line.decode("utf-8", errors="replace")
                    for line in names.stdout.splitlines()
                    if line and line != b".base_commit"
                ],
                patch_bytes=len(patch_bytes),
                patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
            )
            return metadata
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        metadata.update(status="export_error", error=f"{type(exc).__name__}: {exc}")
        return metadata
    finally:
        _write_json(metadata_path, metadata)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a Harbor candidate patch")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    args = parser.parse_args()

    result = export_patch(args.repo, args.logs)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"ok", "empty"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
