"""Detect active source-runtime dependencies in target manifests.

The detector answers one question: does the migrated project still *declare* the
source framework as a direct dependency?  It deliberately inspects only the
dependency-declaring fields of the project's own manifests -- never free text
such as ``description``, ``keywords`` or script bodies -- and it only inspects
manifests that the migration contract actually assigns dependency validation to.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


_MANIFESTS = {
    "package.json", "Cargo.toml", "go.mod",
    "requirements.txt", "pyproject.toml", "pom.xml", "build.gradle",
}
# Lockfiles (``Cargo.lock``, ``package-lock.json``, ``go.sum`` ...) describe the
# full transitive closure, so a source framework can legitimately remain there
# because an unrelated dev/benchmark dependency pulls it in (for example
# ``clap`` via the ``criterion`` benchmark crate). Only the project's own
# direct manifests declare an *active* source-runtime dependency.

_GENERIC_LANGUAGE_NAMES = {
    "go", "rust", "java", "python", "typescript", "javascript",
}

_PACKAGE_JSON_FIELDS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
    "bundledDependencies",
    "bundleDependencies",
    "resolutions",
)

_CARGO_SECTIONS = (
    "dependencies",
    "dev-dependencies",
    "build-dependencies",
)

_GRADLE_COORDINATE = re.compile(
    r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation|"
    r"testRuntimeOnly|annotationProcessor|kapt)\s*\(?\s*['\"]([^'\"]+)['\"]"
)
_POM_ARTIFACT = re.compile(r"<(?:artifactId|groupId)>\s*([^<\s]+)\s*</")
_GO_REQUIRE = re.compile(r"^\s*require\s+(?:\(\s*)?(.+)$")


@dataclass(frozen=True)
class DependencyHit:
    path: str
    line: int
    package: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": "source_runtime_dependency",
            "capability": "import",
            "path": self.path,
            "line": self.line,
            "package": self.package,
        }


def _source_identity(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve the source identity across both contract projections.

    Older bundles nest the source fields under ``migration.source``; the public
    projection carries the same fields at the top level as
    ``source_technology``.  Reading both keeps the dependency detector active
    for either layout instead of silently degrading to an empty name.
    """

    migration = config.get("migration")
    if isinstance(migration, Mapping):
        source = migration.get("source")
        if isinstance(source, Mapping) and source:
            return source
    technology = config.get("source_technology")
    return technology if isinstance(technology, Mapping) else {}


def _target_identity(config: Mapping[str, Any]) -> Mapping[str, Any]:
    migration = config.get("migration")
    if isinstance(migration, Mapping):
        target = migration.get("target")
        if isinstance(target, Mapping) and target:
            return target
    technology = config.get("target_technology")
    return technology if isinstance(technology, Mapping) else {}


def _source_name(config: Mapping[str, Any]) -> str:
    """Return the casefolded source framework name, or ``""`` when not usable.

    Generic language-only sources (``Go`` -> Go, ``Rust`` -> Rust) are not
    runtime dependencies of a manifest, so they are ignored.
    """

    source = _source_identity(config)
    name = str(source.get("name") or "").strip().casefold()
    language = str(source.get("language_name") or "").strip().casefold()
    if not name:
        return ""
    if name in _GENERIC_LANGUAGE_NAMES and language == name:
        return ""
    return name


def _source_and_target(config: Mapping[str, Any]) -> tuple[str, str, str]:
    migration = config.get("migration")
    migration = migration if isinstance(migration, Mapping) else {}
    source_name = _source_name(config)
    target = _target_identity(config)
    target_name = str(target.get("name") or target.get("language_name") or "").strip().casefold()
    category = str(migration.get("category") or "").strip()
    return source_name, target_name, category


def _dependency_manifests(config: Mapping[str, Any]) -> set[str] | None:
    """Return the manifest paths the contract assigns dependency validation to.

    ``None`` means the contract carries no migration units, so the detector
    falls back to scanning every direct manifest (used by unit tests and legacy
    contracts).  An empty set means units exist but none request dependency
    validation, so nothing is scanned.
    """

    raw = config.get("migration_units")
    if not isinstance(raw, list):
        return None
    units = [row for row in raw if isinstance(row, Mapping)]
    if not units:
        return None
    in_scope: set[str] = set()
    for unit in units:
        methods = {
            str(item).strip().casefold()
            for item in (unit.get("validation_methods") or ())
        }
        if str(unit.get("kind") or "").strip().casefold() != "dependency" and (
            "dependency" not in methods
        ):
            continue
        for locator in (
            *(unit.get("source_locators") or ()),
            *(unit.get("target_locators") or ()),
        ):
            if not isinstance(locator, Mapping):
                continue
            raw_path = str(locator.get("path") or "").strip()
            if not raw_path:
                continue
            # Normalize "./x" to "x" without ``str.lstrip`` (which would also
            # strip a legitimate leading "." from names like ".github").
            candidate = PurePosixPath(raw_path)
            if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
                continue
            in_scope.add(candidate.as_posix())
    return in_scope


def _in_scope(relative: str, scope: set[str] | None) -> bool:
    if scope is None:
        return True
    if relative in scope:
        return True
    for entry in scope:
        prefix = entry.rstrip("/")
        if prefix and relative.startswith(prefix + "/"):
            return True
    return False


def _manifest_files(root: Path, scope: set[str] | None) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name not in _MANIFESTS:
            continue
        relative = path.relative_to(root)
        if any(part in {".git", "node_modules", "target", "dist", "build", "vendor"} for part in relative.parts):
            continue
        if not _in_scope(relative.as_posix(), scope):
            continue
        files.append(path)
    return files


