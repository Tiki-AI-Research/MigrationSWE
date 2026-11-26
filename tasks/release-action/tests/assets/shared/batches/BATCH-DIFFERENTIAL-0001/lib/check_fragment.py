#!/usr/bin/env python3
"""Calibration checker for the BATCH-DIFFERENTIAL-0001 asset fragment.

Verifies, among other structural rules, that every residue rule is actually
calibratable against the reference target:

  * the rule ``target`` is absent from every ``paths``-matched file in the
    reference target, and
  * appending the declared calibration ``content`` to the calibration ``path``
    would make the same rule match (the content contains the target).

This is an authoring self-check, not a runtime grader. It never mutates the
read-only reference tree: calibration is validated declaratively.
"""

from __future__ import annotations

import glob
import json
import os
import sys

ALLOWED_TOP_LEVEL = {
    "schema_version",
    "batch_id",
    "observations",
    "files",
    "gates",
    "semantic_rules",
    "state_checks",
    "public_surface_evidence",
    "ordinary_residue_rules",
    "deceptive_fallback_rules",
    "deceptive_fallback_not_applicable",
}
RULE_FIELDS = {"id", "kind", "target", "paths", "source_evidence", "requirement_ids", "calibration"}
RULE_KINDS = {"literal", "dependency", "import", "file"}
PSE_FIELDS = {"id", "surface", "source_path", "requirement_ids", "observation_ids"}

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))


def _files_matching(repo_root: str, patterns: list[str]) -> list[str]:
    matches: set[str] = set()
    for pattern in patterns:
        for path in glob.glob(os.path.join(repo_root, pattern), recursive=True):
            if os.path.isfile(path):
                matches.add(path)
    return sorted(matches)


def _target_in_files(files: list[str], target: str) -> list[str]:
    hits = []
    for path in files:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            if target in handle.read():
                hits.append(os.path.relpath(path, ASSET_ROOT))
    return hits


def _check_rules(rules: list[dict], label: str, repo_root: str, batch_prefix: str, errors: list[str], seen_ids: set[str]) -> None:
    for rule in rules:
        rule_id = rule.get("id", "<missing>")
        if set(rule) != RULE_FIELDS:
            errors.append(f"{label}[{rule_id}]: fields must be exactly {sorted(RULE_FIELDS)}, got {sorted(rule)}")
        if not rule_id.startswith(batch_prefix + "-"):
            errors.append(f"{label}[{rule_id}]: id must start with '{batch_prefix}-'")
        if rule_id in seen_ids:
            errors.append(f"{label}[{rule_id}]: duplicate id")
        seen_ids.add(rule_id)
        if rule.get("kind") not in RULE_KINDS:
            errors.append(f"{label}[{rule_id}]: invalid kind {rule.get('kind')!r}")

        target = str(rule.get("target", ""))
        if not target:
            errors.append(f"{label}[{rule_id}]: empty target")
        hits = _target_in_files(_files_matching(repo_root, rule.get("paths", [])), target)
        if hits:
            errors.append(f"{label}[{rule_id}]: target {target!r} already present in reference target: {hits}")

        calibration = rule.get("calibration", {})
        if calibration.get("operation") != "append":
            errors.append(f"{label}[{rule_id}]: calibration.operation must be 'append'")
        content = str(calibration.get("content", ""))
        if target and target not in content:
            errors.append(f"{label}[{rule_id}]: calibration.content must contain target {target!r}")
        cal_path = calibration.get("path", "")
        if not cal_path or not os.path.exists(os.path.join(repo_root, cal_path)):
            errors.append(f"{label}[{rule_id}]: calibration.path {cal_path!r} not found in reference target")

        for evidence in rule.get("source_evidence", []):
            if not os.path.exists(os.path.join(SOURCE_ROOT, evidence.get("path", ""))):
                errors.append(f"{label}[{rule_id}]: source_evidence path {evidence.get('path')!r} not found in source")


def main() -> int:
    fragment_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ASSET_ROOT, "fragments", "BATCH-DIFFERENTIAL-0001.json"
    )
    with open(fragment_path, "r", encoding="utf-8") as handle:
        fragment = json.load(handle)

    errors: list[str] = []
    if set(fragment) - ALLOWED_TOP_LEVEL:
        errors.append(f"unexpected top-level keys: {sorted(set(fragment) - ALLOWED_TOP_LEVEL)}")
    if fragment.get("schema_version") != "migration-asset-fragment:v2":
        errors.append("schema_version must be migration-asset-fragment:v2")
    batch_id = fragment.get("batch_id")
    if batch_id != "BATCH-DIFFERENTIAL-0001":
        errors.append(f"batch_id must be BATCH-DIFFERENTIAL-0001, got {batch_id!r}")

    cases = json.load(open(os.path.join(ASSET_ROOT, "shared", "batches", batch_id, "corpus", "cases.json")))["cases"]
    expected_observations = sorted({oid for case in cases for oid in case["observation_ids"]})
    observations = fragment.get("observations", [])
    if sorted(observations) != expected_observations:
        errors.append("observations must exactly match the batch observation ids")

    gates = fragment.get("gates", [])
    gate_ids = [gate.get("observation_id") for gate in gates]
    if sorted(gate_ids) != expected_observations or len(gate_ids) != len(set(gate_ids)):
        errors.append("gates must correspond one-to-one with observations")
    for gate in gates:
        if set(gate) != {"observation_id", "assertion"}:
            errors.append(f"gate {gate.get('observation_id')}: must contain only observation_id and assertion")
        elif set(gate.get("assertion", {})) != {"relation"}:
            errors.append(f"gate {gate.get('observation_id')}: assertion must contain only relation")

    if fragment.get("state_checks") != []:
        errors.append("state_checks must be strictly [] for a DIFFERENTIAL batch")

    seen_ids: set[str] = set()
    for label in ("semantic_rules", "ordinary_residue_rules", "deceptive_fallback_rules"):
        rules = fragment.get(label, [])
        if not isinstance(rules, list):
            errors.append(f"{label} must be an array")
            continue
        _check_rules(rules, label, TARGET_ROOT, batch_id, errors, seen_ids)
    if not fragment.get("semantic_rules"):
        errors.append("semantic_rules must be provided and non-empty")
    if not fragment.get("deceptive_fallback_rules") and not isinstance(fragment.get("deceptive_fallback_not_applicable"), dict):
        errors.append("provide deceptive_fallback_rules or deceptive_fallback_not_applicable")

    for evidence in fragment.get("public_surface_evidence", []):
        if set(evidence) != PSE_FIELDS:
            errors.append(f"public_surface_evidence {evidence.get('id')}: fields must be exactly {sorted(PSE_FIELDS)}")
        if not evidence.get("id", "").startswith(batch_id + "-"):
            errors.append(f"public_surface_evidence {evidence.get('id')}: id must be prefixed with {batch_id}-")
        if evidence.get("id") in seen_ids:
            errors.append(f"public_surface_evidence {evidence.get('id')}: duplicate id")
        seen_ids.add(evidence.get("id"))
        for observation_id in evidence.get("observation_ids", []):
            if observation_id not in expected_observations:
                errors.append(f"public_surface_evidence {evidence.get('id')}: unknown observation {observation_id}")

    if errors:
        print("FRAGMENT INVALID")
        for error in errors:
            print(" -", error)
        return 1
    print("FRAGMENT VALID")
    return 0


TARGET_ROOT = os.environ.get("TARGET_REPO", "/input/target")
SOURCE_ROOT = os.environ.get("SOURCE_REPO", "/input/source")

if __name__ == "__main__":
    raise SystemExit(main())
