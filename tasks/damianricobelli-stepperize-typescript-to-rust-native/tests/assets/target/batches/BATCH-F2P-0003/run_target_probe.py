#!/usr/bin/env python3
"""Target-side entry point for BATCH-F2P-0003.

Thin wrapper around the shared probe runner. The shared runner evaluates the
same semantic predicate used on the source side; the target repository
provides the Rust crate, so the postconditions hold.
"""

import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "shared", "batches", "BATCH-F2P-0003")
)
if SHARED not in sys.path:
    sys.path.insert(0, SHARED)

import f2p_probe  # noqa: E402

if __name__ == "__main__":
    sys.exit(f2p_probe.main(sys.argv[1:], default_side="target"))
