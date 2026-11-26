"""Conditional persistent-state preservation checks."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

REQUIRED_STATE_INVARIANTS = ("null_missing", "ordering", "defaults", "ids", "relations", "precision", "permissions", "error_semantics")


def evaluate_state_preservation(*, applicable: bool, checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not applicable:
        return {"status": "NA", "checks": []}
    if not checks:
        return {"status": "INCONCLUSIVE", "checks": [], "error": "state preservation requires fixtures and checks"}
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in checks]
    status = "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
    return {"status": status, "checks": [dict(row) for row in checks]}
