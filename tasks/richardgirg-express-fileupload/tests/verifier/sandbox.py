#!/usr/bin/env python3
"""Namespace + privilege isolation for every subprocess that runs candidate code.

Ported from the ``sandbox-unshare`` reference project (``tests/verifier/sandbox.py``
and ``tests/docker-compose.yaml``). Candidate code is fenced off by two
independent barriers that are applied in this order:

1. ``unshare --net --pid --fork --mount-proc --kill-child`` puts the process in
   private network / PID / mount namespaces. The fresh network namespace only
   ever has loopback (which the HTTP-based tests need), so neither candidate nor
   evaluator commands can reach a real endpoint; ``--kill-child`` guarantees a
   timed-out command cannot leave background processes behind.
2. ``setpriv --reuid --regid --clear-groups --inh-caps=-all --ambient-caps=-all
   --bounding-set=-all --no-new-privs`` then drops the process to the
   unprivileged candidate identity with an empty capability set, so ``lib/``
   cannot read the frozen F2P payload, the node registry or the manifests under
   ``/tests``.

The evaluator itself keeps root inside the same namespaces
(``network_isolated_command``) so it can still stage scratch trees, hand over
the candidate's report directory and read the frozen assets.

Namespace creation needs ``CAP_SYS_ADMIN`` + ``CAP_NET_ADMIN`` and an unconfined
AppArmor profile; ``tests/docker-compose.yaml`` grants exactly those and nothing
more. If the platform starts the verifier without them, ``unshare`` fails with
EPERM and the command is reported as an infrastructure error -- never as a
silent, network-connected fallback to root.

Deliberate deviation from the reference: the reference also gives every node a
fresh writable copy of the repository. This task stages its own candidate
worktree in the hard gate and runs the frozen suites against ``/workspace/repo``,
so only the namespace / identity barriers are ported here.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# ``agent`` is the unprivileged identity the environment image already creates
# and uses to own ``/workspace/repo``; candidate code runs as that user.
DEFAULT_USER = "agent"

# Exit code used when loopback could not be enabled inside the new network
# namespace. It is deliberately unusual so it can never be confused with a
# candidate exit status.
LOOPBACK_FAILURE_CODE = 111

# Tools the namespace barrier is built from. ``ip`` is *not* required: the base
# image ships without iproute2, so loopback is enabled by netns_loopback.py.
SANDBOX_TOOLS = ("unshare", "setpriv")

# stderr markers that mean "the sandbox itself failed", not "the candidate
# failed". They are what turns an unavailable namespace into an infrastructure
# error instead of a zero score for an honest candidate.
NAMESPACE_ERROR_MARKERS = (
    "unshare:",
    "unshare failed",
    "operation not permitted",
    "failed to mount /proc",
    "cannot change root filesystem propagation",
    "setpriv:",
    "loopback-up failed",
)


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def sandbox_user() -> Optional[str]:
    """The identity candidate code must run as (``None`` disables the drop)."""
    explicit = os.environ.get("MIGRATION_SANDBOX_USER")
    if explicit is not None:
        explicit = explicit.strip()
        return explicit or None
    return DEFAULT_USER


def user_exists(name: Optional[str]) -> bool:
    if not name:
        return False
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def candidate_ids(user: Optional[str] = None) -> Optional[tuple]:
    """(uid, gid) of the candidate identity, or ``None`` when it is missing."""
    target = sandbox_user() if user is None else user
    if not target:
        return None
    try:
        record = pwd.getpwnam(target)
    except KeyError:
        return None
    return record.pw_uid, record.pw_gid


def candidate_uid(user: Optional[str] = None) -> Optional[int]:
    ids = candidate_ids(user)
    return ids[0] if ids else None


def isolation_supported() -> bool:
    """True when the namespace barrier can actually be applied right now."""
    return is_linux() and is_root() and shutil.which("unshare") is not None


def _python_bin() -> str:
    return sys.executable or shutil.which("python3") or "python3"


def _loopback_helper() -> str:
    return str(Path(__file__).resolve().parent / "netns_loopback.py")


def namespace_prefix() -> List[str]:
    """Create private network/PID/mount namespaces and enable loopback.

    ``ip link set lo up`` is attempted when iproute2 happens to be installed,
    but the authoritative step is ``netns_loopback.py``: the frozen environment
    image has no ``ip``, and a network namespace whose loopback is down makes
    every ``app.listen(0)`` / supertest call fail with ``EADDRNOTAVAIL``, which
    would look like a candidate regression. A loopback failure is fatal and is
    classified as an infrastructure error.
    """
    enable_loopback = (
        "command -v ip >/dev/null 2>&1 && ip link set lo up 2>/dev/null; "
        f'"{_python_bin()}" "{_loopback_helper()}" || exit {LOOPBACK_FAILURE_CODE}; '
        'exec "$@"'
    )
    return [
        "unshare",
        "--net",
        "--pid",
        "--fork",
        "--mount-proc",
        "--kill-child",
        "sh",
        "-c",
        enable_loopback,
        "sh",
    ]


def capability_drop_prefix(uid: int, gid: Optional[int] = None) -> List[str]:
    """Drop to the candidate identity with an empty capability set.

    Applied *after* namespace creation, so candidate code never needs (and never
    gets) the privileges that namespace creation itself required.
    """
    gid = uid if gid is None else gid
    return [
        "setpriv",
        f"--reuid={uid}",
        f"--regid={gid}",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
    ]


# --- candidate environment -------------------------------------------------
# Candidate code runs inside the same container as the evaluator. The evaluator
# may hold model-gateway credentials and proxy settings, so candidate processes
# must never inherit its environment wholesale: only the keys below are passed
# through, and each has a concrete reason to exist.
#   PATH/LANG/LC_* /TZ  - run the pinned node runtime with the frozen locale.
#   NODE_ENV/NODE_PATH  - point node at the frozen dependencies.
#   MIGRATION_*         - the pinned runtime the verifier selected.
CANDIDATE_ENV_PASSTHROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "NODE_ENV",
    "NODE_PATH",
    "MIGRATION_NODE",
    "MIGRATION_PYTHON",
)

# Directory names under the sandbox root. The root itself stays root-owned and
# ``0711`` (traversable, not listable); each leaf belongs to the candidate so a
# normal ``~/.npm`` or ``os.tmpfile()`` write cannot fail with EACCES and turn
# into a phantom regression.
CANDIDATE_SANDBOX_ROOT = Path(
    os.environ.get("MIGRATION_CANDIDATE_SANDBOX_ROOT") or "/workspace/candidate-sandbox"
)
CANDIDATE_SANDBOX_SUBDIRS = ("home", "tmp", "cache", "npm")

# Every proxy spelling is blanked explicitly (an empty value means "no proxy"
# for both curl and node), then NO_PROXY is pinned to ``*`` so no inherited
# proxy *and* no inherited no-proxy exception can leak through.
CANDIDATE_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "ftp_proxy",
    "NPM_CONFIG_PROXY",
    "NPM_CONFIG_HTTPS_PROXY",
    "npm_config_proxy",
    "npm_config_https_proxy",
)

CANDIDATE_ENV_DENY_PREFIXES = (
    "OPENAI_",
    "ANTHROPIC_",
    "CLAUDE_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GEMINI_",
    "OPENROUTER_",
    "LANGSMITH_",
    "HF_",
)

CANDIDATE_ENV_DENY_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")


def _candidate_extra_allowed(key: str) -> bool:
    upper = key.upper()
    if any(upper.startswith(prefix) for prefix in CANDIDATE_ENV_DENY_PREFIXES):
        return False
    tokens = {token for token in re.split(r"[^A-Z0-9]+", upper) if token}
    return not any(part in tokens for part in CANDIDATE_ENV_DENY_PARTS)


def candidate_dirs(root: Optional[Path] = None) -> Dict[str, Path]:
    base = Path(root) if root is not None else CANDIDATE_SANDBOX_ROOT
    return {name: base / name for name in CANDIDATE_SANDBOX_SUBDIRS}


def ensure_candidate_dirs(root: Optional[Path] = None) -> Dict[str, Path]:
    """Create the dedicated candidate home/tmp/cache dirs when running as root.

    The parent is deliberately ``0711`` and root-owned so the candidate can
    traverse into its own leaves but cannot list the sandbox root. The leaves
    are handed to the candidate identity and kept ``0700``. On a non-Linux
    development host (where the sandbox is a no-op) failures are tolerated; the
    evaluation image creates and validates the same tree at build time.
    """
    dirs = candidate_dirs(root)
    parent = dirs["home"].parent
    ids = candidate_ids()
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if is_root():
            os.chown(parent, 0, 0)
        os.chmod(parent, 0o711)
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
            if is_root() and ids is not None:
                os.chown(path, ids[0], ids[1])
            os.chmod(path, 0o700)
    except OSError:
        pass
    return dirs


def candidate_env(
    base: Optional[Dict[str, str]] = None,
    root: Optional[Path] = None,
    extra: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Build the explicit whitelist environment for a candidate subprocess.

    ``base`` is the evaluator's mapping (usually ``os.environ`` with the
    per-command ``NODE_ENV``/``NODE_PATH`` overrides already merged in). Only
    :data:`CANDIDATE_ENV_PASSTHROUGH` keys are copied from it; everything else
    -- model gateway creds, arbitrary secrets, proxy configuration -- is
    dropped. ``extra`` carries evaluator-supplied per-command overrides (the
    runtime probe's activation flags) and is still filtered for secret-shaped
    names.
    """
    source = os.environ if base is None else base
    env: Dict[str, str] = {}
    for key in CANDIDATE_ENV_PASSTHROUGH:
        value = source.get(key)
        if value is not None:
            env[key] = str(value)
    for key, value in (extra or {}).items():
        if key in CANDIDATE_ENV_PASSTHROUGH:
            continue
        if _candidate_extra_allowed(str(key)):
            env[str(key)] = str(value)
    dirs = ensure_candidate_dirs(root)
    env["HOME"] = str(dirs["home"])
    env["TMPDIR"] = str(dirs["tmp"])
    env["XDG_CACHE_HOME"] = str(dirs["cache"])
    env["NPM_CONFIG_CACHE"] = str(dirs["npm"])
    for key in CANDIDATE_PROXY_KEYS:
        env[key] = ""
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def is_candidate_command(cmd: Sequence[str]) -> bool:
    """True when ``cmd`` is a command already wrapped for the candidate.

    Candidate commands are the ones that carry the ``setpriv`` capability-drop
    prefix from :func:`sandbox_command`; evaluator commands only get the
    namespace prefix and keep root's environment.
    """
    return any(Path(str(part)).name == "setpriv" for part in cmd if part)


