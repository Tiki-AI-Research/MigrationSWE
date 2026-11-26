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


def _typed_json_value(value: object) -> object:
    if not isinstance(value, dict) or value.get("type") != "json" or "value" not in value:
        raise ValueError("observation value must be a typed JSON envelope")
    return value["value"]


def compare_observation_to_baseline(
    baseline: object,
    candidate: object,
    normalization_policy: object = None,
    *,
    status_only_status_comparison: bool = True,
) -> str:
    """Compare one observation with its source baseline consistently.

    A status-only P2P baseline carries no concrete projection.  Comparing it
    against a concrete candidate projection by value would fail every case, so
    the two sides are compared by status instead.  Calibration and the real H7
    entry must both call this function; keeping separate copies caused them to
    disagree on the same fixture.
    """

    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        return "INCONCLUSIVE"
    baseline_value = baseline.get("value")
    candidate_value = candidate.get("value")
    if status_only_status_comparison and (
        status_only_projection(baseline_value)
        != status_only_projection(candidate_value)
    ):
        return (
            "PASS"
            if str(baseline.get("status")) == str(candidate.get("status"))
            else "FAIL"
        )
    try:
        from .differential import compare as compare_values

        policy = normalization_policy if isinstance(normalization_policy, dict) else {}
        return str(
            compare_values(
                _typed_json_value(baseline_value),
                _typed_json_value(candidate_value),
                policy,
            )["status"]
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        return "INCONCLUSIVE"


__all__ = [
    "status_only_projection",
    "merge_comparison_status",
    "compare_observation_to_baseline",
]