def _package_json_dependencies(text: str) -> set[str]:
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return set()
    if not isinstance(payload, Mapping):
        return set()
    names: set[str] = set()
    for field in _PACKAGE_JSON_FIELDS:
        table = payload.get(field)
        if isinstance(table, Mapping):
            names.update(str(key) for key in table)
        elif isinstance(table, list):
            names.update(str(item) for item in table)
    overrides = payload.get("overrides")
    if isinstance(overrides, Mapping):
        names.update(str(key) for key in overrides)
        for value in overrides.values():
            if isinstance(value, Mapping):
                names.update(str(key) for key in value)
    return {name.casefold() for name in names if name.strip()}


def _cargo_dependencies(text: str) -> set[str]:
    try:
        payload = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return set()
    names: set[str] = set()

    def collect(table: Any) -> None:
        if isinstance(table, Mapping):
            names.update(str(key) for key in table)

    for section in _CARGO_SECTIONS:
        collect(payload.get(section))
    workspace = payload.get("workspace")
    if isinstance(workspace, Mapping):
        collect(workspace.get("dependencies"))
    target = payload.get("target")
    if isinstance(target, Mapping):
        for entry in target.values():
            if isinstance(entry, Mapping):
                for section in _CARGO_SECTIONS:
                    collect(entry.get(section))
    return {name.casefold() for name in names if name.strip()}


def _pyproject_dependencies(text: str) -> set[str]:
    try:
        payload = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return set()
    names: set[str] = set()

    def add_pep508(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, str):
                continue
            name = re.split(r"[<>=!~;\[\s]", row.strip(), maxsplit=1)[0]
            if name:
                names.add(name)

    project = payload.get("project")
    if isinstance(project, Mapping):
        add_pep508(project.get("dependencies"))
        optional = project.get("optional-dependencies")
        if isinstance(optional, Mapping):
            for group in optional.values():
                add_pep508(group)
    tool = payload.get("tool")
    if isinstance(tool, Mapping):
        poetry = tool.get("poetry")
        if isinstance(poetry, Mapping):
            for field in ("dependencies", "dev-dependencies"):
                table = poetry.get(field)
                if isinstance(table, Mapping):
                    names.update(str(key) for key in table)
    return {name.casefold() for name in names if name.strip()}


def _requirements_dependencies(text: str) -> set[str]:
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[\s]", line, maxsplit=1)[0]
        if name:
            names.add(name)
    return {name.casefold() for name in names if name.strip()}


def _go_dependencies(text: str) -> set[str]:
    names: set[str] = set()
    in_block = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("require ("):
            in_block = True
            continue
        if in_block and stripped == ")":
            in_block = False
            continue
        if in_block:
            parts = stripped.split()
            if parts:
                names.add(parts[0])
            continue
        match = _GO_REQUIRE.match(raw)
        if match and not match.group(1).strip().startswith("("):
            parts = match.group(1).split()
            if parts:
                names.add(parts[0])
    return {name.casefold() for name in names if name.strip()}


def _pom_dependencies(text: str) -> set[str]:
    return {
        match.group(1).casefold()
        for match in _POM_ARTIFACT.finditer(text)
        if match.group(1).strip()
    }


def _gradle_dependencies(text: str) -> set[str]:
    names: set[str] = set()
    for match in _GRADLE_COORDINATE.finditer(text):
        coordinate = match.group(1).strip()
        if not coordinate:
            continue
        names.add(coordinate.split(":", 1)[0].strip())
    return {name.casefold() for name in names if name}


def _declared_dependencies(path: Path, text: str) -> set[str]:
    name = path.name
    if name == "package.json":
        return _package_json_dependencies(text)
    if name == "Cargo.toml":
        return _cargo_dependencies(text)
    if name == "pyproject.toml":
        return _pyproject_dependencies(text)
    if name == "requirements.txt":
        return _requirements_dependencies(text)
    if name == "go.mod":
        return _go_dependencies(text)
    if name == "pom.xml":
        return _pom_dependencies(text)
    if name == "build.gradle":
        return _gradle_dependencies(text)
    return set()


def _dependency_matches(declared: str, source_name: str) -> bool:
    if declared == source_name:
        return True
    # Go module paths embed the project name as a path segment.
    if "/" in declared:
        return source_name in declared.split("/")
    return False


def _line_of(text: str, token: str) -> int:
    match = re.search(re.escape(token), text, re.IGNORECASE)
    if match is None:
        return 0
    return text.count("\n", 0, match.start()) + 1


def detect_source_runtime_dependency(
    root: Path, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    source_name, target_name, category = _source_and_target(config)
    if not source_name:
        return []
    # A version upgrade keeps the same framework; its own manifest necessarily
    # still declares the framework, so "residual source framework" is vacuous.
    if category == "version_upgrade" or (target_name and target_name == source_name):
        return []
    scope = _dependency_manifests(config)
    hits: list[dict[str, Any]] = []
    for path in _manifest_files(root, scope):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        declared = _declared_dependencies(path, text)
        if not any(_dependency_matches(item, source_name) for item in declared):
            continue
        relative = path.relative_to(root).as_posix()
        hits.append(
            DependencyHit(
                path=relative,
                line=_line_of(text, source_name),
                package=source_name,
            ).as_dict()
        )
    return hits


__all__ = ["detect_source_runtime_dependency", "DependencyHit", "_declared_dependencies"]
