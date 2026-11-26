"""Stable Migration verifier aggregation engine."""
from __future__ import annotations

import re
from pathlib import Path
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


def _missing_target_locators(
    unit: Mapping[str, Any], candidate_root: Path | None
) -> list[str]:
    """Non-test target files a migration unit declares but the candidate lacks.

    A unit is only migrated when its declared target production locators exist;
    observations alone cannot prove a module was migrated because the candidate
    supplies them.  Test locators are excluded: candidate obligations must be
    source-derived and a legitimate candidate need not reproduce the reference
    solution's own test files.
    """

    if candidate_root is None:
        return []
    locators = unit.get("target_locators")
    if not isinstance(locators, Sequence) or isinstance(locators, (str, bytes)):
        return []
    missing: list[str] = []
    for locator in locators:
        if not isinstance(locator, Mapping):
            continue
        path = str(locator.get("path", "")).strip("/")
        if not path or path.endswith("_test.go"):
            continue
        if not (candidate_root / path).is_file():
            missing.append(path)
    return missing


_GO_TOP_LEVEL_DECL = re.compile(
    r"(?m)^[ \t]*(?:"
    r"func\s+(?:\([^)\n]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)"
    r"|(?:type|const|var)\s+([A-Za-z_][A-Za-z0-9_]*)"
    r"|(?:type|const|var)\s*(\()"
    r")"
)


