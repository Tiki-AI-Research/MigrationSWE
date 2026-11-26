#!/usr/bin/env python3
"""Target-side integration entry point for the ``omg-mcp-server`` migration.

Executed by the target runtime as
``{repo}/mcp-server/.venv/bin/python {assets}/run_tests.py {report}``.  It
dispatches every target-side batch probe, including the pass-to-pass corpus and
the differential corpus (``batch-exec`` therefore covers P2P, not only F2P),
and writes the ``migration-observations:v6`` report to ``{report}``.

The shared dispatch/aggregation logic lives in
``shared/integration/harness.py``.
"""

from __future__ import annotations

import os
import sys

ASSET_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ASSET_ROOT, "shared", "integration"))

import harness  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(harness.main("target", sys.argv))
