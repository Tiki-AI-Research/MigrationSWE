"""Stable Migration verifier aggregation engine."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

try:
    from .reward import calculate_verifier_reward
    from .progress import build_verifier_progress, progress_to_dict
    from .observation_adapter import GenericObservationAdapter
except ImportError:  # Standalone Runtime Draft layout.
    from reward import calculate_verifier_reward
    from progress import build_verifier_progress, progress_to_dict
    from observation_adapter import GenericObservationAdapter


def _component(rows: Sequence[Mapping[str, Any]], *, empty: str = "INCONCLUSIVE") -> dict[str, Any]:
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    status = empty if not statuses else "INFRA_ERROR" if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses) else "INCONCLUSIVE" if any(item in {"INCONCLUSIVE", "NA"} for item in statuses) else "FAIL" if any(item == "FAIL" for item in statuses) else "PASS"
    return {"status": status, "passed": sum(item == "PASS" for item in statuses), "total": len(statuses), "checks": list(rows)}


def _ratio(rows: Sequence[Mapping[str, Any]]) -> float:
    return 0.0 if not rows else sum(row.get("status") == "PASS" for row in rows) / len(rows)


def evaluate_migration_units(
    units: Sequence[Mapping[str, Any]],
    unit_checks: Mapping[str, Any] | None,
    observations: Sequence[Mapping[str, Any]],
    ordinary_residue_hits: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Attach Case-specific observation evidence to each migration unit."""

    by_id = {
        str(row.get("id")): row
        for row in observations
        if isinstance(row, Mapping) and row.get("id")
    }
    mapping = unit_checks or {}
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
        inspected = GenericObservationAdapter().inspect_scope(
            {"id": unit_id, "observation_ids": refs}, observations
        )
        status = "FAIL" if unit_hits and inspected.status not in {"INFRA_ERROR", "INCONCLUSIVE"} else inspected.status
        results.append({
            **dict(unit),
            "id": unit_id,
            "status": status,
            "observation_ids": refs,
            "checks": checks,
            "missing_observation_ids": missing,
            "ordinary_residue_hits": [dict(row) for row in unit_hits],
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


def _state_applicability(
    applicability: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    """Resolve STATE applicability into an explicit three-state value.

    ``required``/``na`` are only returned when the frozen applicability
    declares them explicitly (``na`` must carry a non-empty reason).  A missing
    or unknown declaration is ``undetermined`` and must never be silently
    coerced to ``na``.
    """

    if not isinstance(applicability, Mapping) or "STATE" not in applicability:
        return "undetermined", "STATE applicability was not declared"
    entry = applicability.get("STATE")
    status = entry.get("status") if isinstance(entry, Mapping) else entry
    reason = entry.get("reason") if isinstance(entry, Mapping) else None
    reason = str(reason).strip() if isinstance(reason, str) and str(reason).strip() else None
    if status == "required":
        return "required", reason
    if status == "na":
        return "na", reason
    return "undetermined", f"STATE applicability status is invalid: {status!r}"


def _hardgate_short_circuit(
    status: str,
    *,
    cheating: bool = False,
    hardgate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    component_status = (
        "FAIL" if status == "FAIL"
        else "INFRA_ERROR" if status == "INFRA_ERROR"
        else "INCONCLUSIVE"
    )
    empty = {
        "status": component_status,
        "passed": 0,
        "total": 0,
        "checks": [],
        "short_circuited_by_hardgate": status,
    }
    result = {
        "schema_version": "migration-verifier-result:v2",
        "status": (
            "failed" if status == "FAIL"
            else "infra_error" if status == "INFRA_ERROR"
            else "inconclusive"
        ),
        "hardgate": status,
        "components": {
            "migration_scope_complete": dict(empty),
            "regression_test": {
                **empty,
                "p2p": dict(empty),
                "f2p": dict(empty),
                "differential": dict(empty),
            },
            "required_state_preservation_complete": dict(empty),
        },
        "test_progress": None,
        "progress": {
            "scope_ratio": None,
            "f2p_ratio": None,
            "state_ratio": None,
            "T": None,
            "eligible": False,
            "errors": ["hard gate did not pass"],
        },
        "eligible_for_graded": False,
        "evidence_index": [],
    }
    if isinstance(hardgate, Mapping):
        result["hardgate_detail"] = {
            "status": hardgate.get("status", status),
            "compound_gates": hardgate.get("compound_gates", {}),
            "missing_atoms": hardgate.get("missing_atoms", []),
            "defaulted_atoms": hardgate.get("defaulted_atoms", {}),
            "summary": hardgate.get("summary", {}),
        }
    result["reward"] = calculate_verifier_reward(
        hardgate={"status": status, "cheating": cheating},
        verifier=result,
    )
    return result


def evaluate_verifier(*, hardgate: Mapping[str, Any], migration_units: Sequence[Mapping[str, Any]], p2p: Sequence[Mapping[str, Any]], f2p: Sequence[Mapping[str, Any]], differential: Sequence[Mapping[str, Any]], state: Mapping[str, Any], weights: Mapping[str, float], unit_checks: Mapping[str, Any] | None = None, applicability: Mapping[str, Any] | None = None) -> dict[str, Any]:
    del weights
    if hardgate.get("status") != "PASS":
        return _hardgate_short_circuit(
            str(hardgate.get("status", "INCONCLUSIVE")),
            cheating=bool(hardgate.get("cheating")),
            hardgate=hardgate,
        )
    diagnostic_ids = {str(row.get("id")) for row in differential}
    unit_checks = {
        str(key): {"observation_ids": [str(identifier) for identifier in (
            value.get("observation_ids", []) if isinstance(value, Mapping) else value
        ) if str(identifier) not in diagnostic_ids]}
        for key, value in (unit_checks or {}).items()
    }
    all_observations = [*p2p, *f2p, *list(state.get("checks", []))]
    ordinary_residue_hits = hardgate.get("ordinary_residue_hits", [])
    ordinary_residue_hits = ordinary_residue_hits if isinstance(ordinary_residue_hits, Sequence) and not isinstance(ordinary_residue_hits, (str, bytes)) else []
    scoped_units = evaluate_migration_units(
        migration_units,
        unit_checks,
        all_observations,
        ordinary_residue_hits,
    )
    scope = _component(scoped_units)
    p2p_result = _component(p2p) if _required(applicability, "P2P", True) else _component(p2p, empty="NA")
    f2p_result = _component(f2p) if _required(applicability, "F2P", True) else _component(f2p, empty="NA")
    diff_result = _component(differential) if _required(applicability, "DIFFERENTIAL", True) else _component(differential, empty="NA")
    regression_statuses = [
        row["status"] for row in (p2p_result, f2p_result)
        if row["status"] != "NA"
    ]
    regression_status = "INFRA_ERROR" if "INFRA_ERROR" in regression_statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in regression_statuses else "FAIL" if "FAIL" in regression_statuses else "PASS"
    state_applicability, state_reason = _state_applicability(applicability)
    state_applicable = state_applicability == "required"
    state_rows = list(state.get("checks", []))
    roles = state.get("roles", {})
    roles = roles if isinstance(roles, Mapping) else {}
    invalid_roles = state_applicable and (
        not roles or any(role not in {"critical_data", "required_state"} for role in roles.values())
    )
    required_rows = [row for row in state_rows if roles.get(str(row.get("id"))) == "required_state"]
    if state_applicability == "undetermined":
        # Applicability was omitted or could not be determined: never treat it
        # as NA, surface it as INCONCLUSIVE so downstream cannot ignore it.
        state_result = {
            "status": "INCONCLUSIVE", "passed": 0, "total": 0, "checks": state_rows,
            "reason": "STATE_APPLICABILITY_UNDETERMINED",
            "detail": state_reason,
            "applicability": "undetermined",
        }
    elif state_applicability == "na":
        if state_rows or bool(state.get("applicable")):
            # Declared na while evidence exists is a contract inconsistency.
            state_result = {
                "status": "INCONCLUSIVE", "passed": 0, "total": 0, "checks": state_rows,
                "reason": "STATE_APPLICABILITY_INCONSISTENT",
                "detail": "STATE declared na but observations/applicability are present",
                "applicability": "na",
            }
        else:
            state_result = {
                "status": "NA", "passed": 0, "total": 0, "checks": [],
                "reason": state_reason or "STATE is explicitly not applicable",
                "applicability": "na",
            }
    elif invalid_roles:
        state_result = {
            "status": "INCONCLUSIVE", "passed": 0, "total": 0, "checks": [],
            "reason": "STATE_ROLE_MISSING", "applicability": "required",
        }
    else:
        # `_component([])` is INCONCLUSIVE, covering "required but no evidence".
        state_result = {**_component(required_rows), "applicability": "required"}
    progress_value = build_verifier_progress(
        migration_scope=scope,
        f2p=f2p_result,
        state=state_result,
        state_applicable=state_applicable,
        state_applicability=state_applicability,
    )
    progress = progress_to_dict(progress_value)
    blockers = [scope["status"], regression_status, state_result["status"]]
    if any(item == "INFRA_ERROR" for item in blockers):
        status = "infra_error"
    elif any(item == "INCONCLUSIVE" for item in blockers):
        status = "inconclusive"
    elif any(item == "FAIL" for item in blockers):
        status = "failed"
    else:
        status = "success"
    result = {"schema_version": "migration-verifier-result:v2", "status": status, "hardgate": "PASS", "components": {"migration_scope_complete": scope, "regression_test": {"status": regression_status, "p2p": p2p_result, "f2p": f2p_result, "differential": diff_result}, "required_state_preservation_complete": state_result}, "progress": progress, "test_progress": {"T": progress["T"]} if progress["T"] is not None else None, "eligible_for_graded": False, "evidence_index": [], "diagnostics": {"p2p": p2p_result, "differential": diff_result}}
    result["reward"] = calculate_verifier_reward(
        hardgate=hardgate,
        verifier=result,
    )
    return result
