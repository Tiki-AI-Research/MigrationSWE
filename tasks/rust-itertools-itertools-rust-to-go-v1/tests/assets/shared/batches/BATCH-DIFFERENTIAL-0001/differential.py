"""Normalization and comparison helpers for the DIFFERENTIAL batch.

The batch runs the same shared corpus against the Rust source reference
(``/input/source``) and the Go target (``/input/target`` during authoring, the
candidate module at grading time). Because Go marshals a nil slice as ``null``
while Rust reports an empty sequence, both sides are normalized before the
``equal_after_normalization`` relation is applied.

The dependency snapshot is read from ``/assets/dependencies`` and is never
copied or rewritten.
"""

import json
import os

ASSETS_DIR = os.environ.get("MIGRATION_ASSETS_DIR", "/assets")
DEPENDENCY_MANIFEST = os.path.join(ASSETS_DIR, "dependencies", "dependency-manifest.json")


class DifferentialError(RuntimeError):
    """Raised when a corpus fragment cannot be interpreted."""


def load_dependency_manifest(path=DEPENDENCY_MANIFEST):
    """Read the read-only dependency manifest produced for this case.

    Returns the parsed manifest and the batch ids this batch depends on so the
    caller can assert that the SKELETON shared helpers are available.
    """
    with open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    batch_ids = []
    for batch in manifest.get("batches", []):
        batch_id = batch.get("batch_id")
        if batch_id:
            batch_ids.append(batch_id)
    return manifest, batch_ids


def normalize(value):
    """Collapse representation-only differences between the two runtimes.

    * ``None`` (Go nil slice) becomes ``[]``.
    * Tuples become lists.
    * Dicts are normalized recursively.
    """
    if value is None:
        return []
    if isinstance(value, tuple):
        return [normalize(item) for item in value]
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    return value


def normalize_size_hint(hint):
    """Normalize a size hint to a ``[lower, upper]`` pair.

    A negative upper bound means "unbounded"; it is preserved as ``-1``.
    """
    if hint is None:
        return None
    if isinstance(hint, dict):
        return [int(hint["Lower"]), int(hint["Upper"])]
    if isinstance(hint, (list, tuple)) and len(hint) == 2:
        return [int(hint[0]), int(hint[1])]
    raise DifferentialError(f"unsupported size hint: {hint!r}")


def equal_after_normalization(source_value, target_value):
    """Apply the differential relation used by every gate of this batch."""
    return normalize(source_value) == normalize(target_value)


def compare_case(case, source_value, target_value):
    """Compare one corpus case and return a result entry."""
    passed = equal_after_normalization(source_value, target_value)
    detail = "equal after normalization"
    if not passed:
        detail = (
            "source=%s target=%s"
            % (json.dumps(normalize(source_value), sort_keys=True),
               json.dumps(normalize(target_value), sort_keys=True))
        )
    return {
        "case_id": case["id"],
        "observation_id": case["observation_id"],
        "requirement_ids": list(case.get("requirement_ids", [])),
        "passed": bool(passed),
        "detail": detail,
    }


def compare_batch(cases, source_results, target_results):
    """Compare a whole corpus given per-side result maps keyed by case id."""
    results = []
    for case in cases:
        case_id = case["id"]
        if case_id not in source_results or case_id not in target_results:
            results.append({
                "case_id": case_id,
                "observation_id": case["observation_id"],
                "requirement_ids": list(case.get("requirement_ids", [])),
                "passed": False,
                "detail": "missing probe result for one side",
            })
            continue
        results.append(compare_case(case, source_results[case_id], target_results[case_id]))
    return results
