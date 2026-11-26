from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO = Path("/workspace/repo")
LOGS = Path("/logs/artifacts")


def run_git(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    patch_path = LOGS / "model.patch"
    metadata_path = LOGS / "model-patch.json"
    metadata: dict[str, object] = {
        "schema_version": "harbor-task-eval/patch-v1",
        "repo": str(REPO),
        "patch_path": str(patch_path),
        "metadata_path": str(metadata_path),
    }

    try:
        base = (REPO / ".base_commit").read_text(encoding="utf-8").strip()
        if not base:
            raise RuntimeError(".base_commit is empty")
        env = os.environ.copy()
        with tempfile.TemporaryDirectory(prefix="patch-export-", dir=LOGS) as temp:
            env["GIT_INDEX_FILE"] = str(Path(temp) / "index")
            for args in (["cat-file", "-e", f"{base}^{{commit}}"], ["read-tree", base], ["add", "-A", "--", "."]):
                result = run_git(list(args), env)
                if result.returncode:
                    raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
            # Keep Harbor's bookkeeping markers out of the exported patch.  Removing
            # them from the temporary index would turn their absence into a deletion
            # relative to the base tree, so exclude them at diff time instead.
            pathspec = [
                ".",
                ":(exclude).base_commit",
                ":(exclude).base_tree_hash",
                ":(exclude).source_base_commit",
            ]
            names = run_git(
                [
                    "diff", "--cached", "--name-only", "--find-renames", base,
                    "--", *pathspec,
                ],
                env,
            )
            diff = run_git(
                [
                    "diff", "--cached", "--binary", "--full-index", "--find-renames",
                    "--no-ext-diff", base, "--", *pathspec,
                ],
                env,
            )
            if names.returncode or diff.returncode:
                raise RuntimeError("git diff failed")
            patch_bytes = diff.stdout
            patch_path.write_bytes(patch_bytes)
            metadata.update(
                status="ok" if patch_bytes else "empty",
                base_commit=base,
                changed_files=[
                    line.decode("utf-8", "replace")
                    for line in names.stdout.splitlines()
                    if line and line != b".base_commit"
                ],
                patch_bytes=len(patch_bytes),
                patch_sha256=hashlib.sha256(patch_bytes).hexdigest(),
            )
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        metadata.update(status="export_error", error=f"{type(exc).__name__}: {exc}")
        patch_path.unlink(missing_ok=True)
    finally:
        metadata_path.write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    return 0 if metadata.get("status") in {"ok", "empty"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
