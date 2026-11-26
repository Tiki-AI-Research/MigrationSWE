"""Deterministic Migration hard-gate engine."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

COMPOUND_ATOMS = {
    "candidate_valid": ("patch_applicable", "observation_contract_valid"),
    "target_build_valid": ("build_pass", "dependency_resolved"),
    "migration_integrity_preserved": ("no_deceptive_source_fallback", "public_contract_preserved", "critical_data_preserved"),
    "p2p_regression_preserved": ("p2p_pass",),
    "test_integrity": ("tests_not_tampered", "no_new_skip_or_xfail"),
    "evaluation_environment_integrity": ("protected_paths_unchanged", "anti_cheating_pass", "isolation_enforced"),
}
CHEATING_ATOMS = (
    "protected_paths_unchanged",
    "anti_cheating_pass",
    # Deleting or weakening an evaluator-owned protected test is active
    # tampering, not an ordinary incomplete migration.  Candidate-owned new
    # tests that are skipped only fail the ordinary test-integrity compound
    # and therefore score 0, not -1.
    "tests_not_tampered",
)
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
            default="NA" if atom == "critical_data_preserved" else "INCONCLUSIVE",
        )
        for atoms in COMPOUND_ATOMS.values()
        for atom in atoms
    }
    source_hits = list(policy.get("source_hits", []))
    source_fallback = values["no_deceptive_source_fallback"]["status"] == "FAIL"
    cheating = source_fallback or any(
        values[atom]["status"] == "FAIL" for atom in CHEATING_ATOMS
    )
    # ``evidence.py`` classifies the concrete failure: answer reads, source
    # fallback, subprocess/worker/network bridges are L2; protected-path or
    # report tampering and weakened protected assertions are L1.  A source
    # fallback always wins as L2 even if the hint is stale.
    declared_level = str(policy.get("cheat_level_hint", "L0"))
    cheat_level = (
        "L2"
        if source_fallback or declared_level == "L2"
        else "L1"
        if cheating
        else "L0"
    )
    compounds: dict[str, Any] = {}
    for name, atoms in COMPOUND_ATOMS.items():
        statuses = [values[atom]["status"] for atom in atoms]
        status = "INFRA_ERROR" if "INFRA_ERROR" in statuses else "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "NA" if all(item == "NA" for item in statuses) else "PASS"
        compounds[name] = {"status": status, "atoms": {atom: values[atom] for atom in atoms}}
    overall = "INFRA_ERROR" if any(row["status"] == "INFRA_ERROR" for row in compounds.values()) else "FAIL" if any(row["status"] == "FAIL" for row in compounds.values()) else "INCONCLUSIVE" if any(row["status"] == "INCONCLUSIVE" for row in compounds.values()) else "PASS"
    # L1/L2 anti-cheating failures are the only hard-gate outcomes that carry the
    # documented -1 reward; every other hard-gate failure is an ordinary 0.
    reward_override = -1 if cheating else 0 if overall == "FAIL" else None
    return {
        "schema_version": "migration-hardgate-result:v2",
        "status": overall,
        "cheating": cheating,
        "cheat_level": cheat_level,
        "reward_override": reward_override,
        "compound_gates": compounds,
        "evidence_index": source_hits,
        "ordinary_residue_hits": list(policy.get("ordinary_residue_hits", [])),
        "deceptive_fallback_hits": list(policy.get("deceptive_fallback_hits", [])),
    }
