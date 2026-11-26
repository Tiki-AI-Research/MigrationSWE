from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


CANDIDATE_UID = 1001
CANDIDATE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "NPM_CONFIG_CACHE",
        "LANG",
        "LC_ALL",
        "SHARING_TEST",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    }
)
SANDBOX_TOOLS = ("node", "setpriv", "unshare", "ip")
SANDBOX_ROOT = Path("/workspace/candidate-sandboxes")
NODE_RUNTIME = "/opt/node14/bin/node"
NODE_RUNTIME_VERSION = "v14.18.0"


def network_isolated_command(args: list[str]) -> list[str]:
    """Isolate evaluator networking while preserving candidate UID mappings.

    The evaluator keeps its original user namespace so controllers can chown
    scratch files and drop to UID 1001. Only loopback is enabled for local HTTP.
    Namespace creation failures must never fall back to a connected process.
    """
    if os.name == "nt":
        return list(args)
    return [*namespace_prefix(), *args]


def candidate_environment(scratch: Path) -> dict[str, str]:
    env = {
        "PATH": "/opt/node14/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "NPM_CONFIG_CACHE": str(scratch / "cache" / "npm"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "SHARING_TEST": "1",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "http_proxy": "",
        "https_proxy": "",
        "ALL_PROXY": "",
        "all_proxy": "",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    if set(env) != CANDIDATE_ENV_KEYS:
        raise RuntimeError("candidate environment whitelist drifted")
    return env


def namespace_prefix() -> list[str]:
    """Create private network/PID/mount namespaces and enable loopback.

    Namespace creation requires ``CAP_SYS_ADMIN`` (plus ``CAP_NET_ADMIN`` for
    the ``ip link set lo up`` step) and ``--mount-proc`` additionally requires
    the container's AppArmor profile to permit mount propagation. Harbor starts
    the separate verifier environment with Docker's default, capability-poor
    settings, so ``tests/docker-compose.yaml`` grants exactly those privileges.

    The namespaces are always created by the evaluator identity; no user
    namespace is created, so this never depends on the host's unprivileged
    user-namespace policy.
    """
    return [
        "unshare",
        "--net",
        "--pid",
        "--fork",
        "--mount-proc",
        "--kill-child",
        "sh", "-c", 'ip link set lo up && exec "$@"', "sh",
    ]


def capability_drop_prefix(uid: int) -> list[str]:
    """Drop to the candidate UID with an empty capability set.

    Applied after namespace creation so candidate code runs unprivileged
    without needing to create its own user namespace.
    """
    return [
        "setpriv",
        f"--reuid={uid}",
        f"--regid={uid}",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
    ]


def sandbox_command(args: list[str], *, uid: int = CANDIDATE_UID) -> list[str]:
    """Run a candidate command as ``uid`` inside private namespaces."""
    effective_args = [NODE_RUNTIME, *args[1:]] if args and args[0] == "node" else args
    return [
        *namespace_prefix(),
        *capability_drop_prefix(uid),
        *effective_args,
    ]


def check_sandbox_tools() -> dict[str, Any]:
    missing = [name for name in (*SANDBOX_TOOLS, NODE_RUNTIME) if shutil.which(name) is None]
    if missing:
        return {
            "success": False,
            "status": "infrastructure_error",
            "missing": missing,
            "capabilities": {},
            "summary": f"candidate sandbox tools are missing: {missing}",
        }

    probe = [*namespace_prefix(), *capability_drop_prefix(CANDIDATE_UID), "true"]
    try:
        result = subprocess.run(
            probe,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "success": False,
            "status": "infrastructure_error",
            "missing": [],
            "capabilities": {
                "network_namespace": False,
                "pid_namespace": False,
                "mount_namespace": False,
                "capability_drop": False,
            },
            "probe": probe,
            "summary": f"sandbox namespace capability probe failed: {exc}",
        }
    capabilities = {
        "network_namespace": result.returncode == 0,
        "pid_namespace": result.returncode == 0,
        "mount_namespace": result.returncode == 0,
        "capability_drop": result.returncode == 0,
    }
    return {
        "success": result.returncode == 0,
        "status": "passed" if result.returncode == 0 else "infrastructure_error",
        "missing": [],
        "capabilities": capabilities,
        "probe": probe,
        "exit_code": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
        "summary": (
            "candidate sandbox tools and namespace capabilities are available"
            if result.returncode == 0
            else "candidate sandbox namespace capabilities are unavailable"
        ),
    }


def _make_scratch() -> Path:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True, mode=0o711)
    SANDBOX_ROOT.chmod(0o711)
    scratch = Path(tempfile.mkdtemp(prefix="probe-", dir=SANDBOX_ROOT))
    os.chown(scratch, CANDIDATE_UID, CANDIDATE_UID)
    scratch.chmod(0o700)
    for name in ("home", "tmp", "cache"):
        child = scratch / name
        child.mkdir(mode=0o700)
        os.chown(child, CANDIDATE_UID, CANDIDATE_UID)
    return scratch


