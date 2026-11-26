"""Deterministic Migration hard-gate engine."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

COMPOUND_ATOMS = {
    "candidate_valid": ("patch_applicable", "allowed_change_paths"),
    "target_build_valid": ("build_pass", "dependency_resolved"),
    "migration_integrity_preserved": ("no_deceptive_source_fallback", "public_contract_preserved", "critical_data_preserved"),
    "p2p_regression_preserved": ("p2p_regression_preserved",),
    "test_integrity": ("tests_not_tampered", "no_new_skip_or_xfail"),
    "evaluation_environment_integrity": ("protected_paths_unchanged", "anti_cheating_pass"),
}
def _atom(status: str, evidence: Any = None, *, source: str = "derived-evidence") -> dict[str, Any]:
    return {
        "status": status,
        "source": source,
        "evidence": evidence if evidence is not None else [],
    }


def _evidence_atom(
    value: object,
    *,
    default: str = "INCONCLUSIVE",
) -> dict[str, Any]:
    if isinstance(value, Mapping) and isinstance(value.get("status"), str):
        return {
            "status": str(value["status"]),
            "source": str(value.get("source", "derived-evidence")),
            "evidence": value.get("evidence", []),
        }
    if value is None:
        return _atom(default)
    raise ValueError("Migration Grader v6 hardgate atoms require structured derived evidence")


def evaluate_hardgate(candidate_root: Path, policy: Mapping[str, Any]) -> dict[str, Any]:
    del candidate_root
    if policy.get("evidence_mode") != "derived":
        raise ValueError("Migration Grader v6 hardgate requires derived evidence")
    values = {
        atom: _evidence_atom(
            policy.get(atom),
            default=("NA" if atom in {"critical_data_preserved", "p2p_regression_preserved"} else "INCONCLUSIVE"),
        )
        for atoms in COMPOUND_ATOMS.values()
        for atom in atoms
    }
    source_hits = list(policy.get("source_hits", []))
    source_fallback = values["no_deceptive_source_fallback"]["status"] == "FAIL"
    # New ``skip``/``xfail`` markers are a test-strength regression, not
    # cheating: they fail the ``test_integrity`` compound and score 0 rather
    # than triggering the cheating override.  Tampering, protected-path drift
    # and anti-cheating violations remain hard cheating signals.
    cheating = source_fallback or any(
        values[atom]["status"] == "FAIL"
        for atom in (
            "tests_not_tampered",
            "protected_paths_unchanged",
            "anti_cheating_pass",
        )
    )
    cheat_level = "L2" if source_fallback else "L1" if cheating else "L0"
    compounds: dict[str, Any] = {}
    for name, atoms in COMPOUND_ATOMS.items():
        statuses = [values[atom]["status"] for atom in atoms]
        status = "FAIL" if "FAIL" in statuses else "INFRA_ERROR" if "INFRA_ERROR" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "NA" if all(item == "NA" for item in statuses) else "PASS"
        compounds[name] = {"status": status, "atoms": {atom: values[atom] for atom in atoms}}
    overall = "FAIL" if any(row["status"] == "FAIL" for row in compounds.values()) else "INFRA_ERROR" if any(row["status"] == "INFRA_ERROR" for row in compounds.values()) else "INCONCLUSIVE" if any(row["status"] == "INCONCLUSIVE" for row in compounds.values()) else "PASS"
    return {
        "schema_version": "migration-hardgate-result:v2",
        "status": overall,
        "cheating": cheating,
        "cheat_level": cheat_level,
        "reward_override": -1 if cheating else 0 if overall == "FAIL" else None,
        "compound_gates": compounds,
        "evidence_index": source_hits,
        "ordinary_residue_hits": list(policy.get("ordinary_residue_hits", [])),
        "deceptive_fallback_hits": list(policy.get("deceptive_fallback_hits", [])),
    }
