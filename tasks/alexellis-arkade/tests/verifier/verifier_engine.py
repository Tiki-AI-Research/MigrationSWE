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


# A unit's locator list mixes the migrated source files and the target
# framework's dependency surface.  Only the latter proves that the real target
# framework is on the runtime path, so only it is required to exist: the
# dependency manifests and the vendored framework sources (whose content the API
# markers below then identify).
#
# The migrated *source* files are deliberately not required by name.  The task
# prescribes the migration's behaviour and its public contract, not its file
# layout: ``instruction.md`` never names ``cmd/kong.go`` / ``cmd/kong_test.go``
# (``cmd/apps_catalog.go`` only appears as part of the Cobra exception set, and
# does not exist at the base commit), and the case's own F2P batch declares that
# the reference implementation's "file names, struct names and call sites ...
# are never asserted".  Requiring them would judge the layout rather than the
# migration and would fail a complete migration that mounts the Kong root
# elsewhere (r12 harbor V1).
_REQUIRED_LOCATOR_ROOTS = ("vendor/",)
_REQUIRED_LOCATOR_EXACT = {"go.mod", "go.sum"}
_TARGET_DEPENDENCY_REPLACEMENT = re.compile(
    r"(?m)^\s*replace\s+(github\.com/alecthomas/kong)\b"
)


def _required_locator(entry: Mapping[str, Any]) -> bool:
    """Is this locator part of the *target technology* rather than the adapter?"""
    path = str(entry.get("path", "")).strip("/")
    return path.startswith(_REQUIRED_LOCATOR_ROOTS) or path in _REQUIRED_LOCATOR_EXACT


def _unit_api_markers(unit: Mapping[str, Any]) -> list[str]:
    """The API declarations that identify the target framework for this unit."""
    markers = unit.get("target_api_markers")
    if not isinstance(markers, Sequence) or isinstance(markers, (str, bytes)):
        return []
    return [str(item) for item in markers if str(item).strip()]


