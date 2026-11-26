"""Target-side F2P probe runner for BATCH-F2P-0003.

Executes every F2P atomic claim against the reference target tree.  The target
postconditions are expected to hold here, so this runner must report a pass for
all 24 observations.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CORE_DIR = os.path.join(ASSET_ROOT, "shared", "batches", "BATCH-F2P-0003")
sys.path.insert(0, CORE_DIR)

import f2p_probe_core  # noqa: E402


def main():
    repo_root = os.environ.get("PROBE_ROOT", "/input/target")
    out_path = os.path.join(HERE, "probe-results.json")
    payload = f2p_probe_core.run(repo_root, out_path)
    print("target probe completed")
    return 0 if payload["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
