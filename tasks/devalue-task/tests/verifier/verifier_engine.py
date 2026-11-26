"""Stable Migration verifier aggregation engine."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .unit_checks import evaluate_unit_static_checks
except ImportError:  # Standalone Runtime Draft layout.
    from unit_checks import evaluate_unit_static_checks


def _component(rows: Sequence[Mapping[str, Any]], *, empty: str = "INCONCLUSIVE") -> dict[str, Any]:
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    status = (
        empty
        if not statuses
        else "INFRA_ERROR"
        if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses)
        else "INCONCLUSIVE"
        if any(item in {"INCONCLUSIVE", "NA"} for item in statuses)
        else "NOT_RUN"
        if any(item == "NOT_RUN" for item in statuses)
        else "FAIL"
        if any(item == "FAIL" for item in statuses)
        else "PASS"
    )
    return {"status": status, "passed": sum(item == "PASS" for item in statuses), "total": len(statuses), "checks": list(rows)}


def _ratio(rows: Sequence[Mapping[str, Any]]) -> float:
    return 0.0 if not rows else sum(row.get("status") == "PASS" for row in rows) / len(rows)


def evaluate_migration_units(
    units: Sequence[Mapping[str, Any]],
    unit_checks: Mapping[str, Any] | None,
    observations: Sequence[Mapping[str, Any]],
    ordinary_residue_hits: Sequence[Mapping[str, Any]] = (),
    candidate_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Attach observation evidence and deterministic static checks to each unit."""

    by_id = {
        str(row.get("id")): row
        for row in observations
        if isinstance(row, Mapping) and row.get("id")
    }
    mapping = unit_checks or {}
    root = Path(candidate_root) if candidate_root is not None else None
    results: list[dict[str, Any]] = []
    assigned_hits: set[int] = set()
    for unit in units:
        unit_id = str(unit.get("id") or unit.get("unit_id") or "")
        raw = mapping.get(unit_id, {})
        refs = raw.get("observation_ids", []) if isinstance(raw, Mapping) else raw
        refs = [str(item) for item in refs] if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)) else []
        checks = [by_id[item] for item in refs if item in by_id]
        missing = [item for item in refs if item not in by_id]
        locators = []
        for field in ("source_locators", "target_locators"):
            value = unit.get(field, [])
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                locators.extend(value)
        unit_hits: list[Mapping[str, Any]] = []
        for index, hit in enumerate(ordinary_residue_hits):
            hit_path = str(hit.get("path", ""))
            if any(
                isinstance(locator, Mapping)
                and (
                    hit_path == str(locator.get("path", "")).strip("/")
                    or hit_path.startswith(str(locator.get("path", "")).strip("/") + "/")
                )
                for locator in locators
            ):
                unit_hits.append(hit)
                assigned_hits.add(index)
        static = evaluate_unit_static_checks(unit, root)
        static_status = str(static.get("status", "INCONCLUSIVE"))
        if any(str(row.get("status")) in {"INFRA_ERROR", "TIMEOUT"} for row in checks):
            status = "INFRA_ERROR"
        elif static_status == "INCONCLUSIVE" or not refs or missing:
            status = "INCONCLUSIVE"
        elif static_status == "FAIL" or unit_hits:
            status = "FAIL"
        elif any(str(row.get("status")) in {"FAIL", "INCONCLUSIVE", "NA"} for row in checks):
            status = "FAIL"
        else:
            status = "PASS"
        results.append({
            **dict(unit),
            "id": unit_id,
            "status": status,
            "observation_ids": refs,
            "checks": checks,
            "missing_observation_ids": missing,
            "ordinary_residue_hits": [dict(row) for row in unit_hits],
            "static_checks": static,
        })
    unassigned = [
        dict(hit)
        for index, hit in enumerate(ordinary_residue_hits)
        if index not in assigned_hits
    ]
    if unassigned:
        results.append(
            {
                "id": "UNASSIGNED-SOURCE-RESIDUE",
                "kind": "residue",
                "status": "FAIL",
                "observation_ids": [],
                "checks": [],
                "missing_observation_ids": [],
                "ordinary_residue_hits": unassigned,
                "static_checks": {"status": "FAIL", "unit_id": "UNASSIGNED-SOURCE-RESIDUE", "checks": []},
            }
        )
    return results


def _required(applicability: Mapping[str, Any] | None, group: str, default: bool) -> bool:
    if not isinstance(applicability, Mapping) or group not in applicability:
        return default
    entry = applicability.get(group)
    if isinstance(entry, Mapping):
        return entry.get("status") == "required"
    return str(entry) == "required"


