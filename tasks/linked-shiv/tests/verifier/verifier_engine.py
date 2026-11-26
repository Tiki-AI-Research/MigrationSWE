"""Stable Migration verifier aggregation engine."""

from __future__ import annotations

from typing import Any, Mapping, Sequence


_KNOWN_STATUSES = frozenset(
    {"PASS", "FAIL", "NA", "INCONCLUSIVE", "INFRA_ERROR", "TIMEOUT"}
)


def _status_of(row: Mapping[str, Any]) -> str:
    status = str(row.get("status", "INCONCLUSIVE")).upper()
    return "INFRA_ERROR" if status == "TIMEOUT" else status


def _component(
    rows: Sequence[Mapping[str, Any]],
    *,
    empty: str = "INCONCLUSIVE",
) -> dict[str, Any]:
    """Summarize checks without allowing an inconclusive result to hide FAIL."""

    checks = [dict(row) for row in rows if isinstance(row, Mapping)]
    statuses = [_status_of(row) for row in checks]
    if not statuses:
        status = empty
    elif any(item not in _KNOWN_STATUSES for item in statuses):
        status = "INFRA_ERROR"
    elif any(item in {"INFRA_ERROR", "TIMEOUT"} for item in statuses):
        status = "INFRA_ERROR"
    elif any(item == "FAIL" for item in statuses):
        status = "FAIL"
    elif any(item == "INCONCLUSIVE" for item in statuses):
        status = "INCONCLUSIVE"
    elif all(item == "NA" for item in statuses):
        status = "NA" if empty == "NA" else "INCONCLUSIVE"
    else:
        status = "PASS"
    return {
        "status": status,
        "passed": sum(item == "PASS" for item in statuses),
        "total": len(statuses),
        "checks": checks,
    }


