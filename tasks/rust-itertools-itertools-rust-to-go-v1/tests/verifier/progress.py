"""Canonical verifier-only progress calculation."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any


@dataclass(frozen=True)
class VerifierProgress:
    scope_ratio: float | None
    f2p_ratio: float | None
    state_ratio: float | None
    T: float | None
    eligible: bool
    errors: tuple[str, ...] = ()
    state_applicability: str = "undetermined"


def _ratio(component: object, *, label: str) -> tuple[float | None, str | None]:
    if not isinstance(component, Mapping):
        return None, f"{label} evidence is missing"
    if component.get("status") not in {"PASS", "FAIL"}:
        return None, f"{label} evidence is not evaluable"
    passed = component.get("passed")
    total = component.get("total")
    if (
        isinstance(passed, bool)
        or not isinstance(passed, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total <= 0
        or passed < 0
        or passed > total
    ):
        return None, f"{label} evidence is malformed"
    return passed / total, None


def build_verifier_progress(
    *,
    migration_scope: Mapping[str, object] | None,
    f2p: Mapping[str, object] | None,
    state: Mapping[str, object] | None,
    state_applicable: bool,
    state_applicability: str | None = None,
) -> VerifierProgress:
    """Build the only scoring progress definition used by the verifier.

    P2P and DIFFERENTIAL are intentionally not inputs: they are retained as
    diagnostics by the verifier, while migration scope, F2P, and applicable
    state preservation form the arithmetic mean used for reward.

    STATE applicability is three-state.  An explicit ``na`` excludes the STATE
    dimension from ``T`` but is recorded so downstream can distinguish it from
    an omitted/undetermined declaration, which is a scoring error.
    """

    if state_applicability is None:
        if state_applicable:
            state_applicability = "required"
        elif isinstance(state, Mapping) and state.get("status") == "NA":
            state_applicability = "na"
        else:
            state_applicability = "undetermined"
    if state_applicability not in {"required", "na", "undetermined"}:
        raise ValueError("invalid STATE applicability")

    scope_ratio, scope_error = _ratio(migration_scope, label="migration scope")
    f2p_ratio, f2p_error = _ratio(f2p, label="F2P")
    state_ratio: float | None = None
    state_error: str | None = None
    if state_applicability == "required":
        state_ratio, state_error = _ratio(state, label="STATE")
    elif state_applicability == "na":
        if not isinstance(state, Mapping) or state.get("status") != "NA":
            state_error = "STATE must be explicitly NA when not applicable"
    else:
        state_error = "STATE applicability is undetermined"

    errors = tuple(
        error
        for error in (scope_error, f2p_error, state_error)
        if error is not None
    )
    values = [scope_ratio, f2p_ratio]
    if state_applicability == "required":
        values.append(state_ratio)
    eligible = not errors and all(value is not None for value in values)
    progress = sum(value for value in values if value is not None) / len(values) if eligible else None
    return VerifierProgress(
        scope_ratio=scope_ratio,
        f2p_ratio=f2p_ratio,
        state_ratio=state_ratio,
        T=progress,
        eligible=eligible,
        errors=errors,
        state_applicability=state_applicability,
    )


def progress_to_dict(progress: VerifierProgress) -> dict[str, Any]:
    return {
        "scope_ratio": progress.scope_ratio,
        "f2p_ratio": progress.f2p_ratio,
        "state_ratio": progress.state_ratio,
        "T": progress.T,
        "eligible": progress.eligible,
        "errors": list(progress.errors),
        "state_applicability": progress.state_applicability,
    }


__all__ = ["VerifierProgress", "build_verifier_progress", "progress_to_dict"]
