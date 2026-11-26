#!/usr/bin/env python3
"""Target-side runtime entry point for BATCH-INTEGRATION-0001.

Invoked as ``python3 {assets}/harness.py {report}``. It dispatches every
required runtime observation (P2P, F2P and DIFFERENTIAL) against the target
repository under test and writes a migration-observations:v6 report.
"""

import os
import sys

ASSET_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ASSET_ROOT, "shared", "integration"))

import harness_lib  # noqa: E402

if __name__ == "__main__":
    report = sys.argv[1] if len(sys.argv) > 1 else "-"
    sys.exit(harness_lib.main("target", report, ASSET_ROOT))
