"""Single source of truth for the Migration hard-gate reward decision."""

from __future__ import annotations


def reward_decision(
    *,
    hardgate: str,
    p2p_status: str,
    cheating: bool,
    test_reward: float,
) -> tuple[float, bool]:
    """Return ``(reward, hard_gate_passed)`` for a verifier result.

    A passing hard gate transports the verifier's single test score.  An
    anti-cheating failure is the only path to the negative reward; all other
    hard-gate failures receive zero.
    """

    normalized = str(hardgate).upper()
    p2p = str(p2p_status).upper()
    hard_gate_passed = (
        normalized == "PASS" and p2p in {"PASS", "NA"} and not bool(cheating)
    )
    if hard_gate_passed:
        return float(test_reward), True
    return (-1.0 if cheating else 0.0), False


__all__ = ["reward_decision"]
