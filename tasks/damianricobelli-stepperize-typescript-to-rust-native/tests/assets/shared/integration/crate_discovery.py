#!/usr/bin/env python3
"""Locate the migrated native Rust crate inside the candidate repository.

The migration contract requires "a native Rust library crate inside this
repository"; it does not pin a directory.  The evaluator's behavioural probes,
however, must inject integration tests into whatever crate the candidate
produced, so they need a deterministic way to find it.  This helper centralises
that discovery:

* prefer a package literally named ``stepperize-core``;
* otherwise fall back to any non-virtual package that ships a library
  (``[lib]`` table or ``src/lib.rs``);
* exclude build/vendor trees.

It reports the crate directory, its repository-relative path and the Rust
library import name (``[lib].name`` with ``-`` normalised to ``_``), so callers
can both install tests and rewrite the ``use stepperize_core::…`` import.
"""

from __future__ import annotations

import json
import os
import sys

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "target",
        "dist",
        "build",
        "vendor",
        ".gomodcache",
        ".gocache",
        ".pytest_cache",
        "__pycache__",
    }
)

DEFAULT_PACKAGE_NAME = "stepperize-core"


def _load_manifest(path: str):
    if tomllib is None:
        return None
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return None


def _candidate_manifests(repo: str):
    found = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in _EXCLUDED_DIRS]
        if "Cargo.toml" in files:
            found.append(os.path.join(root, "Cargo.toml"))
    return sorted(found)


def discover_crate(repo: str, package_name: str = DEFAULT_PACKAGE_NAME):
    """Return crate metadata, or ``None`` when no Rust library crate exists."""

    repo = os.path.abspath(repo)
    preferred = None
    fallback = None
    for manifest in _candidate_manifests(repo):
        data = _load_manifest(manifest)
        if not isinstance(data, dict):
            continue
        package = data.get("package")
        if not isinstance(package, dict):
            continue  # virtual workspace manifest: no package of its own
        name = str(package.get("name") or "").strip()
        if not name:
            continue
        crate_dir = os.path.dirname(manifest)
        lib = data.get("lib") if isinstance(data.get("lib"), dict) else {}
        lib_name = str(lib.get("name") or name).strip().replace("-", "_")
        source = os.path.abspath(os.path.join(crate_dir, lib.get("path", "src/lib.rs")))
        if not os.path.isfile(source) or os.path.commonpath([repo, source]) != repo:
            continue
        entry = {
            "dir": crate_dir,
            "rel": os.path.relpath(crate_dir, repo).replace(os.sep, "/"),
            "package": name,
            "lib": lib_name,
            "source": source,
            "manifest": manifest,
        }
        if name == package_name:
            preferred = entry
            break
        if fallback is None:
            fallback = entry
    return preferred or fallback


def cargo_command(repo, args):
    """Run the discovered library's real Cargo target, regardless of layout."""
    info = discover_crate(repo)
    if info is None:
        raise ValueError("no native Rust library crate found")
    return ["cargo", args[0], "--manifest-path", info["manifest"], *args[1:]]


def main(argv):
    repo = os.environ.get("MIGRATION_REPO") or os.getcwd()
    args = list(argv)
    cargo_args = None
    if "--cargo" in args:
        split = args.index("--cargo")
        cargo_args, args = args[split + 1:], args[:split]
    for index, arg in enumerate(args):
        if arg == "--repo" and index + 1 < len(args):
            repo = args[index + 1]
        elif arg == "--package" and index + 1 < len(args):
            package = args[index + 1]
    package = locals().get("package", DEFAULT_PACKAGE_NAME)
    found = discover_crate(repo, package)
    if found is None:
        sys.stderr.write("no native Rust library crate found under %s\n" % repo)
        return 1
    if cargo_args:
        os.execvp("cargo", cargo_command(repo, cargo_args))
    print(json.dumps(found, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
