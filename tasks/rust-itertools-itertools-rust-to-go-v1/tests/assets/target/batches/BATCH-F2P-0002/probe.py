#!/usr/bin/env python3
"""Target-side F2P probe entry for BATCH-F2P-0002.

Runs the frozen target postconditions against the reference target tree
(default ``/input/target``) and writes a ``migration-result:v1`` report.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared" / "batches" / "BATCH-F2P-0002"))

import harness  # noqa: E402


def main(argv):
    repo = argv[1] if len(argv) > 1 else "/input/target"
    report = argv[2] if len(argv) > 2 else "/logs/f2p-0002-target.json"
    return harness.main("target", repo, report)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
