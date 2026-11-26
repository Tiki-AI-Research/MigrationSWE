"""Permission handoff for private inputs and unprivileged candidate copies."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
from pathlib import Path, PurePosixPath


def _tree_entries(root: Path, excluded: tuple[str, ...] = ()) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("workspace root must be a real directory")
    entries = [root]
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in sorted(directory.iterdir()):
            if child.name in excluded:
                continue
            mode = child.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                raise ValueError("workspace contains an unsupported file type")
            entries.append(child)
            if stat.S_ISDIR(mode):
                pending.append(child)
    return entries


def normalize_workspace_permissions(
    root: Path, *, uid: int | None = None, excluded: tuple[str, ...] = ()
) -> None:
    entries = _tree_entries(root, excluded)
    for path in entries:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue
        if uid is not None:
            os.chown(path, uid, uid, follow_symlinks=False)
        path.chmod(0o755 if stat.S_ISDIR(mode) or mode & 0o111 else 0o644)


def prepare_readonly_repository(source: Path, destination: Path) -> None:
    entries = _tree_entries(source)
    source_root = source.resolve()
    for path in entries:
        if path.is_symlink():
            target = path.readlink()
            if target.is_absolute() or not path.resolve().is_relative_to(source_root):
                raise ValueError("repository symlink escapes the private input")
    shutil.copytree(source, destination, symlinks=True)
    normalize_workspace_permissions(destination)


def workspace_target(root: Path, relative: str, *, allow_leaf_link: bool = False) -> Path:
    parts = _target_parts(relative)
    if root.is_symlink():
        raise ValueError("workspace root must not be a symlink")
    target = root
    for index, part in enumerate(parts):
        target = target / part
        if target.is_symlink() and not (allow_leaf_link and index == len(parts) - 1):
            raise ValueError("workspace target traverses a symlink")
    return target


def _target_parts(relative: str) -> tuple[str, ...]:
    parts = PurePosixPath(relative).parts
    if (
        not parts or PurePosixPath(relative).is_absolute()
        or "\\" in relative or any(part in {".", ".."} for part in relative.split("/"))
        or PurePosixPath(relative).as_posix() != relative
    ):
        raise ValueError("workspace target must be a safe relative path")
    return parts


def tree_permission_script(target: str) -> str:
    quoted = shlex.quote(target)
    return (
        f"if [ ! -L {quoted} ]; then "
        f"find {quoted} -type d -exec chmod u=rwx,go=rx,a-s,a-t {{}} +; "
        f"find {quoted} -type f ! -perm /111 -exec chmod 0644 {{}} +; "
        f"find {quoted} -type f -perm /111 -exec chmod 0755 {{}} +; fi"
    )


def target_guard_script(relative: str, *, allow_leaf_link: bool = False) -> str:
    parts = _target_parts(relative)
    commands = []
    for length in range(1, len(parts) + (0 if allow_leaf_link else 1)):
        target = shlex.quote("/workspace/repo/" + "/".join(parts[:length]))
        commands.append(
            f"if [ -L {target} ]; then "
            "echo 'workspace target traverses a symlink' >&2; exit 1; fi"
        )
    return "; ".join(commands) or ":"
