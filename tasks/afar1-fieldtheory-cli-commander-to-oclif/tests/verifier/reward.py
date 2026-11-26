"""Verifier-only reward calculation shared by local and Harbor runners.

The final score has exactly three stages: hard gate, verifier, total.  There is
no objective-metric or rubric stage, no second grading path, and the verifier
test weight is fixed at 1.0.  The reward is therefore ``T`` itself:

* cheating (L2 runtime bridge, protected-path or report tampering) -> ``-1``
* non-cheating hard-gate failure -> ``0``
* otherwise -> canonical verifier progress ``T``

P2P is a hard-gate boolean only.  It never contributes to ``T`` and is reported
as ``p2p_pass_ratio: null`` so no completion ratio can be derived from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from .progress import build_verifier_progress, progress_to_dict
except ImportError:
    from progress import build_verifier_progress, progress_to_dict

TEST_WEIGHT = 1.0
SCORING_VERSION = "migration-verifier-only-reward:v2"
REWARD_MODE = "VERIFIER_ONLY"
_VALID_BRANCHES = {
    "cheating",
    "hard_gate_failed",
    "verifier_failed",
    "verifier_success",
    "inconclusive",
}


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
) -> dict[str, Any]:
    """Calculate the verifier-only reward; ``reward == T`` with weight 1.0."""

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

    # Cheating is decided first and independently of the hard-gate status so a
    # runtime bridge can never be scored as an ordinary verifier failure.
    if cheating:
        branch, value = "cheating", -1.0
    elif hardgate_status == "FAIL":
        branch, value = "hard_gate_failed", 0.0
    elif hardgate_status != "PASS" or verifier_status in {"infra_error", "inconclusive"}:
        branch, value = "inconclusive", 0.0
    elif not metrics_valid:
        branch, value = "inconclusive", 0.0
    elif verifier_status == "success" and progress == 1.0:
        branch, value = "verifier_success", TEST_WEIGHT
    else:
        branch, value = "verifier_failed", TEST_WEIGHT * float(progress)

    if branch in {"inconclusive", "cheating", "hard_gate_failed"}:
        progress = None

    return {
        "branch": branch,
        "test_weight": TEST_WEIGHT,
        "T": progress,
        "value": round(value, 7),
        "migration_scope_complete_ratio": scope_ratio,
        "f2p_pass_ratio": f2p_ratio,
        # P2P is a hard-gate boolean; there is no P2P completion ratio.
        "p2p_pass_ratio": None,
        "required_state_preservation_ratio": state_ratio,
        "reward_mode": REWARD_MODE,
        "scoring_version": SCORING_VERSION,
    }


def calculate_reward(
    *,
    hardgate: Mapping[str, object],
    verifier: Mapping[str, object],
) -> dict[str, Any]:
    """Single reward entrypoint: hard gate -> verifier -> total."""

    return calculate_verifier_reward(hardgate=hardgate, verifier=verifier)


__all__ = [
    "TEST_WEIGHT",
    "SCORING_VERSION",
    "REWARD_MODE",
    "_VALID_BRANCHES",
    "calculate_verifier_reward",
    "calculate_reward",
]
