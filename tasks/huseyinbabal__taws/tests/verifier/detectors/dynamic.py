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
from .subprocess import (
    _candidate_files,
    _go_package_bindings,
    _line_of,
    _value_mentions,
    source_runtime_tokens,
)


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

# Dynamic loading of a foreign artifact is a delegation mechanism on its own, so
# the ``//go:linkname`` linker directive is fail-closed (a migrated service has
# no legitimate reason to bind an unexported runtime symbol).  The bare
# ``plugin.Open`` reference covers value forms (``op := plugin.Open``,
# ``reflect.ValueOf(plugin.Open)``, a helper argument) that the call-shaped sink
# above misses because no trailing call paren is present at the reference site.
_FAIL_CLOSED_SINKS = ("//go:linkname",)
_FAIL_CLOSED_VALUE = ("plugin.Open",)
_WINDOW = 240


def _plugin_value_targets(text: str) -> set[str]:
    """``plugin.Open`` value references under every import spelling.

    The canonical ``plugin.Open`` plus an explicit rename (``import p "plugin"``
    then ``p.Open``).  A value reference hands the loader around, so a
    call-shaped sink never sees the loaded artifact name; the bare reference is
    fail-closed on its own.  Dot imports are intentionally not expanded because a
    bare ``Open`` is too generic to fail-closed safely.
    """

    targets: set[str] = set(_FAIL_CLOSED_VALUE)
    names, _ = _go_package_bindings(text, "plugin")
    targets.update(f"{alias}.Open" for alias in names)
    return targets


def _plugin_gated_calls(text: str) -> set[str]:
    """``plugin.Open`` call forms under every import spelling (token-gated)."""

    names, _ = _go_package_bindings(text, "plugin")
    return {f"{alias}.Open(" for alias in names}


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
        # Bare (non-call) references to a dynamic-load primitive are fail-closed
        # on their own: ``op := plugin.Open`` hands the loader around, so a
        # call-shaped sink never sees the artifact name.
        for name, offset in _value_mentions(clean, _plugin_value_targets(clean)):
            window = clean[offset:min(len(clean), offset + _WINDOW)]
            hits.append(
                DynamicHit(
                    path=path.relative_to(root).as_posix(),
                    line=_line_of(clean, offset),
                    sink=name,
                    token=name,
                    evidence=window[:200],
                ).as_dict()
            )
        ordered_sinks: list[str] = []
        seen_sinks: set[str] = set()
        for sink in (*_SINKS, *_plugin_gated_calls(clean), *_FAIL_CLOSED_SINKS):
            if sink in seen_sinks:
                continue
            seen_sinks.add(sink)
            ordered_sinks.append(sink)
        for sink in ordered_sinks:
            start = lowered.find(sink.casefold())
            while start >= 0:
                window = clean[start:min(len(clean), start + _WINDOW)]
                if sink in _FAIL_CLOSED_SINKS:
                    matched: str | None = sink
                else:
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
