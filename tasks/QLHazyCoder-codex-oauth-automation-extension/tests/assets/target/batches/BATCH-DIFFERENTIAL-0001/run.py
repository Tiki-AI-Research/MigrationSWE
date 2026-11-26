"""Target-tree differential probe entry point for BATCH-DIFFERENTIAL-0001.

Executes the shared probe corpus against the read-only /input/target checkout and
emits the normalized observation report on stdout.
"""
import os
import runpy
import sys

BATCH_ID = "BATCH-DIFFERENTIAL-0001"
HERE = os.path.dirname(os.path.abspath(__file__))
SHARED_PROBE = os.path.abspath(
    os.path.join(HERE, "..", "..", "..", "shared", "batches", BATCH_ID, "differential_probe.py")
)
TREE = os.environ.get("DIFFERENTIAL_TARGET_TREE", "/input/target")

sys.argv = ["differential_probe.py", TREE, "target"]
runpy.run_path(SHARED_PROBE, run_name="__main__")
