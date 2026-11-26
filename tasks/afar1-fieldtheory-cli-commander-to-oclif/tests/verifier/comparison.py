"""Shared monotonic observation comparison semantics."""

from __future__ import annotations

import copy


def cli_contract_projection(row: dict) -> dict:
    """Compare only exit-code distinctions promised by instruction.md.

    Usage/data errors are required to be nonzero; interruption remains exactly
    130. Keep the original observation in the report for diagnosis, and leave
    null/timeouts, success, and every other field untouched.
    """

    result = copy.deepcopy(row)
    value = result.get("value", {})
    payload = value.get("value") if isinstance(value, dict) else None
    if not isinstance(payload, dict):
        return result

    def failure_code(value: object) -> object:
        return 1 if isinstance(value, int) and not isinstance(value, bool) and value != 0 else value

    identifier = result.get("id")
    if identifier in {"P2P-010", "P2P-017"}:
        for case in payload.get("cases", []):
            if isinstance(case, dict) and "exit" in case:
                case["exit"] = failure_code(case["exit"])
    elif identifier == "DIFF-010":
        for key in ("searchNoDataExit", "recentLimitZeroExit"):
            if key in payload:
                payload[key] = failure_code(payload[key])
    elif identifier == "DIFF-020":
        # The instruction requires the notice in help; it does not prohibit
        # the same notice when help follows an error.
        payload.pop("errorHelpHasNotice", None)
    return result


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


__all__ = ["cli_contract_projection", "merge_comparison_status", "status_only_projection"]
