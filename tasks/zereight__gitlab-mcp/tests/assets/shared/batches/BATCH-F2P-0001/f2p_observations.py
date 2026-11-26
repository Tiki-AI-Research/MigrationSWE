"""Observation-to-predicate map for ``BATCH-F2P-0001``.

The same map is consumed by the source and target probes, guaranteeing that
both sides evaluate an identical semantic predicate per observation.  Every
observation is a fail-to-pass target postcondition, so the source side is
expected to report ``FAIL`` and the target side ``PASS``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from f2p_predicates import (  # noqa: E402
    Runtime,
    pred_changed_paths_scope,
    pred_compact_json_ensure_ascii,
    pred_no_node_delegation,
    pred_no_node_source_scan,
    pred_packaging_entrypoint,
    pred_phase_index_coercion,
    pred_read_prd_indent,
    pred_stdio_identity,
    pred_story_id_format,
    pred_team_line_formatting,
    pred_tools_list_and_startup,
    pred_updated_at_format,
)

# observation_id -> (callable, contract requirement ids)
OBSERVATIONS = {
    "F2P-001": (pred_updated_at_format, ["R042"]),
    "F2P-002": (pred_read_prd_indent, ["R041"]),
    "F2P-003": (pred_story_id_format, ["R045"]),
    "F2P-004": (pred_phase_index_coercion, ["R044"]),
    "F2P-005": (pred_tools_list_and_startup, ["R006"]),
    "F2P-006": (pred_changed_paths_scope, ["R036"]),
    "F2P-007": (pred_no_node_delegation, ["R035"]),
    "F2P-008": (pred_packaging_entrypoint, ["R046"]),
    "F2P-009": (pred_compact_json_ensure_ascii, ["R040"]),
    "F2P-013": (pred_no_node_source_scan, ["R034"]),
    "F2P-014": (pred_team_line_formatting, ["R043"]),
    "F2P-018": (pred_stdio_identity, ["R001"]),
    "F2P-011": (pred_story_id_format, ["R045"]),
    "F2P-020": (pred_no_node_delegation, ["R035"]),
    "F2P-010": (pred_updated_at_format, ["R042"]),
    "F2P-012": (pred_story_id_format, ["R045"]),
    "F2P-015": (pred_read_prd_indent, ["R041"]),
    "F2P-016": (pred_no_node_delegation, ["R035"]),
    "F2P-017": (pred_updated_at_format, ["R042"]),
    "F2P-019": (pred_read_prd_indent, ["R041"]),
    "F2P-021": (pred_story_id_format, ["R045"]),
    "F2P-022": (pred_read_prd_indent, ["R041"]),
    "F2P-023": (pred_read_prd_indent, ["R041"]),
    "F2P-024": (pred_updated_at_format, ["R042"]),
}

OBSERVATION_ORDER = list(OBSERVATIONS)


def mcp_root_for_side(side: str, explicit: str | None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    if side == "target":
        candidates = ["/input/target/mcp-server", "/workspace/repo/mcp-server"]
    else:
        candidates = ["/input/source/mcp-server"]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    raise SystemExit(f"cannot locate mcp-server for side {side!r}")


def run(side: str, mcp_root: str, source_root: str):
    runtime = Runtime(mcp_root)
    results = {}
    for observation_id, (predicate, requirement_ids) in OBSERVATIONS.items():
        try:
            if predicate is pred_changed_paths_scope:
                ok, detail = predicate(runtime, source_root)
            else:
                ok, detail = predicate(runtime)
        except Exception as error:  # noqa: BLE001 - never mask as PASS
            ok, detail = False, f"probe raised {type(error).__name__}: {error}"
        results[observation_id] = {
            "status": "PASS" if ok else "FAIL",
            "requirement_ids": requirement_ids,
            "detail": detail,
        }
    return {
        "schema_version": "migration-f2p-probe-result:v1",
        "batch_id": "BATCH-F2P-0001",
        "side": side,
        "mcp_root": mcp_root,
        "observations": results,
    }


def main(side: str, argv=None):
    parser = argparse.ArgumentParser(description=f"BATCH-F2P-0001 {side} probe")
    parser.add_argument("--mcp-root", default=None, help="path to the mcp-server tree")
    parser.add_argument("--source-root", default="/input/source/mcp-server")
    parser.add_argument("--report", default=None, help="optional JSON output path")
    args = parser.parse_args(argv)

    mcp_root = mcp_root_for_side(side, args.mcp_root)
    report = run(side, mcp_root, args.source_root)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    failed = [k for k, v in report["observations"].items() if v["status"] != (
        "PASS" if side == "target" else "FAIL")]
    return 0 if not failed else 1
