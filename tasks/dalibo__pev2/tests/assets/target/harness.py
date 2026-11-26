#!/usr/bin/env python3
"""Target-side integration harness for BATCH-INTEGRATION-0001.

Merges every dependency batch's target-side probe into one runtime report with
schema ``migration-observations:v6``.  The asset root is derived from this
file's location (``<assets>/target/harness.py``); only ``shared/``, ``target/``
and ``fixtures/`` are read at run time.

Usage:
    python3 target/harness.py <report.json>

Repo root comes from MIGRATION_REPO_ROOT / MIGRATION_REPO / REPO_ROOT / REPO
(first non-empty wins), defaulting to /workspace/repo.  Every batch command is
executed under an explicit ``timeout`` so this harness can never block forever.
"""

from __future__ import annotations

import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.dirname(HERE)
SHARED = os.path.join(ASSET_ROOT, "shared", "integration")
sys.path.insert(0, SHARED)

import dispatch  # noqa: E402  (located on the path above)


def main(argv):
    report_path = argv[1] if len(argv) > 1 else "/logs/result.json"
    timeout_seconds = int(os.environ.get("INTEGRATION_TEST_TIMEOUT_SEC", "1800"))
    dispatch.run_side("target", os.path.abspath(__file__), report_path, timeout_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
