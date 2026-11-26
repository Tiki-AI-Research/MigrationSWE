#!/usr/bin/env python3
"""Canonical normalization shared by the differential source and target probes.

The differential gate compares source and target observations with the
``equal_after_normalization`` relation.  This module removes only values that
are legitimately run-dependent (absolute paths and timing) and applies the
stable ordering rules from the public contract.  It intentionally keeps every
behavioral field, including commit order, lane positions, edge rows, refs, and
errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

EDGE_TYPE_ORDER = [
    "vertical",
    "horizontal",
    "up",
    "down",
    "left",
    "right",
    "right_top",
    "right_bottom",
    "left_top",
    "left_bottom",
]
EDGE_TYPE_INDEX = {name: index for index, name in enumerate(EDGE_TYPE_ORDER)}
REF_KIND_ORDER = ["tag", "branch", "remote_branch", "stash"]
REF_KIND_INDEX = {name: index for index, name in enumerate(REF_KIND_ORDER)}
VOLATILE_FIELDS = {"duration_ms", "elapsed_ms", "absolute_path", "cwd"}


def _canonical_time(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.isoformat()


def _canonical_commit(commit: dict) -> dict:
    result = {}
    for key, value in commit.items():
        if key in VOLATILE_FIELDS:
            continue
        if key in {"author_date", "committer_date"}:
            result[key] = _canonical_time(value)
        elif key == "parent_commit_hashes":
            result[key] = list(value or [])
        elif key in {"commit_hash", "full"} and isinstance(value, dict):
            result[key] = value.get("full", "")
        else:
            result[key] = value
    return result


def _canonical_ref(ref: dict) -> dict:
    target = ref.get("target")
    if isinstance(target, dict):
        target = target.get("full", "")
    return {
        "kind": ref.get("kind", ""),
        "name": ref.get("name", ""),
        "target": target,
        "message": ref.get("message", ""),
    }


def _ref_key(ref: dict) -> tuple:
    return (
        REF_KIND_INDEX.get(str(ref.get("kind", "")), len(REF_KIND_ORDER)),
        str(ref.get("name", "")),
        str(ref.get("target", "")),
        str(ref.get("message", "")),
    )


def _canonical_edge(edge: dict) -> dict:
    return {
        "edge_type": edge.get("edge_type", ""),
        "pos_x": int(edge.get("pos_x", 0)),
        "associated_line_pos_x": int(edge.get("associated_line_pos_x", 0)),
    }


def _edge_key(edge: dict) -> tuple:
    return (
        int(edge.get("associated_line_pos_x", 0)),
        int(edge.get("pos_x", 0)),
        EDGE_TYPE_INDEX.get(str(edge.get("edge_type", "")), len(EDGE_TYPE_ORDER)),
    )


def normalize_graph(document: dict) -> dict:
    commits = [_canonical_commit(c) for c in document.get("commits", [])]
    row_map = document.get("commit_pos_map", {})
    position_map = {}
    for key, value in row_map.items():
        if isinstance(value, dict):
            position_map[str(key)] = {
                "pos_x": int(value.get("pos_x", 0)),
                "pos_y": int(value.get("pos_y", 0)),
            }
    edges = []
    for row in document.get("edges", []):
        canonical = [_canonical_edge(edge) for edge in row]
        canonical.sort(key=_edge_key)
        deduped = []
        seen = set()
        for edge in canonical:
            key = _edge_key(edge)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(edge)
        edges.append(deduped)
    head = document.get("head")
    if isinstance(head, dict):
        target = head.get("target")
        if isinstance(target, dict):
            target = target.get("full", "")
        head = {"kind": head.get("kind", ""), "name": head.get("name", ""), "target": target or ""}
    return {
        "commits": commits,
        "commit_pos_map": position_map,
        "edges": edges,
        "max_pos_x": int(document.get("max_pos_x", 0)),
        "all_refs": normalize_refs(document.get("all_refs", [])),
        "head": head,
    }


def normalize_refs(refs: list) -> list:
    canonical = [_canonical_ref(ref) for ref in refs]
    canonical.sort(key=_ref_key)
    return canonical


def normalize_diff(document: dict) -> dict:
    files = []
    for entry in document.get("files", []):
        if isinstance(entry, str):
            files.append({"path": entry})
            continue
        item = {k: v for k, v in entry.items() if k not in VOLATILE_FIELDS}
        if isinstance(item.get("path"), str):
            item["path"] = item["path"].replace("\\", "/")
        files.append(item)
    files.sort(key=lambda entry: str(entry.get("path", "")))
    return {"files": files}


def normalize_document(document: object) -> object:
    if not isinstance(document, dict):
        return document
    if "commits" in document and "edges" in document:
        return normalize_graph(document)
    if "all_refs" in document:
        result = dict(document)
        result["all_refs"] = normalize_refs(document.get("all_refs", []))
        return result
    if "files" in document:
        return normalize_diff(document)
    return {k: v for k, v in document.items() if k not in VOLATILE_FIELDS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["auto", "graph", "refs", "diff"], default="auto")
    parser.add_argument("input", nargs="?", default="-")
    args = parser.parse_args(argv)
    stream = sys.stdin if args.input == "-" else open(args.input, "r", encoding="utf-8")
    with stream:
        document = json.load(stream)
    if args.kind == "graph":
        normalized = normalize_graph(document)
    elif args.kind == "refs":
        normalized = normalize_refs(document)
    elif args.kind == "diff":
        normalized = normalize_diff(document)
    else:
        normalized = normalize_document(document)
    json.dump(normalized, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
