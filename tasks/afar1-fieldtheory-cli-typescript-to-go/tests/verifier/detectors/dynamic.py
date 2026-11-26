"""Detect dynamic loading of source-runtime code or plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import (
    process_token_present,
    strip_detector_comments,
    token_present,
)
from .subprocess import _candidate_files, _line_of, source_runtime_tokens


_SINKS = (
    "importlib",
    "import_module(",
    "__import__(",
    "module.constructor._load",
    "process.mainModule",
    "URLClassLoader",
    "Class.forName",
    "defineClass",
    "ScriptEngineManager",
    "plugin.Open(",
    "//go:linkname",
)
_WINDOW = 240


@dataclass(frozen=True)
class DynamicHit:
    path: str
    line: int
    sink: str
    token: str
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": "dynamic_load",
            "capability": "callgraph",
            "path": self.path,
            "line": self.line,
            "sink": self.sink,
            "token": self.token,
            "evidence": self.evidence,
        }


def detect_source_runtime_dynamic_load(
    root: Path,
    config: Mapping[str, Any],
    *,
    tokens: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    wanted = {str(item).casefold() for item in (tokens or source_runtime_tokens(config))}
    wanted.discard("")
    if not wanted:
        return []
    hits: list[dict[str, Any]] = []
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Comments cannot load code; blank them so a comment mentioning a sink
        # next to a runtime token is not reported as dynamic loading.  Loader
        # directives (``//go:linkname``, ``#cgo``) are preserved.
        clean = strip_detector_comments(text, path.suffix)
        lowered = clean.casefold()
        for sink in _SINKS:
            start = lowered.find(sink.casefold())
            while start >= 0:
                window = clean[start:min(len(clean), start + _WINDOW)]
                matched = next(
                    (
                        token for token in wanted
                        if process_token_present(window, token)
                        or token_present(window, f"lib{token}")
                    ),
                    None,
                )
                if matched is not None:
                    hits.append(
                        DynamicHit(
                            path=path.relative_to(root).as_posix(),
                            line=_line_of(clean, start),
                            sink=sink,
                            token=matched,
                            evidence=window[:200],
                        ).as_dict()
                    )
                start = lowered.find(sink.casefold(), start + 1)
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = (hit["path"], hit["line"], hit["sink"], hit["token"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)
    return unique


__all__ = ["detect_source_runtime_dynamic_load"]
