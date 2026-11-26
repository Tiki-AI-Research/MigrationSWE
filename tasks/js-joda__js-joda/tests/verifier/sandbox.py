"""Namespace + privilege sandbox shared by the Harbor verifier entry.

Harbor starts the separate verifier environment in an ordinary, capability-poor
container.  This module executes candidate and deterministic-evaluator commands
inside a private network/PID/mount namespace so candidate code can never reach
the network, then drops to an unprivileged UID with an empty capability set.

    - :func:`network_isolated_command` wraps evaluator commands in
      ``unshare --net --pid --fork --mount-proc --kill-child``.
    - :func:`sandbox_command` does the same for candidate commands and then
      drops to ``uid`` with ``setpriv``.

Namespace creation fails closed with ``Operation not permitted`` when
``tests/docker-compose.yaml`` did not grant ``CAP_SYS_ADMIN``/``CAP_NET_ADMIN``
and ``apparmor/seccomp=unconfined``; the caller classifies that as an
infrastructure error rather than silently running candidate code with network
access.  Runtime user namespaces are deliberately NOT used, so this never
depends on the host's unprivileged-userns policy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

CANDIDATE_UID = int(os.environ.get("MIGRATION_CANDIDATE_UID", "1001"))
SANDBOX_TOOLS = ("unshare", "setpriv", "ip")
_NAMESPACE_ERROR_MARKERS = (
    "unshare:",
    "unshare failed",
    "operation not permitted",
    "failed to mount /proc",
    "cannot change root filesystem propagation",
    "setpriv:",
)


def namespace_prefix() -> list[str]:
    """Create private network/PID/mount namespaces and enable loopback."""

    if os.name == "nt":
        return []
    return [
        "unshare",
        "--net",
        "--pid",
        "--fork",
        "--mount-proc",
        "--kill-child",
        "sh",
        "-c",
        'ip link set lo up && exec "$@"',
        "sh",
    ]


def capability_drop_prefix(uid: int = CANDIDATE_UID) -> list[str]:
    """Drop to ``uid`` with an empty capability set (applied after unshare)."""

    if os.name == "nt":
        return []
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


def network_isolated_command(args: Sequence[str]) -> list[str]:
    """Run an evaluator command inside the private network namespace only."""

    if os.name == "nt":
        return list(args)
    return [*namespace_prefix(), *args]


def sandbox_command(args: Sequence[str], *, uid: int = CANDIDATE_UID) -> list[str]:
    """Run a candidate command as ``uid`` inside the private namespaces."""

    if os.name == "nt":
        return list(args)
    return [*namespace_prefix(), *capability_drop_prefix(uid), *args]


def is_namespace_error(stderr: str) -> bool:
    lowered = (stderr or "").casefold()
    return any(marker in lowered for marker in _NAMESPACE_ERROR_MARKERS)


_TOOLCHAIN_ENV_EXACT = frozenset({
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL",
    "VIRTUAL_ENV", "JAVA_HOME", "MAVEN_HOME", "M2_HOME", "GRADLE_USER_HOME",
    "NODE_PATH", "PYTHONPATH", "RUSTUP_HOME", "RUST_VERSION", "CARGO_HOME",
    "GOPATH", "GOROOT", "GOFLAGS", "COREPACK_HOME", "MISE_DATA_DIR", "MISE_CACHE_DIR",
    "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH",
    "LD_LIBRARY_PATH", "CC", "CXX", "AS", "AR", "LD",
})
# Container toolchains need their own environment (module caches, rustup home,
# Java/Gradle homes).  A blanket ``os.environ`` copy would leak evaluator secrets
# to a tool that may load candidate-controlled config, so keep an allowlist.
_TOOLCHAIN_ENV_PREFIXES = (
    "GO", "CARGO_", "RUSTUP_", "RUST", "NPM_CONFIG_", "npm_config_",
    "JAVA_", "GRADLE_", "MAVEN_", "PIP_", "UV_", "CONDA", "DOTNET_", "MISE_", "SDKMAN",
)


def toolchain_environment() -> dict[str, str]:
    """Return the container's toolchain/compiler environment (allowlisted).

    the container merges declared variables onto the image ENV at launch, but the
    Harbor verifier runs the candidate through a subprocess with an explicit
    environment.  Carry the image's toolchain variables over so builds such as
    ``cargo``/``gradle``/``go`` keep their toolchain home and search paths.
    """

    prefixes = tuple(_TOOLCHAIN_ENV_PREFIXES)
    return {
        key: value
        for key, value in os.environ.items()
        if key in _TOOLCHAIN_ENV_EXACT or key.upper().startswith(prefixes)
    }


def candidate_environment(
    scratch: Path, *, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """A scrubbed environment for a candidate command with no proxy egress."""

    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
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
    }
    if extra:
        environment.update({str(key): str(value) for key, value in extra.items()})
    return environment


def check_sandbox_tools() -> dict[str, Any]:
    """Probe that the namespace/privilege tools exist and actually work."""

    missing = [name for name in SANDBOX_TOOLS if shutil.which(name) is None]
    if missing:
        return {
            "success": False,
            "status": "infrastructure_error",
            "missing": missing,
            "capabilities": {},
            "summary": f"candidate sandbox tools are missing: {missing}",
        }
    if os.name == "nt":
        return {"success": True, "status": "passed", "missing": [], "capabilities": {}}
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
    passed = result.returncode == 0
    return {
        "success": passed,
        "status": "passed" if passed else "infrastructure_error",
        "missing": [],
        "capabilities": {
            "network_namespace": passed,
            "pid_namespace": passed,
            "mount_namespace": passed,
            "capability_drop": passed,
        },
        "probe": probe,
        "exit_code": result.returncode,
        "stdout": (result.stdout or "")[-2000:],
        "stderr": (result.stderr or "")[-2000:],
        "summary": (
            "candidate sandbox tools and namespace capabilities are available"
            if passed
            else "candidate sandbox namespace capabilities are unavailable"
        ),
    }


__all__ = [
    "CANDIDATE_UID",
    "SANDBOX_TOOLS",
    "candidate_environment",
    "toolchain_environment",
    "capability_drop_prefix",
    "check_sandbox_tools",
    "is_namespace_error",
    "namespace_prefix",
    "network_isolated_command",
    "sandbox_command",
]
