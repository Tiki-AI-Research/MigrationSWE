"""Shared P2P projection helpers for BATCH-P2P-0001.

This module is consumed by both the source-side probe
(``source/batches/BATCH-P2P-0001/probe.py``) and the target-side probe
(``target/batches/BATCH-P2P-0001/probe.py``).  It keeps the two sides in
agreement about the normalized projection emitted for every P2P observation.

The P2P gate is ``equal_after_normalization``: the source and the target each
compute a projection for an observation and the grader requires the two
projections to be byte-identical after normalization.  The migrated behavior
lives in two different languages, so there is no shared concrete value for
most observations; per the batch contract both sides therefore emit the same
status wrapper ``{"result": "PASS"}`` when the repository's own native test
suite passes.

The report envelope is owned by the SKELETON batch and is imported rather than
re-implemented, so that source-side and target-side reports stay compatible.
The SKELETON batch is resolved through ``dependency-manifest.json``; the
read-only dependency snapshot is never copied or rewritten.
"""

import json
import os
import sys

BATCH_ID = "BATCH-P2P-0001"
SKELETON_BATCH_ID = "BATCH-AUTHOR-BATCH-SKELETON-0001"

DEPENDENCY_MANIFEST = os.environ.get(
    "MIGRATION_DEPENDENCY_MANIFEST",
    "/assets/dependencies/dependency-manifest.json",
)

HERE = os.path.dirname(os.path.abspath(__file__))


def load_dependency_manifest(path=DEPENDENCY_MANIFEST):
    """Consume the read-only dependency snapshot manifest."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _dependency_batch_dir(batch_id=SKELETON_BATCH_ID, manifest_path=DEPENDENCY_MANIFEST):
    root = os.path.dirname(os.path.abspath(manifest_path))
    return os.path.join(root, "batches", batch_id)


def _find_shared_skeleton():
    """Locate the SKELETON ``shared/skeleton`` package.

    First prefer the assembled asset tree (``shared/skeleton`` is a sibling of
    ``shared/batches``), then fall back to the read-only dependency snapshot.
    """
    candidates = [
        os.path.abspath(os.path.join(HERE, "..", "..", "skeleton")),
    ]
    try:
        candidates.append(
            os.path.join(_dependency_batch_dir(), "shared", "skeleton")
        )
    except Exception:
        pass
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "report.py")):
            return candidate
    return None


_REPORT = None


def _report_module():
    global _REPORT
    if _REPORT is not None:
        return _REPORT
    shared = _find_shared_skeleton()
    if shared and shared not in sys.path:
        sys.path.insert(0, shared)
    try:
        import report  # type: ignore

        _REPORT = report
    except Exception:
        _REPORT = None
    return _REPORT


def load_corpus(path=None):
    """Load the shared P2P corpus fixture."""
    if path is None:
        path = os.path.abspath(
            os.path.join(
                HERE,
                "..",
                "..",
                "..",
                "fixtures",
                "batches",
                BATCH_ID,
                "p2p_corpus.json",
            )
        )
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def status_projection(passed):
    """The stable common projection for a P2P observation."""
    return {"result": "PASS" if passed else "FAIL"}


def project_observations(corpus, side_passed):
    """Map every corpus case to its normalized projection.

    ``side_passed`` is the boolean result of the side's repository-native test
    command.  Every observation shares the same status wrapper because the two
    sides are rendered in different languages and therefore have no common
    concrete value.
    """
    projection = status_projection(side_passed)
    return [
        {
            "observation_id": case["observation_id"],
            "requirement_ids": list(case.get("requirement_ids", [])),
            "projection": dict(projection),
        }
        for case in corpus["cases"]
    ]


def build_report(corpus, side_passed):
    """Build the side report with the SKELETON-owned envelope when available."""
    projections = project_observations(corpus, side_passed)
    report = _report_module()
    if report is not None:
        results = [
            report.make_result(
                entry["observation_id"],
                side_passed,
                json.dumps(entry["projection"], sort_keys=True),
            )
            for entry in projections
        ]
        return report.make_report(BATCH_ID, results)
    return {
        "schema_version": "migration-result:v1",
        "batch_id": BATCH_ID,
        "results": [
            {
                "observation_id": entry["observation_id"],
                "passed": bool(side_passed),
                "detail": json.dumps(entry["projection"], sort_keys=True),
            }
            for entry in projections
        ],
    }


def write_report(path, report):
    report_mod = _report_module()
    if report_mod is not None and hasattr(report_mod, "write_report"):
        return report_mod.write_report(path, report)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
