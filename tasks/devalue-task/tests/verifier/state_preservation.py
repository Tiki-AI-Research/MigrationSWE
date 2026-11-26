"""Conditional persistent-state preservation checks."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

REQUIRED_STATE_INVARIANTS = ("null_missing", "ordering", "defaults", "ids", "relations", "precision", "permissions", "error_semantics")

_STATE_GROUP_STATUS = {"required": True, "na": False}


class ApplicabilityError(ValueError):
    """Required-state applicability cannot be resolved from declared sources."""


def load_migration_contract(
    config: Mapping[str, Any], bundle_root: Path | str
) -> Mapping[str, Any]:
    """Load the frozen migration contract referenced by the task config."""

    assets = config.get("assets")
    assets = assets if isinstance(assets, Mapping) else {}
    name = assets.get("migration_contract", "migration-contract.json")
    if not isinstance(name, str) or not name.strip():
        raise ApplicabilityError("assets.migration_contract is not declared")
    path = Path(bundle_root) / "assets" / name
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplicabilityError(
            f"migration contract is unreadable at {path.name}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise ApplicabilityError("migration contract must be a JSON object")
    return value


def resolve_state_applicability(
    *,
    policy: Mapping[str, Any],
    group_applicability: Mapping[str, Any] | None,
    contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Resolve required-state applicability from every declared source.

    The task config (``policy.state_applicable`` and
    ``group_applicability.STATE.status``) and the migration contract
    (``state_preservation.applicable``) all describe the same decision.  A
    missing, malformed, or contradictory declaration is a configuration error:
    it must never be interpreted as "not applicable", because that would let a
    verifier grant success without ever checking required state.
    """

    sources: dict[str, Any] = {}
    errors: list[str] = []

    def record_boolean(source: str, value: object) -> None:
        if isinstance(value, bool):
            sources[source] = value
        else:
            errors.append(f"{source} must be an explicit boolean")

    if isinstance(policy, Mapping) and "state_applicable" in policy:
        record_boolean("policy.state_applicable", policy.get("state_applicable"))
    else:
        errors.append("policy.state_applicable is not declared")

    state_entry = (
        group_applicability.get("STATE")
        if isinstance(group_applicability, Mapping)
        else None
    )
    if isinstance(state_entry, Mapping) and "status" in state_entry:
        status = state_entry.get("status")
        if status in _STATE_GROUP_STATUS:
            sources["group_applicability.STATE.status"] = _STATE_GROUP_STATUS[status]
        else:
            errors.append(
                "group_applicability.STATE.status must be one of "
                f"{sorted(_STATE_GROUP_STATUS)}, got {status!r}"
            )
    else:
        errors.append("group_applicability.STATE.status is not declared")

    state_contract = (
        contract.get("state_preservation")
        if isinstance(contract, Mapping)
        else None
    )
    if isinstance(state_contract, Mapping) and "applicable" in state_contract:
        record_boolean(
            "contract.state_preservation.applicable",
            state_contract.get("applicable"),
        )
    else:
        errors.append("contract.state_preservation.applicable is not declared")

    if errors:
        raise ApplicabilityError(
            "state applicability is unresolved: " + "; ".join(errors)
        )
    declared = set(sources.values())
    if len(declared) != 1:
        detail = ", ".join(f"{name}={value}" for name, value in sources.items())
        raise ApplicabilityError(
            "state applicability declarations disagree: " + detail
        )
    return {"applicable": declared.pop(), "sources": sources}


def evaluate_state_preservation(*, applicable: bool, checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not applicable:
        return {"status": "NA", "checks": []}
    if not checks:
        return {"status": "INCONCLUSIVE", "checks": [], "error": "state preservation requires fixtures and checks"}
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in checks]
    status = "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
    return {"status": status, "checks": [dict(row) for row in checks]}
