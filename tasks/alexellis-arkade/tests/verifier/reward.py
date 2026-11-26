"""Verifier-only reward calculation shared by local and Harbor runners.

The score has exactly one dimension: ``T``, the mean of the applicable verifier
components (migration scope, F2P, and STATE when it applies).  There is no
objective metric and no rubric component, so this module has no O/R weights, no
``GRADED`` mode and no eligibility flag to branch on.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from .progress import build_verifier_progress, progress_to_dict
except ImportError:
    from progress import build_verifier_progress, progress_to_dict

TEST_WEIGHT = 1.0
COMPONENT_WEIGHTS = {"T": TEST_WEIGHT}
SCORING_VERSION = "migration-verifier-reward:v2"
_VALID_BRANCHES = {
    "cheating",
    "hard_gate_failed",
    "verifier_failed",
    "verifier_success",
    "inconclusive",
}


def _ratio(component: object, *, label: str) -> float | None:
    if not isinstance(component, Mapping):
        return None
    passed = component.get("passed")
    total = component.get("total")
    if (
        isinstance(passed, bool)
        or not isinstance(passed, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or passed < 0
        or passed > total
    ):
        return None
    if total == 0:
        # No observable test in the group: the ratio is undefined, so the caller
        # must not read it as a pass.
        return None
    return passed / total


def _canonical_progress(verifier: Mapping[str, object]) -> dict[str, object] | None:
    value = verifier.get("progress")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("canonical progress must be an object")
    eligible = value.get("eligible")
    progress_t = value.get("T")
    if not isinstance(eligible, bool):
        raise ValueError("canonical progress eligibility is invalid")
    if eligible:
        for key in ("scope_ratio", "f2p_ratio", "state_ratio"):
            ratio = value.get(key)
            if key == "state_ratio" and ratio is None:
                continue
            if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.0 <= ratio <= 1.0:
                raise ValueError(f"canonical progress {key} is invalid")
        if progress_t is None or value.get("errors"):
            raise ValueError("canonical progress eligibility is contradictory")
    elif progress_t is not None:
        raise ValueError("ineligible canonical progress must have null T")
    if progress_t is not None and (
        isinstance(progress_t, bool)
        or not isinstance(progress_t, (int, float))
        or not 0.0 <= float(progress_t) <= 1.0
    ):
        raise ValueError("canonical progress T is invalid")
    if isinstance(verifier.get("test_progress"), Mapping):
        legacy_t = verifier["test_progress"].get("T")
        if legacy_t != progress_t:
            raise ValueError("contradictory canonical and compatibility progress T")
    return dict(value)


def calculate_verifier_reward(
    *,
    hardgate: Mapping[str, object],
    verifier: Mapping[str, object],
    test_weight: float = TEST_WEIGHT,
) -> dict[str, Any]:
    """Calculate the verifier-only reward from ``T`` alone."""

    if not isinstance(test_weight, (int, float)) or isinstance(test_weight, bool):
        raise ValueError("test_weight must be numeric")
    weight = float(test_weight)
    if not 0.0 <= weight <= 1.0:
        raise ValueError("test_weight must be between 0 and 1")

    hardgate_status = str(hardgate.get("status", "INCONCLUSIVE"))
    cheating = bool(hardgate.get("cheating"))
    verifier_status = str(verifier.get("status", "inconclusive"))
    components = verifier.get("components")
    components = components if isinstance(components, Mapping) else {}
    regression = components.get("regression_test")
    regression = regression if isinstance(regression, Mapping) else {}
    canonical = _canonical_progress(verifier)
    if canonical is None:
        state_component = components.get("required_state_preservation_complete")
        state_status = str(state_component.get("status")) if isinstance(state_component, Mapping) else "INCONCLUSIVE"
        canonical = progress_to_dict(build_verifier_progress(
            migration_scope=components.get("migration_scope_complete"),
            f2p=regression.get("f2p"), state=state_component,
            state_applicable=state_status != "NA",
        ))
    scope_ratio = canonical.get("scope_ratio")
    f2p_ratio = canonical.get("f2p_ratio")
    state_ratio = canonical.get("state_ratio")
    progress = canonical.get("T")
    metrics_valid = canonical.get("eligible") is True and progress is not None
    p2p_ratio = _ratio(regression.get("p2p"), label="p2p")

    # A cheating verdict is a property of the evidence, not of the aggregate
    # status: when one gate raises a policy violation and another only reports an
    # infrastructure error, the violation still has to be punished with -1
    # instead of being downgraded to "could not measure".
    if cheating:
        branch, value = "cheating", -1.0
    elif hardgate_status == "FAIL":
        branch, value = "hard_gate_failed", 0.0
    elif hardgate_status != "PASS" or verifier_status in {"infra_error", "inconclusive"}:
        branch, value = "inconclusive", 0.0
    elif not metrics_valid:
        branch, value = "inconclusive", 0.0
    elif verifier_status == "success" and progress == 1.0:
        branch, value = "verifier_success", weight
    else:
        branch, value = "verifier_failed", weight * float(progress)

    if branch in {"inconclusive", "cheating", "hard_gate_failed"}:
        progress = None

    return {
        "branch": branch,
        "test_weight": weight,
        "T": progress,
        "value": round(value, 7),
        "migration_scope_complete_ratio": scope_ratio,
        "f2p_pass_ratio": f2p_ratio,
        "p2p_pass_ratio": p2p_ratio,
        "required_state_preservation_ratio": state_ratio,
    }


def calculate_reward(
    *,
    hardgate: Mapping[str, object],
    verifier: Mapping[str, object],
) -> dict[str, Any]:
    """Apply the single-dimension reward policy to the trusted verifier result."""

    result = calculate_verifier_reward(
        hardgate=hardgate,
        verifier=verifier,
        test_weight=COMPONENT_WEIGHTS["T"],
    )
    result.update({
        "scoring_version": SCORING_VERSION,
        "weights": dict(COMPONENT_WEIGHTS),
    })
    return result


__all__ = [
    "TEST_WEIGHT", "COMPONENT_WEIGHTS", "SCORING_VERSION",
    "calculate_verifier_reward", "calculate_reward",
]
