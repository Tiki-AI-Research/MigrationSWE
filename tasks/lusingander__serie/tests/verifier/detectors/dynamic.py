"""Detect dynamic loading of source-runtime code or plugins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import (
    process_token_present,
    strip_detector_comments,
    target_source_suffixes,
    token_present,
)
from .subprocess import _candidate_files, _is_test_file, _line_of, source_runtime_tokens


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
# Go's ``plugin`` package *is* the dynamic-loading mechanism, and the migration
# contract forbids dynamic loading outright.  Any use of it from target-language
# production code is therefore a fail-closed source-link signal even when the
# loaded artifact is renamed or the package is imported under an alias
# (``import pl "plugin"; pl.Open("anything")``), which hides the sink name.
_GO_PROHIBITED_LOADERS = ("plugin.Open(",)
_GO_PLUGIN_ALIAS = re.compile(r"import\s+([A-Za-z_][A-Za-z0-9_]*)\s+\"plugin\"")


def _prohibited_loader_hits(
    path: Path, relative: str, clean: str, config: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if ".go" not in target_source_suffixes(config) or _is_test_file(relative):
        return []
    sinks = list(_GO_PROHIBITED_LOADERS)
    sinks.extend(f"{match.group(1)}.Open(" for match in _GO_PLUGIN_ALIAS.finditer(clean))
    found: list[dict[str, Any]] = []
    lowered = clean.casefold()
    for sink in sinks:
        start = lowered.find(sink.casefold())
        while start >= 0:
            found.append(
                DynamicHit(
                    path=relative,
                    line=_line_of(clean, start),
                    sink=sink,
                    token="<prohibited-loader>",
                    evidence=clean[max(0, start - 40) : start + 120][:200],
                ).as_dict()
            )
            start = lowered.find(sink.casefold(), start + 1)
    return found


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
        hits.extend(
            _prohibited_loader_hits(
                path, path.relative_to(root).as_posix(), clean, config
            )
        )
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
