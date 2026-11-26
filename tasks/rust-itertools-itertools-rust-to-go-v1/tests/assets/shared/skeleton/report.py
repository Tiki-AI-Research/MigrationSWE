"""Shared result-report helpers used by the source and target harnesses.

The final grader reads a single JSON report. Both the source harness and the
target harness must emit the exact same envelope shape; keeping the builder in
one shared module is what makes that guarantee possible.
"""

import json
import os

RESULT_SCHEMA_VERSION = "migration-result:v1"


def make_result(observation_id, passed, detail):
    """Build one observation result entry."""
    return {
        "observation_id": observation_id,
        "passed": bool(passed),
        "detail": str(detail),
    }


def make_report(batch_id, results):
    """Build the top-level report envelope."""
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "batch_id": batch_id,
        "results": list(results),
    }


def write_report(path, report):
    """Serialize a report to ``path``, creating parent directories as needed."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
