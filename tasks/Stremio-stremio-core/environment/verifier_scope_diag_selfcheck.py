"""Regression guard: DIFF references never count as missing migration scope.

The r7 harbor report (R8-03 / fp-fn V-1) found ``tests/verifier/verifier_engine.py``
excluded differential observation ids from migration-unit scope only *after* the
hard-gate short-circuit.  On any hard-gate failure every unit that referenced a
DIFF evidence id was falsely reported INCONCLUSIVE with ``missing_observation_ids``
naming the DIFF ids, so ``migration_scope_complete_ratio`` was systematically
under-reported (10/41 instead of 41/41).  This guard pins the branch-independent
exclusion:

* the hard-gate short-circuit and the normal (PASS) path apply the identical DIFF
  exclusion, so mixed P2P/F2P/STATE + DIFF units pass in both;
* DIFF ids never appear in ``missing_observation_ids``;
* a genuinely missing non-DIFF reference is still detected;
* the hard-gate reward branch/value is unchanged (0.0, or -1.0 when cheating).

Run it from anywhere::

    python3 environment/verifier_scope_diag_selfcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True

_TASK_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _TASK_ROOT / "tests"
sys.path.insert(0, str(_TESTS))

import verifier.verifier_engine as verifier_engine  # noqa: E402

_DIFF_IDS = ("DIFF-006", "DIFF-007", "DIFF-011", "DIFF-012")


def _check(name: str, condition: object, detail: object = "") -> None:
    if not condition:
        raise RuntimeError(f"{name}: {detail}")


def _clean_inputs() -> dict[str, object]:
    return {
        "units": [
            {"id": "MU-P2P-DIFF", "kind": "source", "target_locators": []},
            {"id": "MU-F2P-DIFF", "kind": "source", "target_locators": []},
            {"id": "MU-PLAIN", "kind": "source", "target_locators": []},
            {"id": "MU-MULTI-DIFF", "kind": "source", "target_locators": []},
        ],
        "unit_checks": {
            "MU-P2P-DIFF": {"observation_ids": ["P2P-001", "DIFF-006", "DIFF-007"]},
            "MU-F2P-DIFF": {"observation_ids": ["F2P-001", "DIFF-011"]},
            "MU-PLAIN": {"observation_ids": ["P2P-001"]},
            "MU-MULTI-DIFF": {"observation_ids": ["STATE-001", "DIFF-012", "DIFF-006"]},
        },
        "p2p": [{"id": "P2P-001", "kind": "P2P", "status": "PASS"}],
        "f2p": [{"id": "F2P-001", "kind": "F2P", "status": "PASS"}],
        "differential": [
            {"id": identifier, "kind": "DIFFERENTIAL", "status": "PASS"}
            for identifier in _DIFF_IDS
        ],
        "state": {
            "checks": [{"id": "STATE-001", "status": "PASS"}],
            "applicable": True,
            "roles": {"STATE-001": "required_state"},
        },
        "applicability": {
            "P2P": "required",
            "F2P": "required",
            "DIFFERENTIAL": "required",
            "STATE": {"status": "required"},
        },
    }


def _evaluate(status: str, *, cheating: bool = False, inputs: dict[str, object] | None = None) -> dict:
    fixture = inputs if inputs is not None else _clean_inputs()
    return verifier_engine.evaluate_verifier(
        hardgate={"status": status, "cheating": cheating},
        migration_units=fixture["units"],
        p2p=fixture["p2p"],
        f2p=fixture["f2p"],
        differential=fixture["differential"],
        state=fixture["state"],
        weights={},
        unit_checks=fixture["unit_checks"],
        applicability=fixture["applicability"],
    )


def _scope(result: dict) -> dict:
    return result["components"]["migration_scope_complete"]


def _signature(scope: dict) -> dict[str, tuple[str, tuple[str, ...]]]:
    return {
        str(row["id"]): (str(row["status"]), tuple(row.get("missing_observation_ids", ())))
        for row in scope["checks"]
    }


def _check_clean(status: str, *, cheating: bool = False) -> dict:
    result = _evaluate(status, cheating=cheating)
    scope = _scope(result)
    _check(f"clean.{status}.scope-status", scope["status"] == "PASS", scope)
    _check(f"clean.{status}.counts", scope["passed"] == scope["total"] == 4, scope)
    for row in scope["checks"]:
        _check(f"clean.{status}.unit.{row['id']}", row["status"] == "PASS", row)
        _check(
            f"clean.{status}.unit.{row['id']}.no-missing",
            row.get("missing_observation_ids") == [],
            row,
        )
    _check(f"clean.{status}.ratio", result["progress"]["scope_ratio"] == 1.0, result["progress"])
    expected_branch = (
        "cheating" if cheating else "hard_gate_failed" if status == "FAIL" else "inconclusive"
    )
    expected_value = -1.0 if cheating else 0.0
    _check(f"clean.{status}.reward-branch", result["reward"]["branch"] == expected_branch, result["reward"])
    _check(f"clean.{status}.reward-value", result["reward"]["value"] == expected_value, result["reward"])
    return result


def _check_parity() -> None:
    failed = _evaluate("FAIL")
    passed = _evaluate("PASS")
    _check("parity.status", passed["status"] == "success", passed["status"])
    _check("parity.scope", _signature(_scope(passed)) == _signature(_scope(failed)), (
        _signature(_scope(passed)),
        _signature(_scope(failed)),
    ))
    _check("parity.ratio", _scope(passed)["passed"] == 4, _scope(passed))
    _check("parity.reward-branch", passed["reward"]["branch"] == "verifier_success", passed["reward"])
    _check("parity.reward-value", passed["reward"]["value"] == 1.0, passed["reward"])


def _check_no_diff_reported_missing(result: dict, label: str) -> None:
    for row in _scope(result)["checks"]:
        missing = [str(item) for item in row.get("missing_observation_ids", [])]
        _check(f"{label}.{row['id']}.no-diff-missing", not set(missing) & set(_DIFF_IDS), row)


def _check_missing_non_diff_detected(status: str) -> None:
    inputs = _clean_inputs()
    inputs["unit_checks"]["MU-P2P-DIFF"] = {
        "observation_ids": ["P2P-001", "DIFF-006", "P2P-999"]
    }
    result = _evaluate(status, inputs=inputs)
    row = next(row for row in _scope(result)["checks"] if row["id"] == "MU-P2P-DIFF")
    _check(f"missing.{status}.status", row["status"] == "INCONCLUSIVE", row)
    _check(f"missing.{status}.ids", row["missing_observation_ids"] == ["P2P-999"], row)


def main() -> int:
    for status in ("FAIL", "INCONCLUSIVE", "INFRA_ERROR"):
        result = _check_clean(status)
        _check_no_diff_reported_missing(result, f"clean.{status}")
        _check_missing_non_diff_detected(status)
    _check_clean("FAIL", cheating=True)
    _check_parity()
    print("verifier_scope_diag_selfcheck: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
