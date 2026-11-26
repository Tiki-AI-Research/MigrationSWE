"""Independent audit of a candidate pnpm dependency graph.

The install command is executed by the evaluator, but this module does not
trust pnpm's exit status or a candidate-provided report.  It parses the
candidate manifest and lockfile, walks the installed virtual store, and checks
that the declared, locked and materialised graphs agree.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # pragma: no cover - availability is checked by the image build
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


_MANIFEST_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")
_MAX_CONTROL_FILE = 16 * 1024 * 1024


class DependencyGraphError(ValueError):
    """The candidate's dependency graph cannot be audited safely."""


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _tree_summary(path: Path) -> dict[str, Any]:
    """Hash one package directory without following any symlink."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DependencyGraphError(f"cannot read installed path {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise DependencyGraphError(
                f"cannot read installed symlink {path}: {exc}"
            ) from exc
        return {"type": "symlink", "target": target}
    if stat.S_ISREG(metadata.st_mode):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise DependencyGraphError(
                f"cannot hash installed file {path}: {exc}"
            ) from exc
        return {
            "type": "file",
            "size": metadata.st_size,
            "sha256": "sha256:" + digest.hexdigest(),
        }
    if stat.S_ISDIR(metadata.st_mode):
        entries: dict[str, Any] = {}
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise DependencyGraphError(
                f"cannot list installed directory {path}: {exc}"
            ) from exc
        for child in children:
            entries[child.name] = _tree_summary(child)
        return {"type": "directory", "entries": entries}
    return {"type": "other", "mode": metadata.st_mode}


def _tree_digest(path: Path) -> str:
    return _canonical_digest(_tree_summary(path))


def _read_control_file(root: Path, name: str) -> tuple[Path, bytes]:
    path = root / name
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DependencyGraphError(f"{name} is missing: {exc}") from exc
    if path.is_symlink():
        raise DependencyGraphError(f"{name} must not be a symlink")
    if not path.is_file():
        raise DependencyGraphError(f"{name} must be a regular file")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_CONTROL_FILE:
        raise DependencyGraphError(f"{name} has an unsafe size")
    return path, path.read_bytes()


def _json_object(raw: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DependencyGraphError(f"{name} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DependencyGraphError(f"{name} must contain a JSON object")
    return value


def _manifest_sections(manifest: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    for section in _MANIFEST_SECTIONS:
        raw = manifest.get(section, {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise DependencyGraphError(f"package.json {section} must be an object")
        values: dict[str, str] = {}
        for name, specifier in raw.items():
            if not isinstance(name, str) or not _safe_package_name(name):
                raise DependencyGraphError(f"package.json {section} has an unsafe name")
            if not isinstance(specifier, str) or not specifier.strip():
                raise DependencyGraphError(
                    f"package.json {section}.{name} must have a string specifier"
                )
            values[name] = specifier
        sections[section] = values
    return sections


def _safe_package_name(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    if "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    parts = name.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _split_key(key: str) -> tuple[str, str]:
    """Split ``name@version(peer...)`` into name and the complete version."""

    start = 1 if key.startswith("@") else 0
    index = key.find("@", start)
    if index < 0:
        raise DependencyGraphError(f"invalid package key: {key}")
    return key[:index], key[index + 1 :]


def _base_version(version: str) -> str:
    return version.split("(", 1)[0]


def _platform_compatible(package: Mapping[str, Any]) -> bool:
    machine = platform.machine().lower()
    cpu = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(
        machine, machine
    )
    allowed_os = package.get("os")
    allowed_cpu = package.get("cpu")
    allowed_libc = package.get("libc")
    if isinstance(allowed_os, Sequence) and not isinstance(allowed_os, (str, bytes)):
        if allowed_os and "linux" not in allowed_os:
            return False
    if isinstance(allowed_cpu, Sequence) and not isinstance(allowed_cpu, (str, bytes)):
        if allowed_cpu and cpu not in allowed_cpu:
            return False
    if isinstance(allowed_libc, Sequence) and not isinstance(allowed_libc, (str, bytes)):
        if allowed_libc and "glibc" not in allowed_libc:
            return False
    return True


def _safe_node_modules_path(candidate: Path, name: str) -> Path:
    if not _safe_package_name(name):
        raise DependencyGraphError(f"unsafe installed package name: {name!r}")
    path = candidate / "node_modules" / name / "package.json"
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DependencyGraphError(
            f"installed package {name} has no readable package.json"
        ) from exc
    node_modules = (candidate / "node_modules").resolve()
    if resolved != node_modules and node_modules not in resolved.parents:
        raise DependencyGraphError(
            f"installed package {name} resolves outside candidate node_modules"
        )
    return resolved


def _read_installed(package_json: Path) -> tuple[str, str]:
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DependencyGraphError(
            f"installed package metadata is unreadable: {package_json}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise DependencyGraphError(
            f"installed package metadata must be an object: {package_json}"
        )
    name = payload.get("name")
    version = payload.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise DependencyGraphError(
            f"installed package metadata lacks name/version: {package_json}"
        )
    return name, version


def _virtual_packages(candidate: Path) -> dict[tuple[str, str], list[Path]]:
    """Read every package materialised in pnpm's virtual store."""

    virtual = candidate / "node_modules" / ".pnpm"
    if virtual.is_symlink() or not virtual.is_dir():
        raise DependencyGraphError(
            "pnpm installed no virtual store; the candidate graph was not rebuilt"
        )
    packages: dict[tuple[str, str], list[Path]] = {}
    for entry in sorted(virtual.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        modules = entry / "node_modules"
        if not modules.is_dir():
            continue
        package_dirs: list[Path] = []
        for child in sorted(modules.iterdir()):
            if child.name.startswith("@"):
                package_dirs.extend(sorted(child.iterdir()))
            else:
                package_dirs.append(child)
        for package_dir in package_dirs:
            package_json = package_dir / "package.json"
            if not package_json.is_file():
                continue
            resolved = package_json.resolve()
            try:
                relative = resolved.relative_to(virtual.resolve())
            except ValueError as exc:
                raise DependencyGraphError(
                    f"virtual-store package escapes the candidate: {package_json}"
                ) from exc
            del relative
            name, version = _read_installed(resolved)
            packages.setdefault((name, version), []).append(resolved.parent)
    if not packages:
        raise DependencyGraphError("pnpm virtual store contains no installed packages")
    return packages


def _semver_tuple(value: str) -> tuple[int, ...] | None:
    core = value.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _version_matches(version: str, specifier: str) -> bool:
    """Small semver range check covering dependency peer declarations."""

    actual = _semver_tuple(version)
    if actual is None:
        return False
    alternatives = [part.strip() for part in specifier.split("||")]
    for alternative in alternatives:
        if not alternative or alternative in {"*", "latest"}:
            return True
        pieces = alternative.split()
        valid = True
        for piece in pieces:
            piece = piece.strip()
            if not piece or piece == "*":
                continue
            if piece.startswith("^"):
                target = _semver_tuple(piece[1:])
                if target is None:
                    valid = False
                    break
                upper = (
                    (target[0] + 1, 0, 0)
                    if target[0] > 0
                    else (0, target[1] + 1, 0)
                    if len(target) > 1 and target[1] > 0
                    else (0, 0, target[2] + 1)
                    if len(target) > 2
                    else (0, 0, 1)
                )
                valid = valid and actual >= target and actual < upper
            elif piece.startswith("~"):
                target = _semver_tuple(piece[1:])
                if target is None:
                    valid = False
                    break
                upper = (target[0], target[1] + 1, 0)
                valid = valid and actual >= target and actual < upper
            elif piece.startswith(">="):
                target = _semver_tuple(piece[2:])
                valid = valid and target is not None and actual >= target
            elif piece.startswith(">"):
                target = _semver_tuple(piece[1:])
                valid = valid and target is not None and actual > target
            elif piece.startswith("<="):
                target = _semver_tuple(piece[2:])
                valid = valid and target is not None and actual <= target
            elif piece.startswith("<"):
                target = _semver_tuple(piece[1:])
                valid = valid and target is not None and actual < target
            elif piece.startswith("="):
                target = _semver_tuple(piece[1:])
                valid = valid and target is not None and actual == target
            else:
                target = _semver_tuple(piece)
                if target is None:
                    valid = False
                    break
                valid = valid and actual == target
        if valid:
            return True
    return False


def _peer_failures(
    packages: Mapping[tuple[str, str], Sequence[Path]],
    package_metadata: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    failures: list[str] = []
    for (name, version), locations in sorted(packages.items()):
        metadata = package_metadata.get(f"{name}@{_base_version(version)}", {})
        peers = metadata.get("peerDependencies", {})
        if not isinstance(peers, Mapping):
            peers = {}
        peers_meta = metadata.get("peerDependenciesMeta", {})
        if not isinstance(peers_meta, Mapping):
            peers_meta = {}
        for peer_name, peer_range in peers.items():
            if not isinstance(peer_name, str) or not isinstance(peer_range, str):
                continue
            optional = isinstance(peers_meta.get(peer_name), Mapping) and bool(
                peers_meta[peer_name].get("optional")
            )
            observed: str | None = None
            for location in locations[:1]:
                peer_json = location.parent / peer_name / "package.json"
                if peer_json.is_file():
                    try:
                        observed_name, observed = _read_installed(peer_json)
                    except DependencyGraphError:
                        observed = None
                    else:
                        if observed_name != peer_name:
                            observed = None
            if observed is None:
                if optional:
                    continue
                failures.append(f"{name}@{version} is missing required peer {peer_name}")
            elif not _version_matches(observed, peer_range):
                failures.append(
                    f"{name}@{version} has peer {peer_name}@{observed}, "
                    f"which does not satisfy {peer_range}"
                )
    return failures


def _reachable_packages(
    importer: Mapping[str, Any],
    snapshots: Mapping[str, Any],
    package_metadata: Mapping[str, Mapping[str, Any]],
    direct: Mapping[str, str],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Return required and optional reachable package/version pairs."""

    required: set[tuple[str, str]] = set()
    optional: set[tuple[str, str]] = set()
    queue: list[tuple[str, str, bool]] = []
    for section in _MANIFEST_SECTIONS:
        entries = importer.get(section, {})
        if not isinstance(entries, Mapping):
            continue
        for name, value in entries.items():
            if not isinstance(name, str) or not isinstance(value, Mapping):
                continue
            version = value.get("version")
            if not isinstance(version, str):
                continue
            queue.append((name, version, section == "optionalDependencies"))
    while queue:
        name, version, is_optional = queue.pop()
        pair = (name, _base_version(version))
        if is_optional:
            optional.add(pair)
        else:
            required.add(pair)
        if pair in required and pair in optional:
            optional.discard(pair)
        key = f"{name}@{version}"
        snapshot = snapshots.get(key)
        if snapshot is None:
            snapshot = snapshots.get(f"{name}@{_base_version(version)}")
        if not isinstance(snapshot, Mapping):
            continue
        for section in ("dependencies", "optionalDependencies"):
            children = snapshot.get(section, {})
            if not isinstance(children, Mapping):
                continue
            for child_name, child_version in children.items():
                if not isinstance(child_name, str) or not isinstance(child_version, str):
                    continue
                queue.append(
                    (
                        child_name,
                        child_version,
                        is_optional or section == "optionalDependencies",
                    )
                )
    del direct
    return required, optional


def audit_dependency_graph(candidate_root: Path) -> dict[str, Any]:
    """Compare manifest, lockfile and installed pnpm graph."""

    result: dict[str, Any] = {
        "status": "FAIL",
        "source": "evaluator-dependency-graph-audit",
        "errors": [],
        "direct": {},
        "installed_count": 0,
        "digest": None,
    }
    if yaml is None:
        result["errors"].append("the evaluator has no YAML parser")
        return result
    candidate = candidate_root.resolve()
    try:
        manifest_path, manifest_raw = _read_control_file(candidate, "package.json")
        lock_path, lock_raw = _read_control_file(candidate, "pnpm-lock.yaml")
        manifest = _json_object(manifest_raw, "package.json")
        try:
            lock = yaml.safe_load(lock_raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise DependencyGraphError(f"pnpm-lock.yaml is not valid YAML: {exc}") from exc
        if not isinstance(lock, Mapping):
            raise DependencyGraphError("pnpm-lock.yaml must contain a mapping")
        importers = lock.get("importers")
        if not isinstance(importers, Mapping) or not isinstance(importers.get("."), Mapping):
            raise DependencyGraphError("pnpm-lock.yaml has no root importer")
        importer = importers["."]
        snapshots = lock.get("snapshots", {})
        packages = lock.get("packages", {})
        if not isinstance(snapshots, Mapping) or not isinstance(packages, Mapping):
            raise DependencyGraphError("pnpm-lock.yaml lacks packages/snapshots")
        manifest_sections = _manifest_sections(manifest)
        errors: list[str] = []
        direct: dict[str, dict[str, str]] = {}
        for section in _MANIFEST_SECTIONS:
            declared = manifest_sections[section]
            locked = importer.get(section, {})
            if not isinstance(locked, Mapping):
                errors.append(f"lockfile importer {section} is not an object")
                continue
            if set(declared) != set(locked):
                missing = sorted(set(declared) - set(locked))
                extra = sorted(set(locked) - set(declared))
                if missing:
                    errors.append(f"lockfile importer is missing {section}: {missing}")
                if extra:
                    errors.append(f"lockfile importer has undeclared {section}: {extra}")
            for name, specifier in sorted(declared.items()):
                entry = locked.get(name)
                if not isinstance(entry, Mapping):
                    continue
                if entry.get("specifier") != specifier:
                    errors.append(
                        f"{section}.{name} specifier {specifier!r} does not match "
                        f"lockfile {entry.get('specifier')!r}"
                    )
                version = entry.get("version")
                if not isinstance(version, str):
                    errors.append(f"{section}.{name} has no locked version")
                    continue
                base = _base_version(version)
                if f"{name}@{base}" not in packages:
                    errors.append(f"{name}@{base} is absent from lockfile packages")
                direct[name] = {"version": base, "section": section}
        result["direct"] = direct

        actual_direct: dict[str, dict[str, Any]] = {}
        for name, expected in sorted(direct.items()):
            try:
                package_json = _safe_node_modules_path(candidate, name)
                actual_name, actual_version = _read_installed(package_json)
            except DependencyGraphError as exc:
                errors.append(str(exc))
                continue
            actual_direct[name] = {
                "version": actual_version,
                "tree_sha256": _tree_digest(package_json.parent),
            }
            if actual_name != name:
                errors.append(
                    f"node_modules/{name} contains package {actual_name!r}"
                )
            if actual_version != expected["version"]:
                errors.append(
                    f"node_modules/{name} is {actual_version}, "
                    f"lockfile requires {expected['version']}"
                )

        installed = _virtual_packages(candidate)
        result["installed_count"] = len(installed)
        package_trees: dict[tuple[str, str], list[str]] = {}
        for pair, locations in installed.items():
            unique = sorted(
                {str(location) for location in locations}
            )
            package_trees[pair] = [
                _tree_digest(Path(location)) for location in unique
            ]
        for (name, version) in sorted(installed):
            if f"{name}@{version}" not in packages:
                errors.append(
                    f"installed {name}@{version} is absent from the lockfile"
                )

        required, optional = _reachable_packages(importer, snapshots, packages, direct)
        for name, version in sorted(required | optional):
            if (name, version) in installed:
                continue
            metadata = packages.get(f"{name}@{version}", {})
            compatible = isinstance(metadata, Mapping) and _platform_compatible(metadata)
            if (name, version) in optional or not compatible:
                continue
            errors.append(f"locked package {name}@{version} was not installed")

        peer_errors = _peer_failures(installed, packages)
        errors.extend(peer_errors)

        rows = []
        for (name, version), locations in sorted(installed.items()):
            metadata = packages.get(f"{name}@{version}", {})
            resolution = metadata.get("resolution", {}) if isinstance(metadata, Mapping) else {}
            integrity = (
                resolution.get("integrity")
                if isinstance(resolution, Mapping)
                else None
            )
            rows.append(
                {
                    "name": name,
                    "version": version,
                    "integrity": integrity,
                    "locations": len({str(location) for location in locations}),
                    "tree_sha256": package_trees[(name, version)],
                }
            )
        virtual = candidate / "node_modules" / ".pnpm"
        virtual_entries: list[dict[str, Any]] = []
        for entry in sorted(virtual.iterdir(), key=lambda item: item.name):
            if entry.is_symlink():
                virtual_entries.append(
                    {
                        "name": entry.name,
                        "type": "symlink",
                        "target": os.readlink(entry),
                    }
                )
            elif entry.is_dir():
                virtual_entries.append({"name": entry.name, "type": "directory"})
            elif entry.is_file():
                virtual_entries.append(
                    {
                        "name": entry.name,
                        "type": "file",
                        "sha256": _sha256(entry),
                    }
                )
            else:
                virtual_entries.append({"name": entry.name, "type": "other"})
        modules_path = candidate / "node_modules" / ".modules.yaml"
        if modules_path.is_symlink():
            raise DependencyGraphError("node_modules/.modules.yaml must not be a symlink")
        modules_sha256 = (
            _sha256(modules_path)
            if modules_path.is_file()
            else None
        )
        pnpm_lock = virtual / "lock.yaml"
        if pnpm_lock.is_symlink():
            raise DependencyGraphError("node_modules/.pnpm/lock.yaml must not be a symlink")
        pnpm_lock_sha256 = (
            _sha256(pnpm_lock)
            if pnpm_lock.is_file()
            else None
        )
        manifest_sha256 = _sha256_bytes(manifest_raw)
        lockfile_sha256 = _sha256_bytes(lock_raw)
        digest = _canonical_digest(
            {
                "manifest_sha256": manifest_sha256,
                "lockfile_sha256": lockfile_sha256,
                "modules_sha256": modules_sha256,
                "pnpm_lock_sha256": pnpm_lock_sha256,
                "virtual_entries": virtual_entries,
                "packages": rows,
                "direct": actual_direct,
            }
        )
        result.update(
            {
                "manifest_sha256": manifest_sha256,
                "lockfile_sha256": lockfile_sha256,
                "modules_sha256": modules_sha256,
                "pnpm_lock_sha256": pnpm_lock_sha256,
                "virtual_entries": virtual_entries,
                "direct_resolved": actual_direct,
                "digest": digest,
                "packages": rows,
            }
        )
        result["errors"] = errors
        result["status"] = "PASS" if not errors else "FAIL"
        return result
    except (DependencyGraphError, OSError) as exc:
        result["errors"] = [str(exc)]
        return result


__all__ = ["DependencyGraphError", "audit_dependency_graph"]
