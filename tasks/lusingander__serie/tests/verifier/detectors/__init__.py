"""Deterministic language-neutral detectors shared by the verifier contract."""

from .dynamic import detect_source_runtime_dynamic_load
from .ffi import detect_source_runtime_ffi
from .subprocess import detect_source_runtime_subprocess, source_runtime_tokens

__all__ = [
    "detect_source_runtime_dynamic_load",
    "detect_source_runtime_ffi",
    "detect_source_runtime_subprocess",
    "source_runtime_tokens",
]
