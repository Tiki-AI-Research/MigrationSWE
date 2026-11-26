"""Run a single BATCH-F2P-0003 predicate in an isolated interpreter.

Usage: python run_one.py <observation_id> <repo_root>

The result is emitted as a single JSON object prefixed by RESULT_MARKER so the
parent harness can locate it even when Django writes to stdout/stderr.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from checks import run_check  # noqa: E402

RESULT_MARKER = "__F2P_RESULT__"


def main():
    observation_id = sys.argv[1]
    repo_root = sys.argv[2] if len(sys.argv) > 2 else os.environ.get(
        "MIGRATION_REPO", "/workspace/repo"
    )
    result = run_check(observation_id, repo_root)
    sys.stdout.write(RESULT_MARKER + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