def network_isolated_command(args: Sequence[str]) -> List[str]:
    """Wrap an *evaluator* command in the private namespaces, keeping root.

    Namespace creation failures must never fall back to a connected process.
    """
    command = list(args)
    if not isolation_supported():
        return command
    return [*namespace_prefix(), *command]


def sandbox_command(args: Sequence[str], user: Optional[str] = None) -> List[str]:
    """Run a *candidate* command in the namespaces, dropped to ``user``."""
    command = list(args)
    if not isolation_supported():
        return command
    prefix = namespace_prefix()
    ids = candidate_ids(user)
    if is_root() and ids is not None and shutil.which("setpriv") is not None:
        prefix = [*prefix, *capability_drop_prefix(ids[0], ids[1])]
    return [*prefix, *command]


def wrap(cmd: Sequence[str], user: Optional[str] = None) -> List[str]:
    """Backwards-compatible alias for :func:`sandbox_command`."""
    return sandbox_command(cmd, user)


def check_sandbox_tools() -> Dict[str, Any]:
    """Fail-closed capability probe for the namespace barrier.

    Returns ``success=False`` whenever the platform claims to be Linux but the
    sandbox cannot be built, so the caller reports an infrastructure error
    instead of scoring an unisolated run.
    """
    if not is_linux():
        return {
            "success": True,
            "status": "unsupported_platform",
            "platform": sys.platform,
            "summary": "namespace isolation is a Linux feature; this development host runs without it",
        }

    missing = [tool for tool in SANDBOX_TOOLS if shutil.which(tool) is None]
    ids = candidate_ids()
    if missing or not is_root() or ids is None:
        return {
            "success": False,
            "status": "infrastructure_error",
            "missing": missing,
            "running_as_root": is_root(),
            "candidate_user": sandbox_user(),
            "capabilities": {
                "network_namespace": False,
                "pid_namespace": False,
                "mount_namespace": False,
                "capability_drop": False,
            },
            "summary": (
                "candidate sandbox cannot be built: "
                + ("missing tools " + repr(missing) if missing else "")
                + ("not running as root " if not is_root() else "")
                + (f"candidate user {sandbox_user()!r} does not exist" if ids is None else "")
            ).strip(),
        }

    probe = [*namespace_prefix(), *capability_drop_prefix(ids[0], ids[1]), "true"]
    try:
        result = subprocess.run(
            probe,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
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
    ok = result.returncode == 0
    return {
        "success": ok,
        "status": "passed" if ok else "infrastructure_error",
        "missing": [],
        "probe": probe,
        "exit_code": result.returncode,
        "stdout": (result.stdout or "")[-2000:],
        "stderr": (result.stderr or "")[-2000:],
        "capabilities": {
            "network_namespace": ok,
            "pid_namespace": ok,
            "mount_namespace": ok,
            "capability_drop": ok,
        },
        "summary": (
            "candidate sandbox tools and namespace capabilities are available"
            if ok
            else "candidate sandbox namespace capabilities are unavailable "
                 "(CAP_SYS_ADMIN/CAP_NET_ADMIN missing: check tests/docker-compose.yaml)"
        ),
    }


def namespace_error(result: Dict[str, Any]) -> bool:
    """True when a failed command failed because the sandbox is unavailable."""
    if result.get("returncode") in (0, None):
        return False
    stderr = str(result.get("stderr") or "").lower()
    return any(marker in stderr for marker in NAMESPACE_ERROR_MARKERS)


def describe(user: Optional[str] = None) -> Dict[str, Any]:
    """Auditable description of the isolation that was actually applied.

    A silent privilege or namespace fallback is exactly how "the candidate
    cannot read /tests / cannot reach the network" degrades into a lie, so the
    effective configuration is recorded in every report.
    """
    target = sandbox_user() if user is None else user
    ids = candidate_ids(target)
    network = isolation_supported()
    dropped = network and ids is not None and shutil.which("setpriv") is not None
    return {
        "requested_user": target,
        "enforced": dropped,
        "running_as_root": is_root(),
        "user_exists": user_exists(target),
        "candidate_uid": ids[0] if ids else None,
        "unshare": shutil.which("unshare"),
        "setpriv": shutil.which("setpriv"),
        "network_isolated": network,
        "network_namespace": "unshare --net --pid --fork --mount-proc --kill-child" if network else None,
        "capability_drop": (
            "setpriv --reuid --regid --clear-groups --inh-caps=-all --ambient-caps=-all "
            "--bounding-set=-all --no-new-privs"
        ) if dropped else None,
    }


def grant(path: Path, user: Optional[str] = None) -> None:
    """Let the sandbox identity write into ``path`` (a private scratch dir).

    Subprocesses that run as the candidate still have to hand their reports back
    to the root-owned verifier. A root-created ``0700`` temp dir would make that
    impossible, which is exactly how "the suite produced no parsable report"
    turns into a phantom infrastructure error.
    """
    ids = candidate_ids(user)
    if is_root() and ids is not None:
        try:
            os.chown(path, ids[0], ids[1])
            os.chmod(path, 0o700)
            return
        except OSError:
            pass
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass


def open_tree(root: Path) -> None:
    """Make a staged tree traversable/readable for the sandbox identity.

    Used on the hard gate's private scratch checkout: the probe runs as the
    unprivileged user but must still be able to read the staged candidate.
    Symlinks are skipped so a staged ``node_modules`` link never chmods its
    real target.
    """
    try:
        os.chmod(root, 0o755)
    except OSError:
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            target = Path(dirpath) / name
            if target.is_symlink():
                continue
            try:
                os.chmod(target, 0o755)
            except OSError:
                pass
        for name in filenames:
            target = Path(dirpath) / name
            if target.is_symlink():
                continue
            try:
                os.chmod(target, (target.stat().st_mode & 0o777) | 0o444)
            except OSError:
                pass


def grant_tree(root: Path, user: Optional[str] = None) -> None:
    """Hand a staged candidate tree to the sandbox identity.

    ``open_tree`` only grants *read* access, which is enough for the syntax and
    runtime probes but not for a behavioral suite: the P2P tests create fixtures
    under ``test/`` and write temporary uploads. A root-owned staged tree makes
    those writes fail with EACCES, which would masquerade as a candidate
    regression. Only the owner bits matter (the tree keeps its ``0755``/``0644``
    modes), so re-owning it is sufficient and does not widen access for anybody
    else.

    The tree is a disposable copy owned by the evaluation, so a candidate that
    rewrites it during the run can only affect its own staged copy.
    """
    ids = candidate_ids(user)
    if not (is_root() and ids is not None):
        return
    uid, gid = ids

    def retarget(path: Path) -> None:
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
        except OSError:
            pass

    retarget(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in [*dirnames, *filenames]:
            path = current / name
            if path.is_symlink():
                continue
            retarget(path)
