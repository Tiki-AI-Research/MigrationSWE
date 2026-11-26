"""Shared monotonic observation comparison semantics."""

from __future__ import annotations


def merge_comparison_status(original_status: str, comparison_status: str) -> str:
    """Apply a value comparison without ever upgrading an existing failure.

    Comparison is an additional constraint.  It may turn a passing observation
    into a failure, but every non-PASS producer status is authoritative and must
    survive unchanged.
    """

    original = str(original_status)
    return str(comparison_status) if original == "PASS" else original


__all__ = ["merge_comparison_status"]