def _not_applicable(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Keep diagnostic rows while excluding a non-required group from gating."""

    checks = [dict(row) for row in rows if isinstance(row, Mapping)]
    statuses = [_status_of(row) for row in checks]
    return {
        "status": "NA",
        "passed": sum(item == "PASS" for item in statuses),
        "total": len(statuses),
        "checks": checks,
    }


def _ratio(component: Mapping[str, Any]) -> float:
    total = component.get("total", 0)
    passed = component.get("passed", 0)
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or isinstance(passed, bool)
        or not isinstance(passed, int)
        or total <= 0
    ):
        return 0.0
    return max(0.0, min(1.0, passed / total))


def evaluate_migration_units(
    units: Sequence[Mapping[str, Any]],
    unit_checks: Mapping[str, Any] | None,
    observations: Sequence[Mapping[str, Any]],
    ordinary_residue_hits: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Attach observable evidence to every declared migration unit."""

    by_id = {
        str(row.get("id")): row
        for row in observations
        if isinstance(row, Mapping) and row.get("id")
    }
    mapping = unit_checks if isinstance(unit_checks, Mapping) else {}
    residue_hits = [hit for hit in ordinary_residue_hits if isinstance(hit, Mapping)]
    results: list[dict[str, Any]] = []
    assigned_hits: set[int] = set()

    for unit in units:
        raw_unit = unit if isinstance(unit, Mapping) else {}
        unit_id = str(raw_unit.get("id") or raw_unit.get("unit_id") or "")
        configured = mapping.get(unit_id)
        if configured is None:
            configured = raw_unit.get("observation_ids", [])
        refs = (
            configured.get("observation_ids", [])
            if isinstance(configured, Mapping)
            else configured
        )
        refs = (
            [str(item) for item in refs]
            if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes))
            else []
        )
        checks = [by_id[item] for item in refs if item in by_id]
        missing = [item for item in refs if item not in by_id]

        locators: list[object] = []
        for field in ("source_locators", "target_locators"):
            value = raw_unit.get(field, [])
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                locators.extend(value)

        unit_hits: list[Mapping[str, Any]] = []
        for index, hit in enumerate(residue_hits):
            hit_path = str(hit.get("path", ""))
            if any(
                isinstance(locator, Mapping)
                and (
                    hit_path == str(locator.get("path", "")).strip("/")
                    or hit_path.startswith(
                        str(locator.get("path", "")).strip("/") + "/"
                    )
                )
                for locator in locators
            ):
                unit_hits.append(hit)
                assigned_hits.add(index)

        if not unit_id or not refs or missing:
            status = "INCONCLUSIVE"
        elif any(_status_of(row) not in _KNOWN_STATUSES for row in checks):
            status = "INFRA_ERROR"
        elif any(_status_of(row) == "INFRA_ERROR" for row in checks):
            status = "INFRA_ERROR"
        elif unit_hits or any(
            _status_of(row) in {"FAIL", "INCONCLUSIVE", "NA"}
            for row in checks
        ):
            status = "FAIL"
        else:
            status = "PASS"

        results.append(
            {
                **dict(raw_unit),
                "id": unit_id,
                "status": status,
                "observation_ids": refs,
                "checks": [dict(row) for row in checks],
                "missing_observation_ids": missing,
                "ordinary_residue_hits": [dict(row) for row in unit_hits],
            }
        )

    unassigned = [
        dict(hit)
        for index, hit in enumerate(residue_hits)
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


def _required(
    applicability: Mapping[str, Any] | None,
    group: str,
    default: bool,
) -> bool:
    if not isinstance(applicability, Mapping) or group not in applicability:
        return default
    entry = applicability.get(group)
    if isinstance(entry, Mapping):
        return str(entry.get("status", "")).casefold() == "required"
    return str(entry).casefold() == "required"


def _short_circuit_component(
    component: Mapping[str, Any] | None,
    *,
    empty: Mapping[str, Any],
    status: str,
) -> dict[str, Any]:
    if not isinstance(component, Mapping):
        return dict(empty)
    value = dict(component)
    checks = value.get("checks")
    if isinstance(checks, list) and checks:
        statuses = [
            str(row.get("status", "")).upper()
            for row in checks
            if isinstance(row, Mapping)
        ]
        if statuses and all(item == "NA" for item in statuses):
            # The group was not executed.  Preserve that fact instead of
            # letting the required-group default turn it into "inconclusive".
            value["status"] = "NA"
    value["short_circuited_by_hardgate"] = status
    return value


def _hardgate_short_circuit(
    status: str,
    *,
    cheating: bool = False,
    components: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized = str(status).upper()
    component_status = (
        "FAIL"
        if normalized == "FAIL"
        else "INFRA_ERROR"
        if normalized == "INFRA_ERROR"
        else "INCONCLUSIVE"
    )
    empty = {
        "status": component_status,
        "passed": 0,
        "total": 0,
        "checks": [],
        "short_circuited_by_hardgate": normalized,
    }
    resolved = components if isinstance(components, Mapping) else {}
    scope = _short_circuit_component(
        resolved.get("migration_scope_complete"), empty=empty, status=normalized
    )
    p2p = _short_circuit_component(
        resolved.get("p2p"), empty=empty, status=normalized
    )
    f2p = _short_circuit_component(
        resolved.get("f2p"), empty=empty, status=normalized
    )
    differential = _short_circuit_component(
        resolved.get("differential"), empty=empty, status=normalized
    )
    state = _short_circuit_component(
        resolved.get("required_state_preservation_complete"),
        empty=empty,
        status=normalized,
    )
    regression_status = (
        str(f2p.get("status", component_status))
        if f2p.get("total", 0) or f2p.get("checks")
        else component_status
    )
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": (
            "failed"
            if normalized == "FAIL"
            else "infra_error"
            if normalized == "INFRA_ERROR"
            else "inconclusive"
        ),
        "hardgate": normalized,
        "cheating": bool(cheating),
        "components": {
            "migration_scope_complete": scope,
            "regression_test": {
                "status": regression_status,
                "p2p": p2p,
                "f2p": f2p,
                "differential": differential,
                "short_circuited_by_hardgate": normalized,
            },
            "required_state_preservation_complete": state,
        },
        "test_progress": None,
        "eligible_for_graded": False,
        "evidence_index": (
            list(resolved.get("evidence_index", []))
            if isinstance(resolved.get("evidence_index"), Sequence)
            and not isinstance(resolved.get("evidence_index"), (str, bytes))
            else []
        ),
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
) -> dict[str, Any]:
    """Evaluate scope, F2P, and conditional state preservation after hardgate."""

    del weights
    hardgate_status = str(hardgate.get("status", "INCONCLUSIVE")).upper()
    hardgate_cheating = bool(hardgate.get("cheating"))

    state_mapping = state if isinstance(state, Mapping) else {}
    state_rows = [
        row
        for row in state_mapping.get("checks", [])
        if isinstance(row, Mapping)
    ]
    all_observations = [
        *[row for row in p2p if isinstance(row, Mapping)],
        *[row for row in f2p if isinstance(row, Mapping)],
        *[row for row in differential if isinstance(row, Mapping)],
        *state_rows,
    ]
    residue = hardgate.get("ordinary_residue_hits", [])
    residue = (
        [row for row in residue if isinstance(row, Mapping)]
        if isinstance(residue, Sequence) and not isinstance(residue, (str, bytes))
        else []
    )
    scoped_units = evaluate_migration_units(
        migration_units,
        unit_checks,
        all_observations,
        residue,
    )
    scope = _component(scoped_units)

    p2p_rows = [row for row in p2p if isinstance(row, Mapping)]
    f2p_rows = [row for row in f2p if isinstance(row, Mapping)]
    differential_rows = [row for row in differential if isinstance(row, Mapping)]
    p2p_required = _required(applicability, "P2P", True)
    f2p_required = _required(applicability, "F2P", True)
    differential_required = _required(applicability, "DIFFERENTIAL", False)
    p2p_result = _component(p2p_rows) if p2p_required else _not_applicable(p2p_rows)
    f2p_result = _component(f2p_rows) if f2p_required else _not_applicable(f2p_rows)
    diff_result = (
        _component(differential_rows)
        if differential_required
        else _not_applicable(differential_rows)
    )

    state_required = bool(state_mapping.get("applicable")) and _required(
        applicability, "STATE", True
    )
    state_result = _component(state_rows) if state_required else _not_applicable(state_rows)

    if hardgate_status != "PASS" or hardgate_cheating:
        # Preserve the real per-group diagnostics (for example, the actual
        # 47/48 P2P ratio) while refusing to produce a verifier score.  A
        # short-circuited run must not be reported as if no test ever ran.
        return _hardgate_short_circuit(
            "FAIL" if hardgate_cheating and hardgate_status == "PASS" else hardgate_status,
            cheating=hardgate_cheating,
            components={
                "migration_scope_complete": scope,
                "p2p": p2p_result,
                "f2p": f2p_result,
                "differential": diff_result,
                "required_state_preservation_complete": state_result,
                "evidence_index": hardgate.get("evidence_index", []),
            },
        )

    # P2P is reported for diagnostics only. It is already a hard-gate input
    # and must not block verifier or contribute to T.
    regression_status = f2p_result["status"] if f2p_required else "NA"

    score_components: dict[str, dict[str, Any]] = {}
    for name, component in (
        ("migration_scope_complete", scope),
        ("F2P", f2p_result),
        ("required_state_preservation_complete", state_result),
    ):
        if component["status"] == "NA":
            continue
        score_components[name] = {
            "score": round(_ratio(component), 7),
            "passed": component["passed"],
            "total": component["total"],
        }

    scores = [item["score"] for item in score_components.values()]
    # T is the mean score across applicable verifier components. P2P is
    # intentionally excluded because it is already a binary hard-gate input.
    progress = sum(scores) / len(scores) if scores else 0.0
    passed_progress = sum(item["passed"] for item in score_components.values())
    total_progress = sum(item["total"] for item in score_components.values())

    blockers = [scope["status"], f2p_result["status"], state_result["status"]]
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
        "components": {
            "migration_scope_complete": scope,
            "regression_test": {
                "status": regression_status,
                "p2p": p2p_result,
                "f2p": f2p_result,
                "differential": diff_result,
            },
            "required_state_preservation_complete": state_result,
        },
        "test_progress": {
            "T": round(max(0.0, min(1.0, progress)), 7),
            "passed": passed_progress,
            "total": total_progress,
            "score_components": score_components,
        },
        "eligible_for_graded": status == "success",
        "evidence_index": (
            list(hardgate.get("evidence_index", []))
            if isinstance(hardgate.get("evidence_index"), Sequence)
            and not isinstance(hardgate.get("evidence_index"), (str, bytes))
            else []
        ),
    }
