#!/usr/bin/env python3
"""Self-test for the node status/failure_class -> histogram bucket mapping.

Usable both as a pytest module (``test_*`` functions) and as a script::

    python3 status_mapping_selftest.py

The script runs every ``test_*`` function, prints a compact PASS/FAIL table,
and exits non-zero on any failure.  Standard library only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # script mode / top-level module import
    from verifier import (
        _NODE_STATUS_DEFAULT_BUCKET,
        _NODE_STATUS_RULES,
        _canonical_controller_node,
        node_status_counts,
    )
except ImportError:  # pytest imported this file as ``verifier.status_mapping_selftest``
    from .verifier import (
        _NODE_STATUS_DEFAULT_BUCKET,
        _NODE_STATUS_RULES,
        _canonical_controller_node,
        node_status_counts,
    )

HISTOGRAM_KEYS = ("total", "passed", "failed", "missing", "skipped", "duplicate", "error")

# ``run_node`` is the only producer of controller results, and it emits exactly
# these shapes; ``_canonical_controller_node`` derives candidate_status and
# failure_class from them.
_PRODUCER_RESULTS: tuple[dict[str, Any], ...] = (
    {"status": "passed", "exit_code": 0, "timed_out": False},
    {"status": "candidate_failure", "exit_code": 1, "timed_out": False},
    {"status": "candidate_timeout", "exit_code": -1, "timed_out": True},
    {"status": "infrastructure_error", "exit_code": -1, "timed_out": False},
    {"status": "infrastructure_error", "exit_code": -1, "timed_out": True},
)

# Node producers outside ``_canonical_controller_node``: the infrastructure
# report, the nodes.json write-failure mutation, and the migration-scope node.
_STATIC_REACHABLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("blocked", "evaluator_infrastructure"),
    ("infrastructure_error", "infrastructure_error"),
    ("candidate_failure", "scope_incomplete"),
    ("failed", ""),
)


def _counts(status: str, failure_class: str | None = None, *, passed: bool = False) -> dict[str, int]:
    node: dict[str, Any] = {"pass": passed}
    if status:
        node["candidate_status"] = status
    if failure_class is not None:
        node["failure_class"] = failure_class
    return node_status_counts([node])


def _single_bucket(status: str, failure_class: str | None = None, *, passed: bool = False) -> str:
    counts = _counts(status, failure_class, passed=passed)
    return next(key for key in HISTOGRAM_KEYS if key != "total" and counts[key] == 1)


def _reachable_pairs() -> set[tuple[str, str]]:
    pairs = set(_STATIC_REACHABLE_PAIRS)
    for result in _PRODUCER_RESULTS:
        node = _canonical_controller_node(
            "f2p::probe", "f2p", {"base_status": "failed_as_expected"}, dict(result)
        )
        if node["pass"] is not True:
            pairs.add((
                str(node["candidate_status"]).lower(),
                str(node["failure_class"] or "").lower(),
            ))
    return pairs


def test_every_bucket_is_counted_exactly_once() -> None:
    nodes = [
        {"pass": True},
        {"pass": False, "candidate_status": "candidate_timeout", "failure_class": "timeout"},
        {"pass": False, "candidate_status": "missing"},
        {"pass": False, "candidate_status": "skipped"},
        {"pass": False, "candidate_status": "duplicate"},
        {"pass": False, "candidate_status": "blocked", "failure_class": "evaluator_infrastructure"},
    ]
    counts = node_status_counts(nodes)
    assert tuple(counts) == HISTOGRAM_KEYS
    assert counts == {
        "total": 6,
        "passed": 1,
        "failed": 1,
        "missing": 1,
        "skipped": 1,
        "duplicate": 1,
        "error": 1,
    }


def test_total_accounting() -> None:
    nodes = [
        {"pass": True},
        {"pass": True},
        {"pass": False, "candidate_status": "candidate_timeout", "failure_class": "timeout"},
        {"pass": False, "candidate_status": "totally_unknown"},
        {"pass": False, "candidate_status": "missing"},
        {"pass": False, "candidate_status": "not_found"},
        {"pass": False, "candidate_status": "skip"},
        {"pass": False, "candidate_status": "duplicated"},
        {"pass": False, "candidate_status": "error"},
        {"pass": False, "candidate_status": "blocked", "failure_class": "evaluator_infrastructure"},
    ]
    counts = node_status_counts(nodes)
    bucket_sum = sum(counts[key] for key in HISTOGRAM_KEYS if key != "total")
    assert counts["total"] == len(nodes) == bucket_sum


def test_passed_requires_pass_true() -> None:
    assert _single_bucket("passed", None, passed=True) == "passed"
    assert _single_bucket("passed", None, passed=False) == "failed"


def test_candidate_timeout_is_failed() -> None:
    assert _single_bucket("candidate_timeout", "timeout") == "failed"
    assert _single_bucket("candidate_timeout") == "failed"
    assert _single_bucket("", "timeout") == "failed"


def test_evaluator_infrastructure_timeout_is_error() -> None:
    assert _single_bucket("infrastructure_error", "infrastructure_error") == "error"
    assert _single_bucket("blocked", "evaluator_infrastructure") == "error"
    assert _single_bucket("error") == "error"
    assert _single_bucket("", "evaluator_infrastructure") == "error"


def test_missing_skipped_duplicate() -> None:
    assert _single_bucket("missing") == "missing"
    assert _single_bucket("not_found") == "missing"
    assert _single_bucket("", "missing") == "missing"
    assert _single_bucket("skipped") == "skipped"
    assert _single_bucket("skip") == "skipped"
    assert _single_bucket("xfail") == "skipped"
    assert _single_bucket("excluded") == "skipped"
    assert _single_bucket("duplicate") == "duplicate"
    assert _single_bucket("duplicated") == "duplicate"
    assert _single_bucket("", "duplicate") == "duplicate"


def test_unknown_status_defaults_to_failed() -> None:
    assert _NODE_STATUS_DEFAULT_BUCKET == "failed"
    assert _single_bucket("totally_unknown") == "failed"
    assert _single_bucket("") == "failed"
    assert _single_bucket("", "assertion_or_runtime") == "failed"


def test_infrastructure_rule_precedes_candidate_timeout_rule() -> None:
    rule_order = [bucket for _, _, bucket in _NODE_STATUS_RULES]
    assert rule_order.index("error") < rule_order.index("failed")
    assert _single_bucket("blocked", "timeout") == "error"


def test_timeout_reachability_invariant() -> None:
    result = {"status": "candidate_timeout", "exit_code": -1, "timed_out": True}
    node = _canonical_controller_node("p2p::timeout", "p2p", {"base_status": "passed"}, result)
    assert node["candidate_status"] == "candidate_timeout"
    assert node["failure_class"] == "timeout"
    assert node_status_counts([node])["failed"] == 1
    timeout_pairs = {pair for pair in _reachable_pairs() if "timeout" in pair}
    assert timeout_pairs == {("candidate_timeout", "timeout")}


def test_reachable_pairs_match_one_bucket_only() -> None:
    contradictions = []
    for status, failure_class in sorted(_reachable_pairs()):
        buckets = {
            bucket
            for statuses, failure_classes, bucket in _NODE_STATUS_RULES
            if status in statuses or failure_class in failure_classes
        }
        if len(buckets) > 1:
            contradictions.append((status, failure_class, sorted(buckets)))
    assert contradictions == []


def _run_all() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # any failure fails the self-test
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    print(f"{len(tests) - failures}/{len(tests)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
