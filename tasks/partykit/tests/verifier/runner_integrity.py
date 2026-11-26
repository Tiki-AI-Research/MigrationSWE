"""Protect installed test tools across candidate execution phases."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Only tool caches are mutable; package code, executables and links are evidence.
CACHE_PATHS = {'.cache', '.vite', '.vite-temp'}


def dependency_inventory(candidate: Path) -> dict[str, str]:
    root = candidate / 'node_modules'
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root)
        if relative == Path('.'):
            dirs[:] = [name for name in dirs if name not in CACHE_PATHS]
        for name in dirs + files:
            path = Path(directory) / name
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[key] = 'link:' + os.readlink(path)
            elif path.is_file():
                result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def source_inventory(candidate: Path) -> dict[str, str]:
    """Record the adapter inputs, including the contents behind source links."""
    package = candidate / 'packages/hono-party'
    paths = [package / 'package.json', package / 'src']
    if (package / 'src').is_dir():
        paths.extend((package / 'src').rglob('*'))
    result = {}
    for path in paths:
        key = path.relative_to(candidate).as_posix()
        if path.is_symlink():
            result[key + ':link'] = os.readlink(path)
        if path.is_file():
            result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            result[key] = 'directory'
    return result


# Executed as root in each private mount namespace before dropping privileges.
# A read-only bind prevents transient replacement as well as persistent drift.
READONLY_DEPENDENCIES = r'''
modules = candidate / "node_modules"
if modules.is_dir():
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    subprocess.run(["mount", "--bind", str(modules), str(modules)], check=True)
    subprocess.run(["mount", "-o", "remount,bind,ro", str(modules)], check=True)
    for name in (".cache", ".vite", ".vite-temp"):
        cache = modules / name
        if cache.is_dir() and not cache.is_symlink():
            subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=1777,nosuid,nodev", "tmpfs", str(cache)], check=True)
'''

# Source inputs must remain the submitted inputs throughout build and harness
# execution. Build outputs and the existing dependency caches stay writable.
READONLY_SOURCE = r'''
package = candidate / "packages/hono-party"
inputs = [package / "src", package / "package.json"]
if (package / "src").is_dir():
    inputs.extend(path.resolve() for path in (package / "src").rglob("*") if path.is_symlink())
subprocess.run(["mount", "--make-rprivate", "/"], check=True)
for path in dict.fromkeys(inputs):
    if path.exists():
        subprocess.run(["mount", "--bind", str(path), str(path)], check=True)
        subprocess.run(["mount", "-o", "remount,bind,ro", str(path)], check=True)
'''