def _target_locator_issues(
    unit: Mapping[str, Any], candidate_root: Path | None
) -> tuple[list[str], list[str]]:
    """Target framework artefacts the unit declares but the candidate omits or forges.

    ``migration-units.json`` pins the real target dependency surface: the
    vendored framework sources (with their content digest) and the dependency
    manifests.  A unit is only complete when they are present *and* carry the
    declared content; otherwise a candidate that re-implements the target API
    under the same import path (a local ``replace`` to a look-alike package, or
    a tree of ``placeholder`` files with the right names) scores full scope
    while shipping none of the target framework.
    """
    if candidate_root is None:
        return [], []
    locators = unit.get("target_locators")
    if not isinstance(locators, Sequence) or isinstance(locators, (str, bytes)):
        return [], []
    missing: list[str] = []
    invalid: list[str] = []
    sources: list[str] = []
    manifests = {
        str(item.get("path", ""))
        for item in locators
        if isinstance(item, Mapping)
    }
    if "go.mod" in manifests:
        try:
            gomod = (candidate_root / "go.mod").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            gomod = ""
        # A ``replace`` of the target framework by a local path keeps the import
        # path and the module metadata in the binary while shipping a look-alike
        # package; the unit is then not migrated onto the target technology.
        for match in _TARGET_DEPENDENCY_REPLACEMENT.finditer(gomod):
            missing.append(f"replace:{match.group(1)}")
    for locator in locators:
        if not isinstance(locator, Mapping) or not _required_locator(locator):
            continue
        path = str(locator.get("path", "")).strip("/")
        if not path:
            continue
        target = candidate_root / Path(path)
        if target.is_symlink() or not target.is_file():
            missing.append(path)
            continue
        try:
            sources.append(target.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            invalid.append(path)
    # Existence is not identity: a tree of ``placeholder`` files keeps the
    # import path working while shipping none of the target framework.  The
    # framework is identified by the API surface the unit declares, which is
    # what separates a real Kong tree (any 1.8.x vendoring) from a look-alike.
    joined = "\n".join(sources)
    for marker in _unit_api_markers(unit):
        if marker not in joined:
            invalid.append(f"api:{marker}")
    return missing, invalid


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
        absent, invalid = _target_locator_issues(unit, candidate_root)
        blocked = inspected.status in {"INFRA_ERROR", "INCONCLUSIVE"}
        status = (
            "FAIL" if (unit_hits or absent or invalid) and not blocked
            else inspected.status
        )
        results.append({
            **dict(unit),
            "id": unit_id,
            "status": status,
            "observation_ids": refs,
            "checks": checks,
            "missing_observation_ids": missing,
            "missing_target_locators": absent,
            "invalid_target_locators": invalid,
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


def _component_ratio(component: Mapping[str, Any] | None) -> float | None:
    """Return ``passed / total`` for a component, or None when unmeasured."""
    if not isinstance(component, Mapping):
        return None
    total = component.get("total")
    passed = component.get("passed")
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
        if isinstance(passed, int) and not isinstance(passed, bool):
            return passed / total
    return None


def _hardgate_short_circuit(
    status: str, *, cheating: bool = False, diagnostics: Mapping[str, Any] | None = None
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
    diagnostics = dict(diagnostics or {})
    components = {
        "migration_scope_complete": dict(empty),
        "regression_test": {
            **empty,
            "p2p": dict(empty),
            "f2p": dict(empty),
            "differential": dict(empty),
        },
        "required_state_preservation_complete": dict(empty),
    }
    # Admission and diagnostics are separate surfaces.  ``status`` stays the
    # admission verdict (the hard gate rejected the run, so nothing is scored),
    # while ``observed_status``/``passed``/``total`` keep the observations that
    # were already collected.  Without this the report shows 0/0 for every
    # component and the root reward loses the real F2P/P2P/DIFFERENTIAL ratios.
    def _attach(target: dict[str, Any], row: Mapping[str, Any]) -> None:
        target["observed_status"] = row.get("status")
        passed = row.get("passed")
        total = row.get("total")
        if isinstance(passed, int) and not isinstance(passed, bool):
            target["passed"] = passed
        if isinstance(total, int) and not isinstance(total, bool):
            target["total"] = total

    observed = diagnostics.get("observed_components")
    if isinstance(observed, Mapping):
        for name in ("migration_scope_complete", "required_state_preservation_complete"):
            row = observed.get(name)
            if isinstance(row, Mapping):
                _attach(components[name], row)
        regression = observed.get("regression_test")
        if isinstance(regression, Mapping):
            for name in ("p2p", "f2p", "differential"):
                row = regression.get(name)
                if isinstance(row, Mapping):
                    _attach(components["regression_test"][name], row)
    ratios = diagnostics.get("observed_ratios")
    ratios = ratios if isinstance(ratios, Mapping) else {}
    applicability = diagnostics.get("state_applicability")
    if applicability not in {"required", "na", "undetermined"}:
        applicability = "undetermined"
    result = {
        "schema_version": "migration-verifier-result:v2",
        "status": (
            "failed" if status == "FAIL"
            else "infra_error" if status == "INFRA_ERROR"
            else "inconclusive"
        ),
        "hardgate": status,
        "components": components,
        "test_progress": None,
        "progress": {
            "scope_ratio": ratios.get("scope_ratio"),
            "f2p_ratio": ratios.get("f2p_ratio"),
            "state_ratio": ratios.get("state_ratio"),
            "T": None,
            # A hard-gate failure yields no measurable progress: the canonical
            # progress contract still requires the explicit eligibility flag and
            # applicability value, otherwise reward calculation aborts and the
            # legitimate rejection is mis-reported as an infrastructure error.
            # The ratios are diagnostics: ``eligible=False`` and ``T=None`` keep
            # the admission conclusion, so they never contribute to the reward.
            "eligible": False,
            "errors": ["hard gate did not pass"],
            "state_applicability": applicability,
        },
        "evidence_index": [],
    }
    if diagnostics:
        result["diagnostics"] = diagnostics
    result["reward"] = calculate_verifier_reward(
        hardgate={"status": status, "cheating": cheating},
        verifier=result,
    )
    return result


def _state_result(
    state: Mapping[str, Any], applicability: Mapping[str, Any] | None
) -> tuple[dict[str, Any], str, str | None, bool]:
    """Return the STATE component plus its resolved applicability.

    Shared by the scoring path and the hard-gate diagnostic path so both report
    the same evidence; only the scoring path folds the result into ``T``.
    """
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
    return state_result, state_applicability, state_reason, state_applicable


def _observed_components(
    *,
    hardgate: Mapping[str, Any],
    migration_units: Sequence[Mapping[str, Any]],
    p2p: Sequence[Mapping[str, Any]],
    f2p: Sequence[Mapping[str, Any]],
    differential: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    unit_checks: Mapping[str, Any] | None,
    applicability: Mapping[str, Any] | None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """Component evidence observed before the hard gate rejected the run."""

    diagnostic_ids = {str(row.get("id")) for row in differential}
    diagnostic_ids |= {str(row.get("id")) for row in f2p}
    scoped_checks = {
        str(key): {"observation_ids": [str(identifier) for identifier in (
            value.get("observation_ids", []) if isinstance(value, Mapping) else value
        ) if str(identifier) not in diagnostic_ids]}
        for key, value in (unit_checks or {}).items()
    }
    all_observations = [*p2p, *f2p, *list(state.get("checks", []))]
    ordinary = hardgate.get("ordinary_residue_hits", [])
    ordinary = ordinary if isinstance(ordinary, Sequence) and not isinstance(ordinary, (str, bytes)) else []
    scope = _component(evaluate_migration_units(
        migration_units, scoped_checks, all_observations, ordinary, candidate_root
    ))
    p2p_result = _component(p2p) if _required(applicability, "P2P", True) else _component(p2p, empty="NA")
    f2p_result = _component(f2p) if _required(applicability, "F2P", True) else _component(f2p, empty="NA")
    diff_result = _component(differential) if _required(applicability, "DIFFERENTIAL", True) else _component(differential, empty="NA")
    state_result, _applicability, _reason, _applicable = _state_result(state, applicability)
    return {
        "migration_scope_complete": scope,
        "regression_test": {
            "p2p": p2p_result,
            "f2p": f2p_result,
            "differential": diff_result,
        },
        "required_state_preservation_complete": state_result,
    }


def validate_weights(weights: object) -> None:
    """The verifier has one scoring dimension; any other weighting is invalid.

    A configuration that still declares objective/rubric weights (or any other
    split) silently changes what the reward means, so it must fail loudly
    instead of being normalised away.
    """
    if not isinstance(weights, Mapping):
        raise ValueError("verifier weights must be an object")
    try:
        normalized = {str(key): float(value) for key, value in weights.items()}
    except (TypeError, ValueError) as error:
        raise ValueError("verifier weights must be numeric") from error
    if normalized != {"T": 1.0}:
        raise ValueError(
            "verifier weights must be exactly {'T': 1.0}, got "
            f"{dict(sorted(normalized.items()))}"
        )


def evaluate_verifier(*, hardgate: Mapping[str, Any], migration_units: Sequence[Mapping[str, Any]], p2p: Sequence[Mapping[str, Any]], f2p: Sequence[Mapping[str, Any]], differential: Sequence[Mapping[str, Any]], state: Mapping[str, Any], weights: Mapping[str, float], unit_checks: Mapping[str, Any] | None = None, applicability: Mapping[str, Any] | None = None, candidate_root: Path | None = None) -> dict[str, Any]:
    validate_weights(weights)
    if hardgate.get("status") != "PASS":
        observed = _observed_components(
            hardgate=hardgate,
            migration_units=migration_units,
            p2p=p2p,
            f2p=f2p,
            differential=differential,
            state=state,
            unit_checks=unit_checks,
            applicability=applicability,
            candidate_root=candidate_root,
        )
        regression = observed["regression_test"]
        state_applicability = _state_applicability(applicability)[0]
        return _hardgate_short_circuit(
            str(hardgate.get("status", "INCONCLUSIVE")),
            cheating=bool(hardgate.get("cheating")),
            diagnostics={
                "admission": "hard_gate_failed",
                "note": (
                    "component status is the admission verdict; observed_status, "
                    "passed/total and the ratios are diagnostics only and never "
                    "change eligibility, T or the reward branch"
                ),
                "state_applicability": state_applicability,
                "observed_components": observed,
                "observed_ratios": {
                    "scope_ratio": _component_ratio(observed["migration_scope_complete"]),
                    "f2p_ratio": _component_ratio(regression["f2p"]),
                    "p2p_ratio": _component_ratio(regression["p2p"]),
                    "differential_ratio": _component_ratio(regression["differential"]),
                    "state_ratio": (
                        _component_ratio(observed["required_state_preservation_complete"])
                        if state_applicability == "required" else None
                    ),
                },
            },
        )
    # ``migration_scope_complete`` measures whether a migration unit exists and
    # works; ``f2p_pass_ratio`` measures the frozen behaviour probes on their
    # own.  A unit's check list may reference either kind, but a probe that
    # belongs to the F2P (or DIFFERENTIAL) component must not be counted again
    # by the scope component: one failing probe would otherwise be scored twice.
    # Both diagnostic families stay in ``all_observations`` so their own
    # component still sees them.
    diagnostic_ids = {str(row.get("id")) for row in differential}
    diagnostic_ids |= {str(row.get("id")) for row in f2p}
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
        candidate_root,
    )
    scope = _component(scoped_units)
    p2p_result = _component(p2p) if _required(applicability, "P2P", True) else _component(p2p, empty="NA")
    f2p_result = _component(f2p) if _required(applicability, "F2P", True) else _component(f2p, empty="NA")
    diff_result = _component(differential) if _required(applicability, "DIFFERENTIAL", True) else _component(differential, empty="NA")
    regression_statuses = [
        row["status"] for row in (p2p_result, f2p_result, diff_result)
        if row["status"] != "NA"
    ]
    regression_status = "INFRA_ERROR" if "INFRA_ERROR" in regression_statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in regression_statuses else "FAIL" if "FAIL" in regression_statuses else "PASS"
    state_result, state_applicability, _state_reason, state_applicable = _state_result(
        state, applicability
    )
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
    result = {"schema_version": "migration-verifier-result:v2", "status": status, "hardgate": "PASS", "components": {"migration_scope_complete": scope, "regression_test": {"status": regression_status, "p2p": p2p_result, "f2p": f2p_result, "differential": diff_result}, "required_state_preservation_complete": state_result}, "progress": progress, "test_progress": {"T": progress["T"]} if progress["T"] is not None else None, "evidence_index": [], "diagnostics": {"p2p": p2p_result, "differential": diff_result}}
    result["reward"] = calculate_verifier_reward(
        hardgate=hardgate,
        verifier=result,
    )
    return result