def _declared_group_applicability(
    applicability: Mapping[str, Any] | None, group: str
) -> bool | str | None:
    """Return an explicit group declaration, ``None`` when absent, or invalid."""

    if not isinstance(applicability, Mapping) or group not in applicability:
        return None
    entry = applicability.get(group)
    status = entry.get("status") if isinstance(entry, Mapping) else entry
    if status == "required":
        return True
    if status == "na":
        return False
    return "invalid"


def _hardgate_short_circuit(
    status: str,
    hardgate: Mapping[str, Any] | None = None,
    not_run_reason: str | None = None,
) -> dict[str, Any]:
    hardgate = hardgate if isinstance(hardgate, Mapping) else {}
    component_status = (
        "NOT_RUN" if not_run_reason
        else "FAIL" if status == "FAIL"
        else "INFRA_ERROR" if status == "INFRA_ERROR"
        else "INCONCLUSIVE"
    )
    empty = {
        "status": component_status,
        "passed": 0,
        "total": 0,
        "checks": [],
        "short_circuited_by_hardgate": status,
        "evaluated": not bool(not_run_reason),
    }
    if not_run_reason:
        empty["not_run_reason"] = not_run_reason
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": (
            "failed" if status == "FAIL"
            else "infra_error" if status == "INFRA_ERROR"
            else "inconclusive"
        ),
        "hardgate": status,
        "cheating": bool(hardgate.get("cheating")),
        "cheat_level": str(hardgate.get("cheat_level", "L0")),
        "reward_override": hardgate.get("reward_override"),
        "f2p_status": component_status,
        "p2p_status": component_status,
        "not_run_reason": not_run_reason,
        "diagnostic_only": False,
        "components": {
            "migration_scope_complete": dict(empty),
            "p2p_regression_preserved": dict(empty),
            "regression_test": {
                **empty,
                "p2p": dict(empty),
                "f2p": dict(empty),
                "differential": dict(empty),
            },
            "required_state_preservation_complete": dict(empty),
        },
        "test_progress": None,
        "eligible_for_graded": False,
        "evidence_index": [],
    }


def _group_ratio(rows: Sequence[Mapping[str, Any]]) -> float:
    if not rows:
        return 0.0
    return sum(row.get("status") == "PASS" for row in rows) / len(rows)


def _scored_progress(
    *,
    scoped_units: Sequence[Mapping[str, Any]],
    f2p: Sequence[Mapping[str, Any]],
    f2p_required: bool,
    state_rows: Sequence[Mapping[str, Any]],
    state_applicable: bool,
) -> dict[str, Any]:
    """Compute T as the arithmetic mean of the applicable group rates.

    P2P is a boolean hard gate and must never contribute to the score, and the
    differential group is not a scored verifier component.  Migration-scope,
    required F2P and applicable required-state coverage rates are averaged
    arithmetically (not multiplied), so a candidate with rates 0.5 and 1.0
    scores 0.75.  Non-applicable groups are omitted from the denominator; a
    missing required group and a run with no applicable groups both score 0,
    matching the verifier contract for incomplete evidence.
    """

    rates: list[float] = []
    incomplete = False
    if scoped_units:
        rates.append(_group_ratio(scoped_units))
    if f2p_required:
        if f2p:
            rates.append(_group_ratio(f2p))
        else:
            incomplete = True
    if state_applicable:
        if state_rows:
            rates.append(_group_ratio(state_rows))
        else:
            incomplete = True

    scored: list[Mapping[str, Any]] = list(scoped_units)
    if f2p_required:
        scored.extend(f2p)
    if state_applicable:
        scored.extend(state_rows)
    passed = sum(row.get("status") == "PASS" for row in scored)
    total = len(scored)
    progress = 0.0 if incomplete or not rates else sum(rates) / len(rates)
    return {
        "T": round(max(0.0, min(1.0, progress)), 7),
        "passed": passed,
        "total": total,
        "incomplete_required_groups": incomplete,
    }


