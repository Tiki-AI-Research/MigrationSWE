"""Detect FFI/native-loading sinks that target the source implementation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import (
    native_runtime_signals,
    strip_detector_comments,
    token_present,
)
from .subprocess import _candidate_files, _line_of


# An FFI sink alone is not evidence of source fallback.  Target C/C++ code may
# legitimately use cgo, extern "C", dlopen or ctypes.  A hit requires a
# high-confidence source-runtime artifact in the same window.
_FFI_SINKS = (
    ("extern \"C\"", "target-abi"),
    ("libc::dlopen", "dynamic-loader"),
    ("libc::dlsym", "dynamic-loader"),
    ("libloading", "dynamic-loader"),
    ("#[link(", "source-link"),
    ("ctypes.", "dynamic-loader"),
    ("cffi", "dynamic-loader"),
    ("CDLL(", "dynamic-loader"),
    ("PyDLL(", "dynamic-loader"),
    ("dlopen(", "dynamic-loader"),
    ("System.load", "dynamic-loader"),
    ("System.loadLibrary", "dynamic-loader"),
    ("process.dlopen", "dynamic-loader"),
    ("node-gyp", "runtime-loader"),
    ("import \"C\"", "target-abi"),
    ("//go:linkname", "source-link"),
    ("plugin.Open(", "runtime-loader"),
)
_WINDOW = 240


@dataclass(frozen=True)
class FfiHit:
    path: str
    line: int
    sink: str
    token: str
    evidence: str
    sink_family: str = "dynamic-loader"
    signal_kind: str = "runtime-artifact"
    confidence: str = "high"

    def as_dict(self) -> dict[str, Any]:
        return {
            "detector": "ffi",
            "capability": "runtime",
            "path": self.path,
            "line": self.line,
            "sink": self.sink,
            "sink_family": self.sink_family,
            "token": self.token,
            "signal_kind": self.signal_kind,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


def _signal_kind(signal: str) -> str:
    lowered = signal.casefold()
    if lowered.startswith("lib") or lowered.startswith("-l"):
        return "loader-artifact"
    return "runtime-symbol"


def detect_source_runtime_ffi(
    root: Path,
    config: Mapping[str, Any],
    *,
    tokens: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    wanted = {
        str(item).casefold() for item in (tokens or native_runtime_signals(config))
    }
    wanted.discard("")
    if not wanted:
        return []
    hits: list[dict[str, Any]] = []
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        clean = strip_detector_comments(text, path.suffix)
        lowered = clean.casefold()
        ordered = sorted(wanted)
        for sink, family in _FFI_SINKS:
            start = lowered.find(sink.casefold())
            while start >= 0:
                window = clean[
                    max(0, start - _WINDOW) : min(len(clean), start + _WINDOW)
                ]
                matched = next(
                    (signal for signal in ordered if token_present(window, signal)),
                    None,
                )
                if matched is not None:
                    hits.append(
                        FfiHit(
                            path=path.relative_to(root).as_posix(),
                            line=_line_of(text, start),
                            sink=sink,
                            sink_family=family,
                            token=matched,
                            signal_kind=_signal_kind(matched),
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


__all__ = ["detect_source_runtime_ffi"]
