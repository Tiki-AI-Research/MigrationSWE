"""Stable Migration verifier aggregation engine.

The verifier owns exactly three components: migration scope coverage, F2P
regression and (conditional) required-state preservation.  ``reward == T`` with
test weight 1.0, computed as the applicable mean of those components.

P2P and DIFFERENTIAL are hard-gate booleans.  The case contract describes a
migration unit with a mix of observation IDs, so those IDs are dropped *before*
scope coverage is measured: they never form a verifier component, never enter a
completion ratio, never become a blocker and never reach ``T``.  A DIFFERENTIAL
failure is detected by the hard gate; if it ever reaches this engine with the
hard gate already claimed PASS, the engine fails closed instead of scoring it.
"""

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


_HARD_GATE_ONLY_KINDS = frozenset({"P2P", "DIFFERENTIAL"})


def _component(rows: Sequence[Mapping[str, Any]], *, empty: str = "INCONCLUSIVE") -> dict[str, Any]:
    statuses = [str(row.get("status", "INCONCLUSIVE")) for row in rows]
    status = empty if not statuses else "INFRA_ERROR" if any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses) else "INCONCLUSIVE" if any(item in {"INCONCLUSIVE", "NA"} for item in statuses) else "FAIL" if any(item == "FAIL" for item in statuses) else "PASS"
    return {"status": status, "passed": sum(item == "PASS" for item in statuses), "total": len(statuses), "checks": list(rows)}