def _chown_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        os.chown(current_path, CANDIDATE_UID, CANDIDATE_UID, follow_symlinks=False)
        current_path.chmod(0o700)
        for name in directories:
            path = current_path / name
            os.chown(path, CANDIDATE_UID, CANDIDATE_UID, follow_symlinks=False)
        for name in files:
            path = current_path / name
            os.chown(path, CANDIDATE_UID, CANDIDATE_UID, follow_symlinks=False)


def prepare_candidate_workspace(repo: Path) -> tuple[Path, Path]:
    """Create an isolated writable copy without evaluator-owned Git metadata."""
    scratch = _make_scratch()
    workspace = scratch / "repo"
    excluded = shutil.ignore_patterns(
        ".git",
        ".base_commit",
        ".base_tree_hash",
        ".source_base_commit",
        ".git-fsck-unreachable",
        ".git-object-inventory",
        ".git-reflog",
    )
    try:
        shutil.copytree(repo, workspace, symlinks=True, ignore=excluded)
        _chown_tree(workspace)
    except (OSError, shutil.Error):
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return scratch, workspace


def cleanup_candidate_workspace(scratch: Path) -> None:
    shutil.rmtree(scratch, ignore_errors=True)


def run_candidate_command(
    repo: Path,
    args: list[str],
    *,
    timeout: int = 60,
    stdin: str | None = None,
) -> dict[str, Any]:
    tools = check_sandbox_tools()
    if tools["success"] is not True:
        return {
            "status": "infrastructure_error",
            "success": None,
            "command": sandbox_command(args),
            "exit_code": None,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "error": tools["summary"],
        }

    try:
        scratch, workspace = prepare_candidate_workspace(repo)
    except OSError as exc:
        return {
            "status": "infrastructure_error",
            "success": None,
            "command": sandbox_command(args),
            "exit_code": None,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "error": f"unable to create candidate scratch: {exc}",
        }

    command = sandbox_command(args)
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            input=stdin,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
            env=candidate_environment(scratch),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "candidate_failure",
            "success": False,
            "command": command,
            "exit_code": -1,
            "timed_out": True,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout": "",
            "stderr": "",
            "error": f"candidate process timed out: {exc}",
        }
    except OSError as exc:
        return {
            "status": "infrastructure_error",
            "success": None,
            "command": command,
            "exit_code": None,
            "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout": "",
            "stderr": "",
            "error": f"unable to start candidate sandbox: {exc}",
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    stdout = result.stdout[-16000:] if result.stdout else ""
    stderr = result.stderr[-16000:] if result.stderr else ""
    for absolute in (
        str(repo),
        str(repo.resolve()).replace("\\", "/"),
        str(workspace),
        str(workspace.resolve()).replace("\\", "/"),
    ):
        stdout = stdout.replace(absolute, "<candidate-repo>")
        stderr = stderr.replace(absolute, "<candidate-repo>")

    namespace_error = result.returncode != 0 and any(
        marker in stderr.lower()
        for marker in (
            "unshare:",
            "unshare failed",
            "operation not permitted",
            "failed to mount /proc",
            "cannot change root filesystem propagation",
            "setpriv:",
        )
    )
    status = (
        "passed"
        if result.returncode == 0
        else ("infrastructure_error" if namespace_error else "candidate_failure")
    )
    return {
        "status": status,
        "success": result.returncode == 0 if not namespace_error else None,
        "command": command,
        "exit_code": result.returncode,
        "timed_out": False,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout": stdout,
        "stderr": stderr,
        "runtime": {
            "name": "node",
            "path": NODE_RUNTIME,
            "required_version": NODE_RUNTIME_VERSION,
        },
        "workspace": {
            "kind": "independent_writable_copy",
            "git_metadata_included": False,
            "dedicated_home_tmp_cache": True,
        },
    }
