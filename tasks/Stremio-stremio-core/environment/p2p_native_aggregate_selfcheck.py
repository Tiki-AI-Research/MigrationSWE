"""Regression guard: status-wrapper P2P is scored from verifier receipts.

The r6 harbor report (R7-QC-6) found the independent P2P surface collector
silently no-op'd (``native-merge.json`` ``route=no-targets``) because the frozen
P2P baseline is a bare status wrapper with no ``test:<file>#<title>`` surface,
so the candidate harness report supplied the only P2P status.  This guard pins
the aggregate fix in ``tests/verifier/harbor_entry.py``:

* every frozen status-wrapper P2P observation is replaced by a status-wrapper
  value derived from the verifier-owned ``target-native-test`` prerequisite
  receipts (never the candidate harness, never a fabricated file/title surface);
* a failed or absent native receipt is fail-closed (FAIL / INCONCLUSIVE);
* a baseline that carries concrete test surfaces keeps the collector path.

Run it from anywhere:

    python3 environment/p2p_native_aggregate_selfcheck.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

_TASK_ROOT = Path(__file__).resolve().parents[1]
_TESTS = _TASK_ROOT / "tests"
sys.path.insert(0, str(_TESTS))

import verifier.harbor_entry as harbor_entry  # noqa: E402
from verifier.comparison import merge_comparison_status  # noqa: E402
from verifier.observation_adapter import GenericObservationAdapter  # noqa: E402


def _check(name: str, condition: object, detail: object = "") -> None:
    if not condition:
        raise RuntimeError(f"{name}: {detail}")


def _frozen_inputs() -> tuple[dict, list[dict]]:
    config = json.loads((_TESTS / "config.json").read_text(encoding="utf-8"))
    baseline = json.loads(
        (_TESTS / "assets" / "source-baseline-outputs.json").read_text(encoding="utf-8")
    )
    return config, list(baseline["observations"])


def _harness_observations(baseline_rows: list[dict], p2p_status: str) -> list[dict]:
    rows = json.loads(json.dumps(baseline_rows))
    for row in rows:
        row["value"]["value"]["result"] = p2p_status
    return rows


def _receipts(native_returncode: int | None) -> list[dict]:
    rows = [
        {
            "kind": "target-build",
            "index": 1,
            "command": ["go", "build", "./..."],
            "returncode": 0,
            "timed_out": False,
        }
    ]
    if native_returncode is not None:
        for index, command in (
            (2, ["go", "test", "./..."]),
            (3, ["gotestsum", "--junitfile", "/tmp/report.xml", "--", "./..."]),
        ):
            rows.append(
                {
                    "kind": "target-native-test",
                    "index": index,
                    "command": command,
                    "returncode": native_returncode,
                    "timed_out": False,
                }
            )
    return rows


def _merge(config: dict, baseline_rows: list[dict], receipts: list[dict], p2p_status: str) -> tuple[list, Path]:
    logs = Path(tempfile.mkdtemp(prefix="p2p-aggregate-selfcheck-"))
    merged = harbor_entry._merge_native_p2p_observations(
        observations=_harness_observations(baseline_rows, p2p_status),
        receipts=receipts,
        config=config,
        runtime=config["runtime"],
        candidate=Path("/nonexistent-candidate"),
        assets=_TESTS / "assets",
        logs=logs,
        timeout=10.0,
        environment={},
    )
    return merged, logs


def _check_passing(config: dict, baseline_rows: list[dict], p2p_ids: list[str]) -> None:
    merged, logs = _merge(config, baseline_rows, _receipts(0), "FAIL")
    p2p = [row for row in merged if row.get("kind") == "P2P"]
    _check("passing.count", len(p2p) == len(p2p_ids), len(p2p))
    _check("passing.ids", sorted(row["id"] for row in p2p) == sorted(p2p_ids))
    for row in p2p:
        inner = row["value"]["value"]
        _check("passing.status", row["status"] == "PASS", row)
        _check(
            "passing.projection",
            inner == {"projection": "status-wrapper", "result": "PASS"},
            inner,
        )
        _check(
            "passing.no-surface",
            "file" not in inner and "title" not in inner and "coverage" not in row,
            row,
        )
    diagnostic = json.loads((logs / "native-merge.json").read_text(encoding="utf-8"))
    _check("passing.route", diagnostic["route"] == "aggregate-native-test-receipts", diagnostic)
    _check("passing.aggregate", diagnostic["aggregate_status"] == "PASS", diagnostic)
    _check("passing.receipts", len(diagnostic["native_test_receipts"]) == 2, diagnostic)


def _check_failing(config: dict, baseline_rows: list[dict]) -> None:
    merged, _ = _merge(config, baseline_rows, _receipts(1), "PASS")
    p2p = [row for row in merged if row.get("kind") == "P2P"]
    _check("failing.status", all(row["status"] == "FAIL" for row in p2p), p2p[:1])
    _check(
        "failing.projection",
        all(row["value"]["value"]["result"] == "FAIL" for row in p2p),
        p2p[:1],
    )


def _check_absent(config: dict, baseline_rows: list[dict]) -> None:
    merged, _ = _merge(config, baseline_rows, _receipts(None), "PASS")
    p2p = [row for row in merged if row.get("kind") == "P2P"]
    _check("absent.status", all(row["status"] == "INCONCLUSIVE" for row in p2p), p2p[:1])


def _check_surface_mode(baseline_rows: list[dict], p2p_ids: list[str]) -> None:
    surface_rows = json.loads(json.dumps(baseline_rows))
    for row in surface_rows:
        if row.get("kind") == "P2P":
            row["value"]["value"] = {
                "file": "runtime/runtime_test.go",
                "title": "TestRuntimeEventMarshal",
            }
    _check(
        "surface.helper-none",
        harbor_entry._frozen_p2p_status_wrapper_ids(surface_rows) is None,
    )
    _check(
        "surface.helper-ids",
        harbor_entry._frozen_p2p_status_wrapper_ids(baseline_rows) == p2p_ids,
    )


def _check_baseline_compare(config: dict, baseline_rows: list[dict], merged: list) -> None:
    by_id = {str(row["id"]): row for row in baseline_rows}
    adapter = GenericObservationAdapter()
    policy = config["normalization_policy"]
    for row in merged:
        if row.get("kind") != "P2P":
            continue
        baseline = by_id[str(row["id"])]
        comparison = adapter.compare(baseline, row, policy).status
        final = merge_comparison_status(str(row["status"]), comparison)
        _check("compare.status", final == "PASS", (row["id"], final, comparison))


def _check_call_site() -> None:
    source = (_TESTS / "verifier" / "harbor_entry.py").read_text(encoding="utf-8")
    _check("callsite.receipts", "receipts=receipts," in source)
    _check(
        "callsite.order",
        source.index("_merge_native_p2p_observations(")
        < source.index("contract_override, contract_receipt"),
    )


def main() -> int:
    config, baseline_rows = _frozen_inputs()
    p2p_ids = [str(row["id"]) for row in baseline_rows if row.get("kind") == "P2P"]
    _check("frozen.p2p-count", len(p2p_ids) == 17, p2p_ids)
    _check_passing(config, baseline_rows, p2p_ids)
    _check_failing(config, baseline_rows)
    _check_absent(config, baseline_rows)
    _check_surface_mode(baseline_rows, p2p_ids)
    merged, _ = _merge(config, baseline_rows, _receipts(0), "FAIL")
    _check_baseline_compare(config, baseline_rows, merged)
    _check_call_site()
    print("p2p_native_aggregate_selfcheck: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
