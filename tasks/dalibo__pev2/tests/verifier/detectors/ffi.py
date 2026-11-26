"""Detect FFI/native-loading sinks that target the source implementation.

An FFI sink on its own is ordinary C interop.  A *fallback* is a sink **and** a
high-confidence source-runtime artifact (``libnode``/``node::``/... for a
TypeScript source).  The detector deliberately handles the obfuscations a plain
literal rule misses, mirroring :mod:`detectors.subprocess`:

* artifact names assembled from byte/char arrays
  (``[0x6c, 0x69, 0x62, ...]`` / ``['l', 'i', ...]``);
* artifact names written *element by element* into a container
  (``name.push(0x6c); name.push(0x69); ...``), the incremental spelling of the
  same array obfuscation (delegated to
  :func:`detectors.subprocess._decode_runtime_literals`, which now also rebuilds
  these per-container element sequences);
* names split across concatenated string literals (``"lib" + "node" + ".so"``);
* names hidden behind string escapes (``"lib\\x6eode.so"``);
* the sink and the artifact name separated far apart in one file, or split
  across **two files** (a ``runtime_names.rs`` constant consumed by a
  ``legacy_bridge.rs`` ``dlopen``).

The association is therefore no longer limited to a 240-character per-file
window: a file that carries an FFI sink is a fallback when the artifact identity
appears anywhere in the candidate (the artifact vocabulary is high-confidence by
design -- see :mod:`detectors.runtime_identity` -- so a whole-tree association
does not misclassify legal FFI such as ``extern "C" { fn getpid() -> i32; }``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_identity import (
    native_runtime_signals,
    strip_detector_comments,
    token_present,
)
from .subprocess import (
    _candidate_files,
    _collapsed,
    _compact,
    _decode_runtime_literals,
    _joined_literals,
    _line_of,
)


# An FFI sink alone is not evidence of source fallback.  Target C/C++ code may
# legitimately use cgo, extern "C", dlopen or ctypes.  A hit requires a
# high-confidence source-runtime artifact in the same file -- or, for the
# cross-file variant, anywhere in the candidate tree.
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

# Escapes that can hide an artifact name inside a single literal:
# ``"lib\x6eode.so"`` (``\xHH``), ``"lib\u006eode"`` (``\uHHHH``) and
# ``"\u{6e}"`` (Rust ``\u{...}``).
_ESCAPE = re.compile(
    r"\\(?:x([0-9a-fA-F]{2})|u\{([0-9a-fA-F]{1,6})\}|u([0-9a-fA-F]{4}))"
)


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


def _decode_escapes(text: str) -> str:
    """Decode ``\\xHH``/``\\uHHHH``/``\\u{...}`` escapes to their characters."""

    def _replace(match: re.Match[str]) -> str:
        digits = match.group(1) or match.group(2) or match.group(3)
        try:
            code = int(digits, 16)
        except (TypeError, ValueError):
            return match.group(0)
        if not 0 <= code <= 0x10FFFF:
            return match.group(0)
        return chr(code)

    return _ESCAPE.sub(_replace, text)


def _text_forms(clean: str) -> list[str]:
    """Return normalised views in which an artifact name may be hidden.

    Mirrors :mod:`detectors.subprocess`: the raw text, a whitespace-compacted
    view, a fully collapsed view (quotes/``+``/backticks removed), the
    concatenation of every string literal, the decoded numeric/char arrays and
    the escape-decoded text.
    """

    compact = _compact(clean)
    forms = [
        clean,
        compact,
        _collapsed(clean),
        _joined_literals(clean),
        _decode_escapes(clean),
    ]
    # Decode arrays and element-by-element container writes from the *original*
    # text: whitespace matters for cast suffixes (``0x6c as u8``) and for the
    # ``<receiver>.<method>(...)`` shapes the element decoder reconstructs.
    decoded = _decode_runtime_literals(clean)
    if decoded:
        forms.append(" ".join(decoded))
    return forms


def _ffi_token_present(text: str, signal: str) -> bool:
    """Match a source-runtime *artifact* signal in ``text``.

    ``node::``/``v8::`` are C++ namespace prefixes that legitimately precede an
    identifier (``node::Init``, ``v8::Isolate``); a plain word-boundary match
    would fail on the following identifier character, so those signals are
    matched as substrings.  Every other signal keeps the identifier-boundary
    rule so ordinary prose is not mistaken for an artifact.
    """

    if signal.endswith("::"):
        return signal.casefold() in text.casefold()
    return token_present(text, signal)


def _signal_in_forms(forms: Sequence[str], signal: str) -> bool:
    return any(_ffi_token_present(form, signal) for form in forms)


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
    ordered = sorted(wanted)
    scanned: list[tuple[Path, str, str, list[str]]] = []
    # Tree-level artifact inventory: a high-confidence source-runtime artifact
    # that appears *anywhere* in the candidate is evidence of a fallback when an
    # FFI sink appears anywhere else (the cross-file variant).
    tree_signals: dict[str, str] = {}
    for path in _candidate_files(root, config):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        clean = strip_detector_comments(text, path.suffix)
        # Normalise the C ABI marker so ``extern"C"`` and the obfuscated
        # ``extern /* abi */ "C"`` (blanked to whitespace above) both reduce to
        # the canonical ``extern "C"`` the sink table matches.
        clean = re.sub(r'extern\s*"C"', 'extern "C"', clean)
        forms = _text_forms(clean)
        scanned.append((path, text, clean, forms))
        for signal in ordered:
            if signal not in tree_signals and _signal_in_forms(forms, signal):
                tree_signals[signal] = path.relative_to(root).as_posix()
    cross_file_token = next((signal for signal in ordered if signal in tree_signals), None)
    hits: list[dict[str, Any]] = []
    for path, text, clean, forms in scanned:
        lowered = clean.casefold()
        for sink, family in _FFI_SINKS:
            start = lowered.find(sink.casefold())
            while start >= 0:
                window = clean[
                    max(0, start - _WINDOW) : min(len(clean), start + _WINDOW)
                ]
                matched = next(
                    (signal for signal in ordered if _ffi_token_present(window, signal)),
                    None,
                )
                in_window = matched is not None
                if matched is None:
                    # Same-file association: the artifact is assembled/decoded
                    # somewhere in this file, possibly far from the sink.
                    matched = next(
                        (
                            signal
                            for signal in ordered
                            if _signal_in_forms(forms, signal)
                        ),
                        None,
                    )
                if matched is None:
                    # Cross-file association: the artifact name lives in another
                    # file of the candidate tree.
                    matched = cross_file_token
                if matched is not None:
                    evidence = window[:200]
                    if not in_window:
                        evidence = (
                            f"cross-file signal {matched!r} @ "
                            f"{tree_signals.get(matched, '<same-file>')}"
                        )
                    hits.append(
                        FfiHit(
                            path=path.relative_to(root).as_posix(),
                            line=_line_of(text, start),
                            sink=sink,
                            sink_family=family,
                            token=matched,
                            signal_kind=_signal_kind(matched),
                            evidence=evidence,
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
