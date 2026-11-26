#!/usr/bin/env python3
"""Small, fail-closed process sandbox for candidate code.

Candidate-owned JavaScript is only executed through this module.  Every
execution gets a private network namespace, a private PID namespace and an
unprivileged UID with an empty capability set.  If the namespaces cannot be
created the caller must treat the run as an infrastructure error; there is no
connected fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CANDIDATE_UID = 1001
_SANDBOX_TOOLS = ("unshare", "setpriv")


class SandboxUnavailable(RuntimeError):
    """Raised when the kernel/container cannot provide the isolation we need."""


def _prefix(args: list[str]) -> list[str]:
    missing = [name for name in _SANDBOX_TOOLS if shutil.which(name) is None]
    if missing:
        raise SandboxUnavailable(f"sandbox tools are missing: {missing}")
    return [
        "unshare",
        "--net",
        "--pid",
        "--fork",
        "--kill-child",
        "sh",
        "-c",
        'exec "$@"',
        "sh",
        "setpriv",
        f"--reuid={CANDIDATE_UID}",
        f"--regid={CANDIDATE_UID}",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
        "--",
        *args,
    ]


def isolated_environment() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "ALL_PROXY": "",
        "all_proxy": "",
        "NO_PROXY": "*",
        "no_proxy": "*",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_AUDIT": "false",
    }
    return env


def run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 600,
    input_data: str | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run ``args`` as UID 1001 inside private network/PID namespaces.

    ``input_data`` is written to the child's stdin.  The hidden harness uses it
    to receive evaluator-owned answer material *before* loading candidate code;
    unlike an argument or environment variable, it is not visible in
    ``process.argv``/``process.env``.  ``pass_fds`` keeps evaluator-owned
    descriptors (for example a pre-opened result file) open across
    ``unshare``/``setpriv``.
    """
    try:
        command = _prefix(args)
    except SandboxUnavailable:
        raise
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=isolated_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            input=input_data,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxUnavailable(f"cannot start sandboxed command: {exc}") from exc
    # ``unshare``/``setpriv`` report kernel or capability refusal on stderr and
    # exit non-zero before the payload starts.  That is an evaluator
    # environment failure, not a candidate failure; never score it as one.
    if result.returncode != 0 and any(
        marker in (result.stderr or "")
        for marker in ("unshare failed:", "setpriv: ")
    ):
        raise SandboxUnavailable((result.stderr or "").strip()[-1000:])
    return result


def chown_tree(root: Path, uid: int = CANDIDATE_UID) -> None:
    """Give the sandbox identity ownership of a scratch tree."""
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        try:
            os.chown(current_path, uid, uid, follow_symlinks=False)
            current_path.chmod(0o700)
        except OSError:
            continue
        for name in directories:
            path = current_path / name
            try:
                os.chown(path, uid, uid, follow_symlinks=False)
            except OSError:
                continue
        for name in files:
            path = current_path / name
            try:
                os.chown(path, uid, uid, follow_symlinks=False)
            except OSError:
                continue


def probe() -> dict[str, Any]:
    """Return a diagnostic record for the sandbox capability probe."""
    try:
        result = run(["true"], cwd=Path("/tmp"), timeout=10)
    except SandboxUnavailable as exc:
        return {"success": False, "status": "infrastructure_error", "error": str(exc)}
    return {
        "success": result.returncode == 0,
        "status": "passed" if result.returncode == 0 else "infrastructure_error",
        "exit_code": result.returncode,
        "stderr": (result.stderr or "")[-1000:],
    }


def main() -> int:
    report = probe()
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("success") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
