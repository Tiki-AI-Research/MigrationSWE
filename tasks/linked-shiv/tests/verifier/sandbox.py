"""Fail-closed process and filesystem isolation for candidate execution."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

CANDIDATE_UID = 1001
CANDIDATE_GID = 1001
SANDBOX_ROOT = Path(os.environ.get("HARBOR_SANDBOX_ROOT", "/workspace/candidate-sandboxes"))
_BASE_ENV = {
    "PATH", "HOME", "TMPDIR", "XDG_CACHE_HOME", "PIP_CACHE_DIR",
    "PIP_NO_INDEX", "PIP_NO_BUILD_ISOLATION", "LANG", "LC_ALL",
    "PYTHONHASHSEED", "PYTHONUNBUFFERED", "SHIV_REPO_ROOT", "SHIV_ROOT",
    "SHIV_ENTRY_POINT", "SHIV_CONSOLE_SCRIPT", "SHIV_INTERPRETER",
    "SHIV_FORCE_EXTRACT", "SHIV_COMPILE_PYC", "SHIV_COMPILE_WORKERS",
    "SHIV_EXTEND_PYTHONPATH", "SHIV_PREPEND_PYTHONPATH",
    "HARBOR_READONLY_ROOT", "HARBOR_WRITABLE_PATHS",
}
SANDBOX_TOOLS = ("mount", "setpriv", "unshare", "ip", "sh")
_SANDBOX_TOOLS = SANDBOX_TOOLS
_NAMESPACE_MARKERS = (
    "unshare:", "unshare failed", "operation not permitted", "failed to mount",
    "cannot mount", "cannot create new namespace", "setpriv:", "mount:",
)


def namespace_prefix() -> list[str]:
    if os.name == "nt":
        return []
    setup = (
        'ip link set lo up && '
        'if [ -n "${HARBOR_READONLY_ROOT:-}" ]; then '
        'old_ifs=$IFS; IFS=:; '
        'mount --bind "$HARBOR_READONLY_ROOT" "$HARBOR_READONLY_ROOT" || exit 125; '
        'mount -o remount,bind,ro "$HARBOR_READONLY_ROOT" || exit 125; '
        'for writable in ${HARBOR_WRITABLE_PATHS:-}; do '
        '[ -n "$writable" ] || continue; '
        'mount --bind "$writable" "$writable" || exit 125; '
        'mount -o remount,bind,rw "$writable" || exit 125; '
        'done; IFS=$old_ifs; cd "$HARBOR_READONLY_ROOT" || exit 125; fi; exec "$@"'
    )
    return ["unshare", "--net", "--pid", "--fork", "--mount-proc", "--kill-child", "sh", "-c", setup, "sh"]


def capability_drop_prefix(uid: int = CANDIDATE_UID, gid: int = CANDIDATE_GID) -> list[str]:
    if os.name == "nt":
        return []
    return [
        "setpriv", f"--reuid={int(uid)}", f"--regid={int(gid)}", "--clear-groups",
        "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all", "--no-new-privs",
    ]


def network_isolated_command(args: list[str]) -> list[str]:
    return [*namespace_prefix(), *args]


def sandbox_command(args: list[str], *, uid: int = CANDIDATE_UID, gid: int = CANDIDATE_GID) -> list[str]:
    return [*namespace_prefix(), *capability_drop_prefix(uid, gid), *args]


def candidate_environment(
    scratch: Path,
    extra: Mapping[str, object] | None = None,
    *,
    readonly_root: Path | None = None,
    writable_paths: Sequence[Path] = (),
) -> dict[str, str]:
    path = os.defpath
    if readonly_root is not None:
        candidate_bin = readonly_root / ".venv" / "bin"
        path = os.pathsep.join((str(candidate_bin), os.defpath))
    environment = {
        "PATH": path, "HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"),
        "XDG_CACHE_HOME": str(scratch / "cache"), "PIP_CACHE_DIR": str(scratch / "cache" / "pip"),
        "COVERAGE_FILE": str(scratch / ".coverage"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HTTP_PROXY": "", "HTTPS_PROXY": "",
        "ALL_PROXY": "", "NO_PROXY": "*", "http_proxy": "", "https_proxy": "",
        "all_proxy": "", "no_proxy": "*",
    }
    if extra:
        for key, value in extra.items():
            key = str(key)
            if key in _BASE_ENV or key.startswith("PIP_") or key.startswith("SHIV_"):
                environment[key] = str(value)
    environment["SHIV_REPO_ROOT"] = str(scratch / "repo")
    if readonly_root is not None:
        environment["HARBOR_READONLY_ROOT"] = str(readonly_root)
        environment["HARBOR_WRITABLE_PATHS"] = ":".join(str(path) for path in writable_paths)
    return environment


def check_sandbox_tools() -> dict[str, Any]:
    if os.name == "nt":
        return {"success": False, "status": "infrastructure_error", "missing": list(_SANDBOX_TOOLS), "capabilities": {}, "summary": "candidate sandbox requires a Linux namespace-capable runtime"}
    missing = [name for name in _SANDBOX_TOOLS if shutil.which(name) is None]
    if missing:
        return {"success": False, "status": "infrastructure_error", "missing": missing, "capabilities": {}, "summary": f"candidate sandbox tools are missing: {missing}"}
    probe = sandbox_command(["true"])
    try:
        result = subprocess.run(probe, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "status": "infrastructure_error", "missing": [], "capabilities": {}, "probe": probe, "summary": f"sandbox namespace capability probe failed: {exc}"}
    capabilities = {"network_namespace": result.returncode == 0, "pid_namespace": result.returncode == 0, "mount_namespace": result.returncode == 0, "capability_drop": result.returncode == 0}
    return {"success": result.returncode == 0, "status": "passed" if result.returncode == 0 else "infrastructure_error", "missing": [], "capabilities": capabilities, "probe": probe, "exit_code": result.returncode, "stdout": (result.stdout or "")[-2000:], "stderr": (result.stderr or "")[-2000:], "summary": "candidate sandbox tools and namespace capabilities are available" if result.returncode == 0 else "candidate sandbox namespace capabilities are unavailable"}


def _chown(path: Path, uid: int, gid: int) -> None:
    if os.name != "nt":
        os.chown(path, uid, gid, follow_symlinks=False)


def _make_scratch() -> Path:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True, mode=0o711)
    SANDBOX_ROOT.chmod(0o711)
    scratch = Path(tempfile.mkdtemp(prefix="candidate-", dir=SANDBOX_ROOT))
    _chown(scratch, CANDIDATE_UID, CANDIDATE_GID)
    scratch.chmod(0o700)
    for name in ("home", "tmp", "cache"):
        child = scratch / name
        child.mkdir(mode=0o700)
        _chown(child, CANDIDATE_UID, CANDIDATE_GID)
    return scratch


def _chown_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        _chown(current_path, CANDIDATE_UID, CANDIDATE_GID)
        current_path.chmod(0o700)
        for name in directories:
            path = current_path / name
            _chown(path, CANDIDATE_UID, CANDIDATE_GID)
            path.chmod(0o700)
        for name in files:
            path = current_path / name
            _chown(path, CANDIDATE_UID, CANDIDATE_GID)
            if not path.is_symlink():
                mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod((mode & 0o111) | 0o600)


def prepare_candidate_workspace(repo: Path) -> tuple[Path, Path]:
    repo = repo.resolve()
    if not repo.is_dir() or repo.is_symlink():
        raise OSError(f"candidate repository is missing or unsafe: {repo}")
    scratch = _make_scratch()
    workspace = scratch / "repo"
    # The migration tests construct the immutable source baseline with
    # ``git archive``.  Keep repository metadata in the private copy so that
    # lookup remains available; the repository is still bind-mounted
    # read-only before the candidate UID is entered, and evaluator markers
    # remain excluded.
    excluded = shutil.ignore_patterns(".base_commit", ".base_tree_hash", ".source_base_commit", ".git-fsck-unreachable", ".git-object-inventory", ".git-reflog")
    try:
        shutil.copytree(repo, workspace, symlinks=True, ignore=excluded)
        _chown_tree(workspace)
    except (OSError, shutil.Error):
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return scratch, workspace


def prepare_readonly_assets(source: Path) -> tuple[Path, Path]:
    if not source.is_dir() or source.is_symlink():
        raise OSError(f"evaluator assets are missing or unsafe: {source}")
    holder = Path(tempfile.mkdtemp(prefix="evaluator-assets-", dir=SANDBOX_ROOT))
    asset_root = holder / "assets"
    try:
        shutil.copytree(source, asset_root, symlinks=False)
        for path in (holder, asset_root, *asset_root.rglob("*")):
            if path.is_symlink():
                raise OSError(f"evaluator asset symlink is not allowed: {path}")
            _chown(path, 0, 0)
            if path.is_dir():
                path.chmod(0o555)
            else:
                mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod((mode & 0o111) | 0o444)
    except (OSError, shutil.Error):
        shutil.rmtree(holder, ignore_errors=True)
        raise
    return holder, asset_root


def prepare_writable_path(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chown_tree(path)


def cleanup_candidate_workspace(scratch: Path) -> None:
    shutil.rmtree(scratch, ignore_errors=True)


def cleanup_readonly_assets(holder: Path) -> None:
    shutil.rmtree(holder, ignore_errors=True)


def is_namespace_error(stderr: str, returncode: int | None = None) -> bool:
    # namespace_prefix uses 125 as a private setup-failure sentinel.  Keep
    # stderr markers for diagnostics from unshare/setpriv/mount themselves,
    # but do not depend on a particular localized diagnostic string.
    if returncode == 125:
        return True
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in _NAMESPACE_MARKERS)


def run_candidate_command(repo: Path, args: list[str], *, timeout: float = 60, stdin: str | None = None, env_extra: Mapping[str, object] | None = None) -> dict[str, Any]:
    tools = check_sandbox_tools()
    command = sandbox_command(args)
    if tools["success"] is not True:
        return {"status": "infrastructure_error", "success": None, "command": command, "exit_code": None, "timed_out": False, "stdout": "", "stderr": "", "error": tools["summary"], "sandbox": tools}
    try:
        scratch, workspace = prepare_candidate_workspace(repo)
    except OSError as exc:
        return {"status": "infrastructure_error", "success": None, "command": command, "exit_code": None, "timed_out": False, "stdout": "", "stderr": "", "error": f"unable to create candidate scratch: {exc}"}
    try:
        started = time.monotonic()
        try:
            # Do not enter the namespace with cwd already pointing into the
            # repository.  Linux preserves that directory reference across
            # mount changes, which would otherwise retain a writable view.
            result = subprocess.run(command, cwd=scratch, input=stdin, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout, check=False, env=candidate_environment(scratch, env_extra, readonly_root=workspace))
        except subprocess.TimeoutExpired as exc:
            return {"status": "candidate_timeout", "success": False, "command": command, "exit_code": -1, "timed_out": True, "duration_ms": int((time.monotonic() - started) * 1000), "stdout": "", "stderr": str(exc)[-16000:], "error": f"candidate process timed out: {exc}"}
        except OSError as exc:
            return {"status": "infrastructure_error", "success": None, "command": command, "exit_code": None, "timed_out": False, "duration_ms": int((time.monotonic() - started) * 1000), "stdout": "", "stderr": "", "error": f"unable to start candidate sandbox: {exc}"}
        stdout, stderr = (result.stdout or "")[-16000:], (result.stderr or "")[-16000:]
        namespace_error = result.returncode != 0 and is_namespace_error(stderr, result.returncode)
        return {"status": "passed" if result.returncode == 0 else "infrastructure_error" if namespace_error else "candidate_failure", "success": result.returncode == 0 if not namespace_error else None, "command": command, "exit_code": result.returncode, "timed_out": False, "duration_ms": int((time.monotonic() - started) * 1000), "stdout": stdout, "stderr": stderr, "workspace": {"kind": "independent_writable_copy", "git_metadata_included": False, "dedicated_home_tmp_cache": True}}
    finally:
        cleanup_candidate_workspace(scratch)


__all__ = ["CANDIDATE_GID", "CANDIDATE_UID", "SANDBOX_ROOT", "SANDBOX_TOOLS", "candidate_environment", "capability_drop_prefix", "check_sandbox_tools", "cleanup_candidate_workspace", "cleanup_readonly_assets", "is_namespace_error", "namespace_prefix", "network_isolated_command", "prepare_candidate_workspace", "prepare_readonly_assets", "prepare_writable_path", "run_candidate_command", "sandbox_command"]
