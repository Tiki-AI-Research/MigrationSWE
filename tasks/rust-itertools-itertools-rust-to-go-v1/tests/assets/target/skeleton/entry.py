"""Skeleton entry point for the target-side (Go) harness.

The INTEGRATION batch assembles the final target harness from this skeleton.
This file intentionally does not live at the top-level `harness.py` path.
"""

import subprocess
import sys


def run_go_tests(repo):
    """Run the migrated Go module's test suite from ``repo``."""
    return subprocess.run(
        ["go", "test", "./..."],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def main(argv):
    repo = argv[1] if len(argv) > 1 else "."
    completed = run_go_tests(repo)
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
