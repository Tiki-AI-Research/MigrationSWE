"""Differential comparison for BATCH-DIFFERENTIAL-0001.

Given the normalized source report and target report produced by the two
probes, decide for every case whether the observable projection is equal. The
comparison is structural (exit code, request order and bodies, parsed outputs,
normalized diagnostic); no human-readable log text participates.
"""

from __future__ import annotations

from typing import Any


def compare_case(source: Any, target: Any) -> bool:
    return source == target


def compare_reports(
    cases: list[dict[str, Any]],
    source_report: dict[str, Any],
    target_report: dict[str, Any],
) -> dict[str, Any]:
    source_cases = source_report.get("cases", {})
    target_cases = target_report.get("cases", {})

    case_results: dict[str, bool] = {}
    observation_results: dict[str, bool] = {}
    for case in cases:
        case_id = case["id"]
        equal = compare_case(source_cases.get(case_id), target_cases.get(case_id))
        case_results[case_id] = equal
        for observation_id in case["observation_ids"]:
            observation_results[observation_id] = observation_results.get(observation_id, True) and equal

    return {
        "cases": case_results,
        "observations": observation_results,
        "passed": all(case_results.values()) and bool(case_results),
    }
