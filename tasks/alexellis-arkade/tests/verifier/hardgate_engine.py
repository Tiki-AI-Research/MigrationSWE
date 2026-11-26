"""Deterministic Migration hard-gate engine."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

COMPOUND_ATOMS = {
    # An unparseable or contract-breaking observation report is authored by the
    # candidate, so it is a candidate-validity defect: it fails this gate, scores
    # zero, and must never be reported as verifier infrastructure failure.
    "candidate_valid": ("patch_applicable", "observation_contract_valid"),
    "target_build_valid": ("build_pass", "dependency_resolved"),
    "migration_integrity_preserved": ("no_deceptive_source_fallback", "public_contract_preserved", "critical_data_preserved"),
    # Regression parity is one gate: the untouched-behaviour tests (P2P) and
    # the observable-difference tests (DIFFERENTIAL) both have to pass for the
    # migration to be behaviourally faithful.
    "p2p_regression_preserved": ("p2p_regression_preserved", "differential_preserved"),
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


def _weakens_test_evidence(atom: Mapping[str, Any]) -> bool:
    """Is the ``tests_not_tampered`` atom a weakened/removed assertion?

    Any way of stopping a baseline assertion from doing its job is a weakened
    test, and the specification grades that as an L1 violation (``reward=-1``):
    deleting it, rewriting it, deleting its expectation literal, or leaving the
    text in place while making it unreachable (always-true early return, guard
    that depends on the environment, constant-false conjunct).  The atom records
    *which* form was found in ``evidence`` for diagnostics, but the grading does
    not depend on that shape.
    """
    return atom.get("status") == "FAIL"


def evaluate_hardgate(candidate_root: Path, policy: Mapping[str, Any]) -> dict[str, Any]:
    del candidate_root
    if policy.get("evidence_mode") != "derived":
        raise ValueError("Migration Grader v6 hardgate requires derived evidence")
    values = {
        atom: _evidence_atom(
            policy.get(atom),
            default=("NA" if atom in {"critical_data_preserved", "p2p_regression_preserved", "differential_preserved"} else "INCONCLUSIVE"),
        )
        for atoms in COMPOUND_ATOMS.values()
        for atom in atoms
    }
    source_hits = list(policy.get("source_hits", []))
    source_fallback = values["no_deceptive_source_fallback"]["status"] == "FAIL"
    # A cheating verdict comes from the branches the specification fixes as
    # hard violations: a source-runtime fallback (L2), weakened/deleted
    # assertions (``tests_not_tampered``, L1), evaluator-bundle tampering and
    # scoring interference (``anti_cheating_pass``, L1).  A new skip/xfail in a
    # candidate-owned test file and a hit on the frozen *source contract*
    # (``cmd/apps/*``) are quality/scope failures: they fail the hard gate and
    # score 0.
    #
    # ``tests_not_tampered`` is not part of ``anti_cheating_pass`` on purpose:
    # the two atoms stay independently attributable, so evidence shows *which*
    # measurement was attacked.
    cheating = (
        source_fallback
        or _weakens_test_evidence(values["tests_not_tampered"])
        or values["anti_cheating_pass"]["status"] == "FAIL"
    )
    cheat_level = "L2" if source_fallback else "L1" if cheating else "L0"
    compounds: dict[str, Any] = {}
    for name, atoms in COMPOUND_ATOMS.items():
        statuses = [values[atom]["status"] for atom in atoms]
        status = "INFRA_ERROR" if "INFRA_ERROR" in statuses else "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "NA" if all(item == "NA" for item in statuses) else "PASS"
        compounds[name] = {"status": status, "atoms": {atom: values[atom] for atom in atoms}}
    overall = "INFRA_ERROR" if any(row["status"] == "INFRA_ERROR" for row in compounds.values()) else "FAIL" if any(row["status"] == "FAIL" for row in compounds.values()) else "INCONCLUSIVE" if any(row["status"] == "INCONCLUSIVE" for row in compounds.values()) else "PASS"
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
