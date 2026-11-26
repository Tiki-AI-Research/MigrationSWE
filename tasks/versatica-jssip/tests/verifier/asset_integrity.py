#!/usr/bin/env python3
"""Verify the frozen evaluator assets before any of them are consumed.

The manifest is the single identity record for the baseline archive, the
migration contract, the hidden test payload and the reference parser.  A
missing or malformed manifest is a configuration error: the verifier must not
silently run against unverified assets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entries(manifest: dict[str, Any]) -> dict[str, str]:
    """Normalize the common manifest shapes into ``relative path -> sha256``."""
    raw = manifest.get("files")
    if raw is None:
        raw = manifest.get("assets")
    if raw is None:
        raw = {
            key: value
            for key, value in manifest.items()
            if isinstance(value, (str, dict)) and key not in {"schema_version", "task_id"}
        }
    if isinstance(raw, list):
        normalized: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or item.get("name")
            digest = item.get("sha256") or item.get("hash")
            if isinstance(path, str) and isinstance(digest, str):
                normalized[path] = digest.removeprefix("sha256:")
        return normalized
    if not isinstance(raw, dict):
        return {}
    normalized = {}
    for key, value in raw.items():
        if isinstance(value, str):
            normalized[str(key)] = value.removeprefix("sha256:")
        elif isinstance(value, dict):
            digest = value.get("sha256") or value.get("hash")
            path = value.get("path") or key
            if isinstance(digest, str) and isinstance(path, str):
                normalized[path] = digest.removeprefix("sha256:")
    return normalized


def _check_file(
    report: dict[str, Any],
    path: Path,
    relative: str,
    expected: str,
) -> bool:
    if not path.is_file():
        report["checks"].append({"path": relative, "status": "failed", "message": "missing"})
        report["errors"].append(f"missing asset: {relative}")
        return False
    actual = _sha256(path)
    status = "passed" if actual == expected else "failed"
    report["checks"].append(
        {"path": relative, "status": status, "expected": expected, "actual": actual}
    )
    if status == "failed":
        report["errors"].append(f"hash mismatch: {relative}")
        return False
    return True


def _contract_consistency(
    report: dict[str, Any],
    assets: Path,
    config_path: Path | None,
) -> None:
    """Consume the frozen contract and prove it still describes the config."""
    contract_path = assets / "migration-contract.json"
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report["errors"].append(f"cannot read migration contract: {exc}")
        return
    config: dict[str, Any] = {}
    if config_path is not None:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            report["errors"].append(f"cannot read verifier config: {exc}")
            return

    contract_units = [str(unit.get("unit_id")) for unit in contract.get("migration_units") or []]
    config_units = [str(unit.get("id")) for unit in config.get("migration_units") or []]
    if contract_units != config_units:
        report["errors"].append(
            f"migration contract units {contract_units} differ from config units {config_units}"
        )
    if contract.get("base_commit") != config.get("base_commit"):
        report["errors"].append("migration contract base_commit differs from verifier config")
    if contract.get("source_technology") != config.get("source_technology"):
        report["errors"].append("migration contract source_technology differs from verifier config")
    if contract.get("target_technology") != config.get("target_technology"):
        report["errors"].append("migration contract target_technology differs from verifier config")

    f2p = [str(node) for node in config.get("f2p") or []]
    p2p = [str(node) for node in config.get("p2p") or []] + [
        str(node) for node in config.get("differential") or []
    ]
    contract_f2p = {
        str(node)
        for unit in contract.get("migration_units") or []
        for node in unit.get("f2p_evidence") or []
    }
    missing = sorted(set(f2p) - contract_f2p)
    if missing:
        report["errors"].append("config F2P nodes absent from contract evidence: " + ", ".join(missing))
    declared = contract.get("baseline_evidence") or {}
    if declared.get("p2p", {}).get("total") not in (None, len(p2p)):
        report["errors"].append("contract baseline P2P total differs from config")
    if declared.get("f2p", {}).get("total") not in (None, len(f2p)):
        report["errors"].append("contract baseline F2P total differs from config")
    if declared.get("f2p", {}).get("passed") not in (None, 0):
        report["errors"].append("contract baseline F2P must have zero source passes")
    if declared.get("p2p", {}).get("passed") not in (None, len(p2p)):
        report["errors"].append("contract baseline P2P passes differ from config")

    # A declared total is not evidence.  The frozen baseline measurement must
    # cover exactly the current F2P/P2P node sets, and every F2P node must have
    # failed on the source baseline while every P2P/differential node passed.
    # This catches new F2P nodes that were never measured and source-passing
    # nodes misclassified as fail-to-pass.
    baseline_name = Path(str(declared.get("asset") or "baseline-measurement.json")).name
    baseline_path = assets / baseline_name
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report["errors"].append(f"cannot read baseline measurement: {exc}")
        baseline = {}
    baseline_f2p: dict[str, str] = {}
    baseline_p2p: dict[str, str] = {}
    for node in baseline.get("nodes") or []:
        if not isinstance(node, dict):
            report["errors"].append("baseline node entry is not an object")
            continue
        node_id = str(node.get("testId") or node.get("name") or "")
        group = str(node.get("group") or "")
        status = str(node.get("status") or "")
        target = baseline_f2p if group == "F2P" else baseline_p2p if group in {"P2P", "differential"} else None
        if not node_id or target is None or not status:
            report["errors"].append(f"malformed baseline node: {node!r}")
            continue
        if node_id in target:
            report["errors"].append(f"duplicate baseline node: {node_id}")
        target[node_id] = status
    if set(baseline_f2p) != set(f2p):
        missing_nodes = sorted(set(f2p) - set(baseline_f2p))
        extra_nodes = sorted(set(baseline_f2p) - set(f2p))
        report["errors"].append(
            "baseline F2P node set differs from config"
            + (f"; missing: {', '.join(missing_nodes)}" if missing_nodes else "")
            + (f"; extra: {', '.join(extra_nodes)}" if extra_nodes else "")
        )
    if set(baseline_p2p) != set(p2p):
        missing_nodes = sorted(set(p2p) - set(baseline_p2p))
        extra_nodes = sorted(set(baseline_p2p) - set(p2p))
        report["errors"].append(
            "baseline P2P node set differs from config"
            + (f"; missing: {', '.join(missing_nodes)}" if missing_nodes else "")
            + (f"; extra: {', '.join(extra_nodes)}" if extra_nodes else "")
        )
    bad_f2p = sorted(node for node, status in baseline_f2p.items() if status != "failed")
    if bad_f2p:
        report["errors"].append(
            "baseline F2P nodes did not fail on source: " + ", ".join(bad_f2p)
        )
    bad_p2p = sorted(node for node, status in baseline_p2p.items() if status != "passed")
    if bad_p2p:
        report["errors"].append(
            "baseline P2P nodes did not pass on source: " + ", ".join(bad_p2p)
        )
    behavior_ids = {
        str(node) for node in (contract.get("behavior_regression") or {}).get("p2p_node_ids") or []
    }
    if behavior_ids != set(p2p):
        report["errors"].append("contract behavior P2P node ids differ from config P2P nodes")
    report["contract"] = {
        "status": "passed" if not report["errors"] else "failed",
        "units": config_units,
        "f2p": len(f2p),
        "p2p": len(p2p),
        "baseline_f2p": len(baseline_f2p),
        "baseline_p2p": len(baseline_p2p),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--tests-root", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args()

    assets = args.assets.resolve()
    tests_root = (args.tests_root or assets.parent).resolve()
    manifest_path = args.manifest or (assets / "assets-manifest.json")
    # The delivery gate invokes this script with only --assets/--out.  Default
    # to the sibling verifier config so the contract cross-check still runs.
    config_path = args.config or (tests_root / "config.json")
    if not config_path.is_file():
        config_path = None
    report: dict[str, Any] = {
        "schema_version": "deepswe-asset-integrity/v1",
        "passed": False,
        "success": False,
        "manifest": str(manifest_path),
        "checks": [],
        "errors": [],
    }

    def finish(code: int) -> int:
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return code

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report["errors"].append(f"cannot read asset manifest: {exc}")
        return finish(2)
    if not isinstance(manifest, dict):
        report["errors"].append("asset manifest must be a JSON object")
        return finish(2)

    entries = _entries(manifest)
    if not entries:
        report["errors"].append("asset manifest contains no hashed entries")
        return finish(2)

    for relative, expected in sorted(entries.items()):
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            report["checks"].append(
                {"path": relative, "status": "failed", "message": "unsafe path in manifest"}
            )
            report["errors"].append(f"unsafe manifest path: {relative}")
            continue
        _check_file(report, assets / relative_path, relative, expected)

    # The hidden payloads live beside the manifest, not inside tests/assets.
    # The integrity policy freezes them at tests/-relative paths so they cannot
    # be silently replaced after the task is packaged.
    policy_path = assets / "evaluation-integrity-policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report["errors"].append(f"cannot read evaluation integrity policy: {exc}")
    else:
        for entry in policy.get("frozen_files") or []:
            if not isinstance(entry, dict):
                continue
            relative = entry.get("path")
            expected = entry.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                report["errors"].append("frozen_files entry is missing path or sha256")
                continue
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                report["errors"].append(f"unsafe frozen path: {relative}")
                continue
            path = tests_root / relative_path
            _check_file(report, path, relative, expected.removeprefix("sha256:"))

    _contract_consistency(report, assets, config_path)
    report["passed"] = not report["errors"]
    report["success"] = report["passed"]
    return finish(0 if report["passed"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
