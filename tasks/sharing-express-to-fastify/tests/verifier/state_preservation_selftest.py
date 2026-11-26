#!/usr/bin/env python3
"""Self-test grader enforcement of migration-contract state evidence."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grader  # noqa: E402


def _case(persistent_state: bool, evidence: dict | None) -> tuple[dict, dict]:
    root = Path(tempfile.mkdtemp())
    contract_path = root / "migration-contract.json"
    contract_path.write_text(
        json.dumps({"persistent_state_involved": persistent_state}), encoding="utf-8"
    )
    config = {"migration_contract": str(contract_path)}
    report = {}
    if evidence is not None:
        report["required_state_preservation_complete"] = evidence
    return report, config


def test_no_state_requires_explicit_na_with_reason() -> None:
    report, config = _case(False, {"status": "na", "reason": "No persistent state is involved."})
    grader.validate_state_preservation(report, config)


def test_missing_state_evidence_fails_closed() -> None:
    report, config = _case(False, None)
    try:
        grader.validate_state_preservation(report, config)
    except ValueError as exc:
        assert "missing required_state_preservation_complete" in str(exc)
    else:
        raise AssertionError("missing state evidence was accepted")


def test_not_run_state_evidence_fails_closed() -> None:
    report, config = _case(False, {"status": "not_run", "reason": "not executed"})
    try:
        grader.validate_state_preservation(report, config)
    except ValueError as exc:
        assert "status must be 'na'" in str(exc)
    else:
        raise AssertionError("not_run state evidence was accepted")


def test_persistent_state_requires_passed() -> None:
    report, config = _case(True, {"status": "passed"})
    grader.validate_state_preservation(report, config)
    report["required_state_preservation_complete"] = {"status": "na", "reason": "ignored"}
    try:
        grader.validate_state_preservation(report, config)
    except ValueError as exc:
        assert "status must be 'passed'" in str(exc)
    else:
        raise AssertionError("na state evidence was accepted for persistent state")


def main() -> int:
    tests = [
        test_no_state_requires_explicit_na_with_reason,
        test_missing_state_evidence_fails_closed,
        test_not_run_state_evidence_fails_closed,
        test_persistent_state_requires_passed,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    print(f"{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