def evaluate_migration_units(
    units: Sequence[Mapping[str, Any]],
    unit_checks: Mapping[str, Any] | None,
    observations: Sequence[Mapping[str, Any]],
    ordinary_residue_hits: Sequence[Mapping[str, Any]] = (),
    hard_gate_only_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Attach Case-specific observation evidence to each migration unit.

    ``hard_gate_only_ids`` are the P2P/DIFFERENTIAL observation IDs of the case
    contract.  They are recorded on the unit for traceability and then removed
    before scope coverage is measured, so a hard-gate-only observation can never
    raise or lower the verifier's scope ratio or ``T``.

    A unit whose declared references are *all* hard-gate-only therefore has no
    verifier-observable evidence left.  Such a unit is reported with status
    ``NA`` and ``scope_eligible = False``: it stays in the result for
    traceability but is excluded from the scope ratio, otherwise an
    unimplementable measurement (there is nothing left to measure) would
    poison ``T``.  This is only allowed when the unit really declared
    references; a unit that declared none at all keeps the fail-closed
    ``INCONCLUSIVE`` behaviour, and any residue hit on the unit still fails it.
    """

    excluded = {str(item) for item in hard_gate_only_ids}
    by_id = {
        str(row.get("id")): row
        for row in observations
        if isinstance(row, Mapping) and row.get("id") and str(row.get("id")) not in excluded
    }
    mapping = unit_checks or {}
    results: list[dict[str, Any]] = []
    assigned_hits: set[int] = set()
    for unit in units:
        unit_id = str(unit.get("id") or unit.get("unit_id") or "")
        raw = mapping.get(unit_id, {})
        declared_refs = raw.get("observation_ids", []) if isinstance(raw, Mapping) else raw
        declared_refs = [str(item) for item in declared_refs] if isinstance(declared_refs, Sequence) and not isinstance(declared_refs, (str, bytes)) else []
        deferred = [item for item in declared_refs if item in excluded]
        refs = [item for item in declared_refs if item not in excluded]
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
            {"id": unit_id, "observation_ids": refs, "hard_gate_only_ids": deferred},
            observations,
        )
        hard_gate_only_unit = bool(declared_refs) and not refs
        if unit_hits:
            # Ordinary (non-bridge) residue inside the unit's own locators is a
            # real scope failure, independent of how the contract splits its
            # evidence between the hard gate and the verifier.
            status = "FAIL"
        elif hard_gate_only_unit:
            status = "NA"
        else:
            status = inspected.status
        results.append({
            **dict(unit),
            "id": unit_id,
            "status": status,
            "observation_ids": refs,
            "hard_gate_only_observation_ids": deferred,
            "hard_gate_only_scope": hard_gate_only_unit,
            "scope_eligible": not hard_gate_only_unit or bool(unit_hits),
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
                "scope_eligible": True,
                "observation_ids": [],
                "hard_gate_only_observation_ids": [],
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


def _empty_component(status: str, *, hardgate: str) -> dict[str, Any]:
    return {
        "status": status,
        "passed": 0,
        "total": 0,
        "checks": [],
        "short_circuited_by_hardgate": hardgate,
    }


def _hardgate_short_circuit(
    status: str,
    *,
    cheating: bool = False,
    reason: str | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    component_status = (
        "FAIL" if status == "FAIL"
        else "INFRA_ERROR" if status == "INFRA_ERROR"
        else "INCONCLUSIVE"
    )
    empty = _empty_component(component_status, hardgate=status)
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
                "status": component_status,
                "passed": 0,
                "total": 0,
                "checks": [],
                "f2p": dict(empty),
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
            "errors": [reason or "hard gate did not pass"],
        },
        "evidence_index": [],
    }
    if diagnostics:
        result["diagnostics"] = {key: value for key, value in diagnostics.items()}
    result["reward"] = calculate_verifier_reward(
        hardgate={"status": status, "cheating": cheating},
        verifier=result,
    )
    return result


def evaluate_verifier(
    *,
    hardgate: Mapping[str, Any],
    migration_units: Sequence[Mapping[str, Any]],
    p2p: Sequence[Mapping[str, Any]] = (),
    f2p: Sequence[Mapping[str, Any]] = (),
    differential: Sequence[Mapping[str, Any]] = (),
    state: Mapping[str, Any] | None = None,
    unit_checks: Mapping[str, Any] | None = None,
    applicability: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate scope, F2P and conditional state into ``T``.

    ``p2p``/``differential`` are accepted only as hard-gate observations to be
    excluded from scope.  They are never emitted and never scored: P2P and
    DIFFERENTIAL are hard-gate booleans, and the verifier has exactly one weight
    (``T`` at 1.0), so no weight map is accepted here.
    """

    state = state if isinstance(state, Mapping) else {}
    if hardgate.get("status") != "PASS":
        return _hardgate_short_circuit(
            str(hardgate.get("status", "INCONCLUSIVE")),
            cheating=bool(hardgate.get("cheating")),
        )

    if _required(applicability, "DIFFERENTIAL", True) and differential:
        # A required behaviour-equivalence check is a hard-gate boolean.  When
        # a caller supplies failing differential evidence while also claiming
        # the hard gate passed, the gate contract was violated: fail closed
        # instead of reporting success for a drifted public contract.
        diff_status = _component(differential)["status"]
        if diff_status in {"FAIL", "INFRA_ERROR", "INCONCLUSIVE"}:
            return _hardgate_short_circuit(
                "INFRA_ERROR" if diff_status in {"INFRA_ERROR", "INCONCLUSIVE"} else "FAIL",
                cheating=bool(hardgate.get("cheating")),
                reason="DIFFERENTIAL contract is decided by the hard gate",
            )

    # P2P/DIFFERENTIAL are hard-gate booleans.  Their observation IDs still
    # appear in the case contract's unit description, so they are dropped from
    # scope evidence explicitly: no verifier component, ratio, blocker or T
    # input may be derived from them.
    hard_gate_only_ids = [
        str(row.get("id"))
        for row in [*p2p, *differential]
        if isinstance(row, Mapping) and row.get("id")
    ]
    scoped_observations = [
        row
        for row in [*f2p, *list(state.get("checks", []))]
        if not (isinstance(row, Mapping) and str(row.get("kind")) in _HARD_GATE_ONLY_KINDS)
    ]
    ordinary_residue_hits = hardgate.get("ordinary_residue_hits", [])
    ordinary_residue_hits = ordinary_residue_hits if isinstance(ordinary_residue_hits, Sequence) and not isinstance(ordinary_residue_hits, (str, bytes)) else []
    scoped_units = evaluate_migration_units(
        migration_units,
        unit_checks,
        scoped_observations,
        ordinary_residue_hits,
        hard_gate_only_ids,
    )
    scope_units = [row for row in scoped_units if row.get("scope_eligible", True)]
    hard_gate_only_units = [
        str(row.get("id")) for row in scoped_units if not row.get("scope_eligible", True)
    ]
    scope = _component(scope_units)
    if hard_gate_only_units:
        # Units whose whole contract is decided by the hard gate are not
        # measurable by the verifier.  They remain listed for traceability but
        # must not enter the scope denominator, otherwise a correct solution can
        # never reach a full scope ratio.
        scope["hard_gate_only_units"] = hard_gate_only_units
        scope["hard_gate_only_unit_count"] = len(hard_gate_only_units)
        scope["declared_unit_count"] = len(scoped_units)
    f2p_result = _component(f2p) if _required(applicability, "F2P", True) else _component(f2p, empty="NA")
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
    blockers = [scope["status"], f2p_result["status"], state_result["status"]]
    if any(item == "INFRA_ERROR" for item in blockers):
        status = "infra_error"
    elif any(item == "INCONCLUSIVE" for item in blockers):
        status = "inconclusive"
    elif any(item == "FAIL" for item in blockers):
        status = "failed"
    else:
        status = "success"
    result = {
        "schema_version": "migration-verifier-result:v2",
        "status": status,
        "hardgate": "PASS",
        "components": {
            "migration_scope_complete": scope,
            "regression_test": {
                "status": f2p_result["status"],
                "passed": f2p_result["passed"],
                "total": f2p_result["total"],
                "checks": f2p_result["checks"],
                "f2p": f2p_result,
            },
            "required_state_preservation_complete": state_result,
        },
        "progress": progress,
        "test_progress": {"T": progress["T"]} if progress["T"] is not None else None,
        "evidence_index": [],
        # DIFFERENTIAL is decided by the hard gate, so the verifier only records
        # how many equivalence observations it deferred, never their status and
        # never a weight.
        "diagnostics": {
            "differential": {
                "owner": "hard_gate",
                "observations": len(differential),
                "scored": False,
            },
            "p2p": {
                "owner": "hard_gate",
                "observations": len(p2p),
                "scored": False,
            },
        },
    }
    result["reward"] = calculate_verifier_reward(
        hardgate=hardgate,
        verifier=result,
    )
    return result
