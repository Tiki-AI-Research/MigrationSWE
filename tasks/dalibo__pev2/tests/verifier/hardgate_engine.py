"""Deterministic Migration hard-gate engine."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

# Exactly the six compound gates of the Migration standard (candidate_valid,
# target_build_valid, migration_integrity_preserved, p2p_regression_preserved,
# test_integrity, evaluation_environment_integrity).  Behavioural (differential)
# oracle equivalence is NOT a compound gate: the standard folds it into
# ``p2p_regression_preserved`` as the observable-behaviour bottom line, so it is
# evaluated inside that atom (``evidence.build_hardgate_evidence``) rather than
# wired as a seventh gate.
COMPOUND_ATOMS = {
    "candidate_valid": ("patch_applicable",),
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


def _tampered_protected_tests(atom: Mapping[str, Any] | None) -> bool:
    """Whether ``tests_not_tampered`` failed for an instruction-protected test.

    ``evidence.build_hardgate_evidence`` splits the tampered files by ownership:
    instruction-frozen source tests (the Vitest specs, ``from-text`` fixtures and
    Playwright snapshots) are evaluator-owned and an L1 ``policy_violation``,
    while the reference solution's own ``present_only`` Rust tests are
    candidate-authored and only an ordinary ``candidate_failure``.  Evidence
    without the ownership split is treated fail-closed as a protected violation
    so an unknown producer can never silently downgrade an L1 tamper to ``0``.
    """

    if not isinstance(atom, Mapping) or str(atom.get("status")) != "FAIL":
        return False
    evidence = atom.get("evidence")
    if not isinstance(evidence, Mapping):
        return True
    if "protected_changed" not in evidence and "candidate_changed" not in evidence:
        return True
    protected = list(evidence.get("protected_missing", evidence.get("missing", [])) or [])
    protected += list(evidence.get("protected_changed", []) or [])
    return bool(protected)



def evaluate_hardgate(candidate_root: Path, policy: Mapping[str, Any]) -> dict[str, Any]:
    del candidate_root
    if policy.get("evidence_mode") != "derived":
        raise ValueError("Migration Grader v6 hardgate requires derived evidence")
    missing_atoms = [
        atom
        for atoms in COMPOUND_ATOMS.values()
        for atom in atoms
        if policy.get(atom) is None
    ]
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
    cheating = (
        source_fallback
        or _tampered_protected_tests(values["tests_not_tampered"])
        or any(
            values[atom]["status"] == "FAIL"
            for atom in (
                "protected_paths_unchanged",
                "anti_cheating_pass",
            )
        )
    )
    cheat_level = "L2" if source_fallback else "L1" if cheating else "L0"
    compounds: dict[str, Any] = {}
    for name, atoms in COMPOUND_ATOMS.items():
        statuses = [values[atom]["status"] for atom in atoms]
        status = "INFRA_ERROR" if "INFRA_ERROR" in statuses else "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "NA" if all(item == "NA" for item in statuses) else "PASS"
        compounds[name] = {"status": status, "atoms": {atom: values[atom] for atom in atoms}}
    overall = "INFRA_ERROR" if any(row["status"] == "INFRA_ERROR" for row in compounds.values()) else "FAIL" if any(row["status"] == "FAIL" for row in compounds.values()) else "INCONCLUSIVE" if any(row["status"] == "INCONCLUSIVE" for row in compounds.values()) else "PASS"
    defaulted_atoms = {
        atom: {"status": values[atom]["status"], "source": values[atom]["source"]}
        for atom in missing_atoms
    }
    return {
        "schema_version": "migration-hardgate-result:v2",
        "status": overall,
        "cheating": cheating,
        "cheat_level": cheat_level,
        "reward_override": -1 if cheating else 0 if overall == "FAIL" else None,
        "compound_gates": compounds,
        "missing_atoms": missing_atoms,
        "defaulted_atoms": defaulted_atoms,
        "summary": {
            "status": overall,
            "failed_atoms": [atom for atom, value in values.items() if value["status"] == "FAIL"],
            "inconclusive_atoms": [atom for atom, value in values.items() if value["status"] == "INCONCLUSIVE"],
            "infra_error_atoms": [atom for atom, value in values.items() if value["status"] == "INFRA_ERROR"],
        },
        "evidence_index": source_hits,
        "ordinary_residue_hits": list(policy.get("ordinary_residue_hits", [])),
        "deceptive_fallback_hits": list(policy.get("deceptive_fallback_hits", [])),
    }
