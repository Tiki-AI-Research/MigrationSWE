#!/usr/bin/env python3
"""Target-side runner for BATCH-F2P-0002.

Executes the shared F2P probes against the reference target checkout.
"""
import pathlib
import subprocess
import sys


HERE = pathlib.Path(__file__).resolve()
REPO_ROOT = HERE.parents[3]
SHARED_PROBE = REPO_ROOT / "shared" / "batches" / "BATCH-F2P-0002" / "probe.py"


def main(argv):
    target = pathlib.Path(argv[1]) if len(argv) > 1 else pathlib.Path("/input/target")
    output = pathlib.Path(argv[2]) if len(argv) > 2 else HERE.with_name("probe-results.json")
    return subprocess.call([sys.executable, str(SHARED_PROBE), str(target), str(output)])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
