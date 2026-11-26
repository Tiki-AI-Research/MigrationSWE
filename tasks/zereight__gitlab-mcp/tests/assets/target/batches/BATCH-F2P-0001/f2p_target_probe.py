#!/usr/bin/env python3
"""Target-side probe for ``BATCH-F2P-0001``.

Runs the shared F2P predicates against the migrated Python ``mcp-server`` tree
(defaults to ``/input/target/mcp-server`` during authoring and
``/workspace/repo/mcp-server`` at grading time).  Every observation must
report ``PASS`` on this side.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SHARED = os.path.join(BATCH_ROOT, "shared", "batches", "BATCH-F2P-0001")

if SHARED not in sys.path:
    sys.path.insert(0, SHARED)

from f2p_observations import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main("target"))
