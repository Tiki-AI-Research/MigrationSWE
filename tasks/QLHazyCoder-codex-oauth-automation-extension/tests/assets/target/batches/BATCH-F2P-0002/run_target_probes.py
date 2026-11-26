"""Target-side driver: execute BATCH-F2P-0002 probes against /input/target."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SHARED = os.path.join(REPO_ROOT, "shared", "batches", "BATCH-F2P-0002")
if SHARED not in sys.path:
    sys.path.insert(0, SHARED)

import probe_runner  # noqa: E402


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "/input/target"
    output = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        REPO_ROOT, "fixtures", "batches", "BATCH-F2P-0002", "target_probe_results.json"
    )
    results = probe_runner.run(root, output)
    print("target root:", root)
    print({key: value["status"] for key, value in results.items()})


if __name__ == "__main__":
    main()
