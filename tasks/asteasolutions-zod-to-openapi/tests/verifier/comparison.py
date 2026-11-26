"""Shared monotonic observation comparison semantics."""

from __future__ import annotations


def status_only_projection(value: object) -> bool:
    """Return whether a typed observation value is only a status wrapper."""

    if not isinstance(value, dict) or value.get("type") != "json":
        return False
    payload = value.get("value")
    if not isinstance(payload, dict) or not payload:
        return False
    return all(
        str(key).casefold() in {"result", "outcome", "status"}
        and isinstance(item, str)
        and item.strip().casefold()
        in {
            "pass",
            "passed",
            "success",
            "successes",
            "ok",
            "fail",
            "failed",
            "failure",
            "error",
            "errors",
            "inconclusive",
        }
        for key, item in payload.items()
    )


def merge_comparison_status(original_status: str, comparison_status: str) -> str:
    """Apply a value comparison without ever upgrading an existing failure.

    Comparison is an additional constraint.  It may turn a passing observation
    into a failure, but every non-PASS producer status is authoritative and must
    survive unchanged.
    """

    original = str(original_status)
    return str(comparison_status) if original == "PASS" else original


__all__ = ["merge_comparison_status", "status_only_projection"]
