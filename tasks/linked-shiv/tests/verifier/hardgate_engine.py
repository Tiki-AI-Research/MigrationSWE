"""Deterministic Migration hard-gate engine.

The engine only aggregates derived evidence. It does not inspect candidate
source files or infer test outcomes from untrusted scalar policy values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


ATOM_STATUSES = frozenset(
    {"PASS", "FAIL", "NA", "INCONCLUSIVE", "INFRA_ERROR", "TIMEOUT"}
)

COMPOUND_ATOMS = {
    "candidate_valid": ("patch_applicable",),
    "target_build_valid": ("build_pass", "dependency_resolved"),
    "migration_integrity_preserved": (
        "no_deceptive_source_fallback",
        "public_contract_preserved",
        "critical_data_preserved",
    ),
    "p2p_regression_preserved": ("p2p_no_regression",),
    "test_integrity": ("tests_not_tampered", "no_new_skip_or_xfail"),
    "evaluation_environment_integrity": (
        "protected_paths_unchanged",
        "anti_cheating_pass",
    ),
}

_L1_CHEAT_ATOMS = (
    # Test-tree tampering and protected/report tampering are L1 anti-cheating
    # failures: they change what the evaluator can observe rather than merely
    # leaving the migration incomplete.  A candidate's own newly added skip
    # marker is not in this set: it is an ordinary candidate failure that the
    # hard gate rejects with reward 0, never the cheating penalty of -1.
    "protected_paths_unchanged",
    "anti_cheating_pass",
    "tests_not_tampered",
)


def _atom(
    status: str,
    evidence: Any = None,
    *,
    source: str = "derived-evidence",
    applicable: bool | None = None,
) -> dict[str, Any]:
    normalized = str(status).upper()
    if normalized == "TIMEOUT":
        normalized = "INFRA_ERROR"
    if normalized not in ATOM_STATUSES - {"TIMEOUT"}:
        normalized = "INFRA_ERROR"
    return {
        "status": normalized,
        "applicable": normalized != "NA" if applicable is None else bool(applicable),
        "source": source,
        "evidence": evidence if evidence is not None else [],
    }


def _evidence_atom(
    value: object,
    *,
    default: str = "INCONCLUSIVE",
) -> dict[str, Any]:
    if value is None:
        return _atom(default)
    if not isinstance(value, Mapping) or not isinstance(value.get("status"), str):
        raise ValueError(
            "Migration Grader v6 hardgate atoms require structured derived evidence"
        )

    raw_status = str(value["status"]).upper()
    if raw_status not in ATOM_STATUSES:
        raise ValueError(f"invalid hardgate atom status: {value['status']!r}")
    applicable = value.get("applicable")
    if applicable is not None and not isinstance(applicable, bool):
        raise ValueError("hardgate atom applicable must be boolean when provided")
    if raw_status == "NA":
        applicable = False
    return _atom(
        raw_status,
        source=str(value.get("source", "derived-evidence")),
        evidence=value.get("evidence", []),
        applicable=applicable,
    )


def _as_list(value: object) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _compound_status(
    values: Mapping[str, Mapping[str, Any]],
    atoms: tuple[str, ...],
) -> tuple[str, bool]:
    applicable = [values[atom] for atom in atoms if values[atom]["applicable"]]
    if not applicable:
        return "NA", False
    statuses = [str(atom["status"]) for atom in applicable]
    if "INFRA_ERROR" in statuses:
        return "INFRA_ERROR", True
    if "FAIL" in statuses:
        return "FAIL", True
    if "INCONCLUSIVE" in statuses:
        return "INCONCLUSIVE", True
    return "PASS", True


def evaluate_hardgate(candidate_root: Path, policy: Mapping[str, Any]) -> dict[str, Any]:
    """Aggregate derived atomic evidence into the six hard gates.

    ``candidate_root`` is intentionally unused: candidate inspection belongs
    to evidence derivation, keeping this function deterministic and auditable.
    """

    del candidate_root
    if policy.get("evidence_mode") != "derived":
        raise ValueError("Migration Grader v6 hardgate requires derived evidence")

    values = {
        atom: _evidence_atom(
            policy.get(atom),
            default="NA" if atom == "critical_data_preserved" else "INCONCLUSIVE",
        )
        for atoms in COMPOUND_ATOMS.values()
        for atom in atoms
    }

    deceptive_hits = _as_list(policy.get("deceptive_fallback_hits"))
    source_fallback = (
        values["no_deceptive_source_fallback"]["applicable"]
        and values["no_deceptive_source_fallback"]["status"] == "FAIL"
    ) or bool(deceptive_hits)
    l1_failure = any(
        values[atom]["applicable"] and values[atom]["status"] == "FAIL"
        for atom in _L1_CHEAT_ATOMS
    )
    cheating = source_fallback or l1_failure or policy.get("cheating") is True
    cheat_level = "L2" if source_fallback else "L1" if cheating else "L0"

    compounds: dict[str, Any] = {}
    for name, atoms in COMPOUND_ATOMS.items():
        status, applicable = _compound_status(values, atoms)
        compounds[name] = {
            "status": status,
            "applicable": applicable,
            "atoms": {atom: values[atom] for atom in atoms},
        }

    applicable_compounds = [
        row for row in compounds.values() if row["applicable"]
    ]
    compound_statuses = [str(row["status"]) for row in applicable_compounds]
    if cheating:
        # A confirmed L1/L2 violation is a candidate failure even when another
        # gate could not be evaluated (for example, because the candidate
        # blocked the observation report).  Letting an infrastructure status win
        # would turn a known cheat into reward 0 instead of the mandated -1.
        overall = "FAIL"
    elif "INFRA_ERROR" in compound_statuses:
        overall = "INFRA_ERROR"
    elif "FAIL" in compound_statuses:
        overall = "FAIL"
    elif "INCONCLUSIVE" in compound_statuses:
        overall = "INCONCLUSIVE"
    else:
        overall = "PASS"

    return {
        "schema_version": "migration-hardgate-result:v2",
        "status": overall,
        "cheating": cheating,
        "cheat_level": cheat_level,
        "reward_override": -1 if cheating else 0 if overall == "FAIL" else None,
        "compound_gates": compounds,
        "automated_evidence": values,
        "evidence_index": _as_list(policy.get("source_hits")),
        "ordinary_residue_hits": _as_list(policy.get("ordinary_residue_hits")),
        "deceptive_fallback_hits": deceptive_hits,
    }
