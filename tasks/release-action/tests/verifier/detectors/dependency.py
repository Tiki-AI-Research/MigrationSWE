"""Detect active source-runtime dependencies in target manifests."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .runtime_identity import source_technology


_MANIFESTS = {
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "requirements.txt", "pyproject.toml", "pom.xml", "build.gradle",
}


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


def _source_names(config: Mapping[str, Any]) -> set[str]:
    source = source_technology(config)
    name = str(source.get("name") or "").strip().casefold()
    language = str(source.get("language_name") or "").strip().casefold()
    names = {name} if name else set()
    if language in {"go", "rust", "java", "python", "typescript", "javascript"} and name in {
        "go", "rust", "java", "python", "typescript", "javascript"
    }:
        names = set()
    return names


def _manifest_files(root: Path) -> list[Path]:
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
        files.append(path)
    return files


def detect_source_runtime_dependency(
    root: Path, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    names = _source_names(config)
    if not names:
        return []
    hits: list[dict[str, Any]] = []
    for path in _manifest_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lowered = text.casefold()
        for name in sorted(names):
            for match in re.finditer(rf"(?:^|[\s\"'/]){re.escape(name)}(?:$|[\s\"':=,])", lowered, re.M):
                hits.append(
                    DependencyHit(
                        path=path.relative_to(root).as_posix(),
                        line=text.count("\n", 0, match.start()) + 1,
                        package=name,
                    ).as_dict()
                )
    seen: set[tuple[str, int, str]] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = (hit["path"], hit["line"], hit["package"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


__all__ = ["detect_source_runtime_dependency"]