def _group_binds_named_spec(text: str, open_index: int) -> bool:
    """True when a ``type/const/var (`` group binds at least one real name.

    ``var ()``/``const ()``/``type ()`` and groups that only hold blank ``_``
    specs compile but declare nothing, so they must not count as migrated
    declarations.  The group body is scanned to its matching close paren so the
    placeholder form cannot be swapped for its empty-group equivalent.
    """

    depth = 0
    index = open_index
    length = len(text)
    body_start = open_index + 1
    while index < length:
        character = text[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                body = text[body_start:index]
                for match in re.finditer(
                    r"(?m)(?:^|;)[ \t]*([A-Za-z_][A-Za-z0-9_]*)", body
                ):
                    if match.group(1) != "_":
                        return True
                return False
        index += 1
    return False


def _has_substantive_declaration(text: str) -> bool:
    """True when a Go file declares at least one real (non-blank) symbol.

    A placeholder such as ``var _ = 0`` or an empty declaration group
    (``var ()``/``const ()``/``type ()``) satisfies a naive declaration-keyword
    check while implementing nothing, so neither the blank identifier, blank
    ``func _``/``type _`` forms, nor empty/blank-only groups count.  A non-empty
    group counts only when it binds at least one named spec.
    """

    for match in _GO_TOP_LEVEL_DECL.finditer(text):
        if match.group(3) is not None:
            if _group_binds_named_spec(text, match.start(3)):
                return True
            continue
        name = match.group(1) or match.group(2)
        if name and name != "_":
            return True
    return False


def _superficial_target_locators(
    unit: Mapping[str, Any], candidate_root: Path | None
) -> list[str]:
    """Non-test Go targets that exist but carry no migrated declarations.

    Existence alone cannot prove a unit was migrated: a placeholder that only
    declares its package -- or a blank ``var _ = 0`` -- satisfies ``is_file``
    while implementing nothing.  A Go target must therefore carry at least one
    substantive top-level declaration.
    """

    if candidate_root is None:
        return []
    locators = unit.get("target_locators")
    if not isinstance(locators, Sequence) or isinstance(locators, (str, bytes)):
        return []
    superficial: list[str] = []
    for locator in locators:
        if not isinstance(locator, Mapping):
            continue
        path = str(locator.get("path", "")).strip("/")
        if not path or path.endswith("_test.go") or not path.endswith(".go"):
            continue
        target = candidate_root / path
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        stripped = re.sub(r"(?m)//.*$", "", stripped)
        if not _has_substantive_declaration(stripped):
            superficial.append(path)
    return superficial


def evaluate_migration_units(
    units: Sequence[Mapping[str, Any]],
    unit_checks: Mapping[str, Any] | None,
    observations: Sequence[Mapping[str, Any]],
    ordinary_residue_hits: Sequence[Mapping[str, Any]] = (),
    candidate_root: Path | None = None,
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
        missing_target_locators = _missing_target_locators(unit, candidate_root)
        superficial_target_locators = _superficial_target_locators(unit, candidate_root)
        if missing_target_locators or superficial_target_locators:
            status = "FAIL"
        results.append({
            **dict(unit),
            "id": unit_id,
            "status": status,
            "observation_ids": refs,
            "checks": checks,
            "missing_observation_ids": missing,
            "missing_target_locators": missing_target_locators,
            "superficial_target_locators": superficial_target_locators,
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
    migration_units: Sequence[Mapping[str, Any]] = (),
    unit_checks: Mapping[str, Any] | None = None,
    observations: Sequence[Mapping[str, Any]] = (),
    candidate_root: Path | None = None,
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
    # Migration scope is a verifier-level completion signal: it must be computed
    # and reported even when the hard gate fails, so a scope/locator failure is
    # never silently collapsed into the hard-gate branch.  Reward is unaffected
    # (the hard-gate branch owns it).
    scope_component = dict(empty)
    scope_ratio: float | None = None
    if migration_units:
        try:
            scoped_units = evaluate_migration_units(
                migration_units,
                unit_checks,
                observations,
                (),
                candidate_root=candidate_root,
            )
            scope_component = _component(scoped_units)
            scope_component["short_circuited_by_hardgate"] = status
            total = scope_component.get("total")
            passed = scope_component.get("passed")
            if isinstance(total, int) and total > 0 and isinstance(passed, int):
                scope_ratio = passed / total
        except Exception:
            scope_component = dict(empty)
            scope_ratio = None
    result = {
        "schema_version": "migration-verifier-result:v2",
        "status": (
            "failed" if status == "FAIL"
            else "infra_error" if status == "INFRA_ERROR"
            else "inconclusive"
        ),
        "hardgate": status,
        "components": {
            "migration_scope_complete": scope_component,
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
            "scope_ratio": scope_ratio,
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


def _without_diagnostic_refs(
    unit_checks: Mapping[str, Any] | None,
    differential: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Drop DIFFERENTIAL observation ids from unit observation references.

    Differential observations are reported in their own component and are not
    part of the P2P/F2P/STATE observation stream the scope check inspects, so a
    unit that merely lists a DIFF evidence id must not be scored as if that
    observation were missing.  The hard-gate short-circuit and the normal path
    must apply the identical exclusion or scope coverage is systematically
    under-reported whenever the hard gate fails.
    """

    diagnostic_ids = {str(row.get("id")) for row in differential}
    return {
        str(key): {"observation_ids": [str(identifier) for identifier in (
            value.get("observation_ids", []) if isinstance(value, Mapping) else value
        ) if str(identifier) not in diagnostic_ids]}
        for key, value in (unit_checks or {}).items()
    }


def evaluate_verifier(*, hardgate: Mapping[str, Any], migration_units: Sequence[Mapping[str, Any]], p2p: Sequence[Mapping[str, Any]], f2p: Sequence[Mapping[str, Any]], differential: Sequence[Mapping[str, Any]], state: Mapping[str, Any], weights: Mapping[str, float], unit_checks: Mapping[str, Any] | None = None, applicability: Mapping[str, Any] | None = None, candidate_root: Path | None = None) -> dict[str, Any]:
    del weights
    unit_checks = _without_diagnostic_refs(unit_checks, differential)
    if hardgate.get("status") != "PASS":
        return _hardgate_short_circuit(
            str(hardgate.get("status", "INCONCLUSIVE")),
            cheating=bool(hardgate.get("cheating")),
            hardgate=hardgate,
            migration_units=migration_units,
            unit_checks=unit_checks,
            observations=[*p2p, *f2p, *list(state.get("checks", []))],
            candidate_root=candidate_root,
        )
    all_observations = [*p2p, *f2p, *list(state.get("checks", []))]
    ordinary_residue_hits = hardgate.get("ordinary_residue_hits", [])
    ordinary_residue_hits = ordinary_residue_hits if isinstance(ordinary_residue_hits, Sequence) and not isinstance(ordinary_residue_hits, (str, bytes)) else []
    scoped_units = evaluate_migration_units(
        migration_units,
        unit_checks,
        all_observations,
        ordinary_residue_hits,
        candidate_root=candidate_root,
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