def evaluate_verifier(
    *,
    hardgate: Mapping[str, Any],
    migration_units: Sequence[Mapping[str, Any]],
    p2p: Sequence[Mapping[str, Any]],
    f2p: Sequence[Mapping[str, Any]],
    differential: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    weights: Mapping[str, float],
    unit_checks: Mapping[str, Any] | None = None,
    applicability: Mapping[str, Any] | None = None,
    candidate_root: Path | str | None = None,
    not_run_reason: str | None = None,
) -> dict[str, Any]:
    del weights
    if hardgate.get("status") != "PASS":
        return _hardgate_short_circuit(
            str(hardgate.get("status", "INCONCLUSIVE")),
            hardgate,
            not_run_reason,
        )
    all_observations = [*p2p, *f2p, *differential, *list(state.get("checks", []))]
    ordinary_residue_hits = hardgate.get("ordinary_residue_hits", [])
    ordinary_residue_hits = ordinary_residue_hits if isinstance(ordinary_residue_hits, Sequence) and not isinstance(ordinary_residue_hits, (str, bytes)) else []
    scoped_units = evaluate_migration_units(
        migration_units,
        unit_checks,
        all_observations,
        ordinary_residue_hits,
        candidate_root,
    )
    scope = _component(scoped_units)
    p2p_required = _required(applicability, "P2P", True)
    f2p_required = _required(applicability, "F2P", True)
    p2p_result = _component(p2p) if p2p_required else _component(p2p, empty="NA")
    f2p_result = _component(f2p) if f2p_required else _component(f2p, empty="NA")
    diff_result = _component(differential) if _required(applicability, "DIFFERENTIAL", True) else _component(differential, empty="NA")
    p2p_gate = {
        "status": p2p_result["status"],
        "passed": p2p_result["passed"],
        "total": p2p_result["total"],
        "requires_all": True,
    }
    state_rows = list(state.get("checks", []))
    declared_state = state.get("applicable")
    group_state = _declared_group_applicability(applicability, "STATE")
    state_error: str | None = None
    if not isinstance(declared_state, bool):
        # Missing, ``None``, or a string such as "unknown" is not "not
        # applicable".  An unresolved declaration must fail closed instead of
        # silently producing an NA state component.
        state_error = (
            "state applicability is not an explicit boolean; "
            "required-state preservation cannot be evaluated"
        )
    elif group_state == "invalid":
        state_error = "group_applicability.STATE.status is not a valid declaration"
    elif isinstance(group_state, bool) and group_state != declared_state:
        state_error = (
            "state applicability declarations disagree: "
            f"state={declared_state}, group_applicability.STATE={group_state}"
        )
    if state_error is not None:
        state_applicable = False
        state_result = {
            "status": "INCONCLUSIVE",
            "passed": 0,
            "total": 0,
            "checks": state_rows,
            "error": state_error,
        }
    else:
        state_applicable = declared_state
        state_result = _component(state_rows) if state_applicable else {"status": "NA", "passed": 0, "total": 0, "checks": []}
    progress = _scored_progress(
        scoped_units=scoped_units,
        f2p=f2p,
        f2p_required=f2p_required,
        state_rows=state_rows,
        state_applicable=state_applicable,
    )
    if state_error is not None:
        # Other applicable groups must not compensate for a missing required
        # state dimension when T_weight=1.
        progress = {**progress, "T": 0.0, "incomplete_required_groups": True}
    if progress["incomplete_required_groups"]:
        f2p_result = {**f2p_result, "status": "INCONCLUSIVE"} if f2p_required else f2p_result
    behaviour_statuses = [
        row["status"] for row in (f2p_result, diff_result) if row["status"] != "NA"
    ]
    behaviour_status = (
        "INFRA_ERROR" if "INFRA_ERROR" in behaviour_statuses
        else "INCONCLUSIVE" if "INCONCLUSIVE" in behaviour_statuses
        else "FAIL" if "FAIL" in behaviour_statuses
        else "PASS"
    )
    # Scope completeness and the behavioural groups stay separate statuses.
    # P2P is an independent boolean hard gate: it can fail the run but it must
    # never contribute to T.
    blockers = [scope["status"], behaviour_status, state_result["status"]]
    if p2p_required and p2p_gate["status"] != "PASS":
        blockers.append("FAIL" if p2p_gate["status"] == "FAIL" else p2p_gate["status"])
    if any(item == "INFRA_ERROR" for item in blockers):
        status = "infra_error"
    elif any(item == "INCONCLUSIVE" for item in blockers):
        status = "inconclusive"
    elif any(item == "FAIL" for item in blockers):
        status = "failed"
    else:
        status = "success"
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": status,
        "hardgate": "PASS",
        "cheating": bool(hardgate.get("cheating")),
        "cheat_level": str(hardgate.get("cheat_level", "L0")),
        "reward_override": None,
        "f2p_status": f2p_result["status"],
        "p2p_status": p2p_gate["status"],
        "not_run_reason": None,
        "diagnostic_only": False,
        "components": {
            "migration_scope_complete": scope,
            "p2p_regression_preserved": p2p_gate,
            "regression_test": {
                "status": behaviour_status,
                "p2p": p2p_result,
                "f2p": f2p_result,
                "differential": diff_result,
            },
            "required_state_preservation_complete": state_result,
        },
        "test_progress": {
            "T": progress["T"],
            "passed": progress["passed"],
            "total": progress["total"],
        },
        "eligible_for_graded": status == "success",
        "evidence_index": [],
    }
