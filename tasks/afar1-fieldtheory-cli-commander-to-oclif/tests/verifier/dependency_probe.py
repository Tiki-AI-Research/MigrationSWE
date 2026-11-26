"""Offline dependency resolution probe.

The hard gate derives ``dependency_resolved`` from this probe's own receipt,
never from a build or an import/version probe. Declared ranges are checked
against the lockfile and materialized packages, and lockfile sources must be
public. When the task explicitly makes lockfile updates optional, installed
packages satisfying every declared range can replace stale or missing lock
entries. That mode requires an actual installation and still validates every
available lockfile source. All checks are static and network-free.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

_MANIFEST_KEYS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)
_DEFAULT_REGISTRIES = ("registry.npmjs.org",)
_NUMERIC = re.compile(r"^\d+$")


def _version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    core = value.strip().split("+")[0].split("-")[0]
    parts = core.split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        if not _NUMERIC.match(part):
            return None
        numbers.append(int(part))
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def _upper_for_caret(version: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, patch = version
    if major:
        return major + 1, 0, 0
    if minor:
        return 0, minor + 1, 0
    return 0, 0, patch + 1


def _upper_for_tilde(version: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, _ = version
    return major, minor + 1, 0


def _wildcard(version: tuple[int, int, int], parts: Sequence[str]) -> bool:
    if not parts:
        return True
    for index, part in enumerate(parts[:3]):
        if part in {"x", "X", "*"}:
            return True
        if not _NUMERIC.match(part):
            return False
        if version[index] != int(part):
            return False
    return True


def _satisfies_one(version: tuple[int, int, int], token: str) -> bool | None:
    """Return ``True``/``False`` or ``None`` when the range is unsupported."""

    token = token.strip()
    if token in {"", "*", "latest"}:
        return True
    for prefix, compare in (
        (">=", lambda a, b: a >= b),
        ("<=", lambda a, b: a <= b),
        (">", lambda a, b: a > b),
        ("<", lambda a, b: a < b),
        ("=", lambda a, b: a == b),
    ):
        if token.startswith(prefix):
            floor = _version(token[len(prefix):].strip())
            return None if floor is None else compare(version, floor)
    if token.startswith("^"):
        floor = _version(token[1:])
        return None if floor is None else floor <= version < _upper_for_caret(floor)
    if token.startswith("~"):
        floor = _version(token[1:])
        return None if floor is None else floor <= version < _upper_for_tilde(floor)
    if any(marker in token for marker in ("x", "X", "*")):
        return _wildcard(version, token.split("."))
    exact = _version(token)
    return None if exact is None else version == exact


def _satisfies(version_text: object, spec: object) -> bool | None:
    version = _version(version_text)
    if version is None or not isinstance(spec, str):
        return None
    for alternative in spec.split("||"):
        alternative = alternative.strip()
        if " - " in alternative:
            low, _, high = alternative.partition(" - ")
            low_version = _version(low.strip())
            high_version = _version(high.strip())
            if low_version is None or high_version is None:
                return None
            if low_version <= version <= high_version:
                return True
            continue
        tokens = [token for token in alternative.split() if token]
        if not tokens:
            continue
        results = [_satisfies_one(version, token) for token in tokens]
        if any(result is None for result in results):
            return None
        if all(results):
            return True
    return False


def _entry(packages: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    value = packages.get(f"node_modules/{name}")
    return value if isinstance(value, Mapping) else None


def _installed_version(root: Path, name: str) -> tuple[bool, str | None]:
    path = root / "node_modules" / Path(*name.split("/")) / "package.json"
    if path.is_symlink() or not path.is_file():
        return False, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True, None
    if not isinstance(payload, Mapping):
        return True, None
    version = payload.get("version")
    return True, version if isinstance(version, str) else None


def _resolved_host(resolved: object) -> str | None:
    if not isinstance(resolved, str) or not resolved:
        return None
    if "://" not in resolved:
        # npm protocols such as ``file:``, ``link:`` or ``git+ssh:`` have no host.
        return ""
    remainder = resolved.split("://", 1)[1]
    return remainder.split("/", 1)[0].split(":", 1)[0].lower()


def probe_dependencies(
    root: Path,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the declared dependency graph offline and report on it.

    The report is ``{"ok": bool, "errors": [...], ...}``.  Any error means the
    hard gate must treat ``dependency_resolved`` as failed.
    """

    settings = policy if isinstance(policy, Mapping) else {}
    registries = settings.get("allowed_registries", _DEFAULT_REGISTRIES)
    allowed_hosts = {
        str(item).strip().lower()
        for item in (registries if isinstance(registries, Sequence) and not isinstance(registries, (str, bytes)) else _DEFAULT_REGISTRIES)
        if str(item).strip()
    }
    check_installed = bool(settings.get("verify_installed", True))
    require_lockfile = bool(settings.get("require_lockfile", True))
    errors: list[str] = []
    warnings: list[str] = []

    manifest_path = root / "package.json"
    lock_path = next(
        (
            candidate
            for candidate in (
                root / "package-lock.json",
                root / "npm-shrinkwrap.json",
            )
            if candidate.is_file() and not candidate.is_symlink()
        ),
        None,
    )
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {
            "ok": False,
            "errors": ["dependency-manifest-missing"],
            "warnings": [],
            "declared": 0,
            "locked": 0,
            "installed_checked": False,
        }
    if lock_path is None and require_lockfile:
        return {
            "ok": False,
            "errors": ["dependency-lockfile-missing"],
            "warnings": [],
            "declared": 0,
            "locked": 0,
            "installed_checked": False,
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "errors": [f"dependency-manifest-unreadable: {type(exc).__name__}"],
            "warnings": [],
            "declared": 0,
            "locked": 0,
            "installed_checked": False,
        }
    if not isinstance(manifest, Mapping) or not isinstance(lock, Mapping):
        return {
            "ok": False,
            "errors": ["dependency-manifest-unreadable: not-an-object"],
            "warnings": [],
            "declared": 0,
            "locked": 0,
            "installed_checked": False,
        }
    packages = lock.get("packages", {}) if lock_path is None else lock.get("packages")
    lock_version = lock.get("lockfileVersion")
    if not isinstance(packages, Mapping):
        return {
            "ok": False,
            "errors": ["dependency-lockfile-unusable: packages-missing"],
            "warnings": [],
            "declared": 0,
            "locked": 0,
            "installed_checked": False,
        }
    if lock_path is not None and lock_version not in {2, 3}:
        errors.append(f"dependency-lockfile-unsupported: {lock_version}")
    root_entry = packages.get("")
    root_entry = root_entry if isinstance(root_entry, Mapping) else {}
    declared = 0
    installed_checked = False
    node_modules = root / "node_modules"
    has_node_modules = node_modules.is_dir() and not node_modules.is_symlink()
    # Some tasks explicitly permit leaving the lockfile unchanged. In that
    # mode a stale/missing lock entry is acceptable only when the materialized
    # package proves that the declared version is actually available offline.
    # Never substitute a successful build or a missing installation for it.
    if not require_lockfile and (not check_installed or not has_node_modules):
        errors.append("dependency-installed-tree-required-with-optional-lockfile")

    for key in _MANIFEST_KEYS:
        values = manifest.get(key)
        if not isinstance(values, Mapping):
            continue
        locked_root = root_entry.get(key)
        locked_root = locked_root if isinstance(locked_root, Mapping) else {}
        for name, spec in sorted(values.items()):
            name = str(name)
            declared += 1
            present, installed = _installed_version(root, name) if check_installed and has_node_modules else (False, None)
            installed_verdict = _satisfies(installed, spec) if installed is not None else None
            installed_fallback = not require_lockfile and present and installed_verdict is True
            if name not in locked_root:
                (warnings if installed_fallback else errors).append(f"dependency-not-locked: {name}")
            elif locked_root.get(name) != spec:
                (warnings if installed_fallback else errors).append(
                    f"lockfile-manifest-mismatch: {name} ({spec!r} != {locked_root.get(name)!r})"
                )
            entry = _entry(packages, name)
            if entry is None:
                (warnings if installed_fallback else errors).append(f"dependency-unresolvable: {name}")
            locked_version = entry.get("version") if entry else None
            verdict = _satisfies(locked_version, spec) if entry else None
            if entry and verdict is None:
                (warnings if installed_fallback else errors).append(
                    f"dependency-range-unsupported: {name} ({spec!r} -> {locked_version!r})"
                )
            elif entry and verdict is False:
                (warnings if installed_fallback else errors).append(
                    f"dependency-version-conflict: {name} ({spec!r} !~ {locked_version!r})"
                )
            if check_installed and has_node_modules:
                installed_checked = True
                if not present:
                    # Report the resolved coordinate, not just the package
                    # name: the locked version is what a reviewer needs in
                    # order to locate the manifest/lock conflict.
                    errors.append(
                        f"dependency-not-installed: {name}"
                        f"@{locked_version if locked_version is not None else '?'}"
                        f" (declared {spec})"
                    )
                elif installed is not None:
                    if installed_verdict is False:
                        errors.append(
                            f"dependency-installed-conflict: {name} ({spec!r} !~ {installed!r})"
                        )
                    elif installed_verdict is None:
                        errors.append(f"dependency-installed-version-unsupported: {name} ({installed!r})")
                    elif installed != locked_version:
                        warnings.append(
                            f"dependency-installed-drift: {name} ({locked_version!r} != {installed!r})"
                        )
                else:
                    errors.append(f"dependency-installed-manifest-invalid: {name}")

    private_sources: list[str] = []
    for name, entry in sorted(packages.items()):
        if name == "" or not isinstance(entry, Mapping):
            continue
        resolved = entry.get("resolved")
        if not isinstance(resolved, str) or not resolved:
            continue
        host = _resolved_host(resolved)
        if host == "":
            errors.append(f"dependency-unreachable-source: {name} ({resolved.split(':', 1)[0]})")
            continue
        if host is not None and host not in allowed_hosts:
            private_sources.append(f"{name} ({host})")
    for row in private_sources:
        errors.append(f"dependency-private-source: {row}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "declared": declared,
        "locked": max(0, len(packages) - 1),
        "lockfile": lock_path.name if lock_path else None,
        "lockfile_version": lock_version,
        "allowed_registries": sorted(allowed_hosts),
        "installed_checked": installed_checked,
    }


__all__ = ["probe_dependencies"]
