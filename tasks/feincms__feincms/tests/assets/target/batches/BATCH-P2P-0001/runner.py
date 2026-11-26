#!/usr/bin/env python3
"""Target-side executable entry point for BATCH-P2P-0001.

Runs the shared P2P probes against the reference target snapshot repository and
emits the raw per-observation PASS/FAIL projection for this side.  The file
carries a shebang and the executable bit so it can run either directly or via
``python3``.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SIDE = "target"

_CANDIDATES = [
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
        "shared",
        "batches",
        "BATCH-P2P-0001",
    ),
    "/assets/shared/batches/BATCH-P2P-0001",
    os.path.join(HERE, "..", "..", "shared", "batches", "BATCH-P2P-0001"),
]

for _candidate in _CANDIDATES:
    _candidate = os.path.abspath(_candidate)
    if os.path.isfile(os.path.join(_candidate, "probe.py")):
        if _candidate not in sys.path:
            sys.path.insert(0, _candidate)
        break

import probe  # noqa: E402


def main(argv):
    return probe.run_cli(SIDE, list(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
