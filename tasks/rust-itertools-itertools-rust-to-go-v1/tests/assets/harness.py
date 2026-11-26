#!/usr/bin/env python3
"""Target-side runtime harness entry (INTEGRATION batch).

Invoked as ``python3 {assets}/harness.py {report}``. It runs every dependency
batch's target probe against the repository under test and writes the merged
``migration-observations:v6`` report. The repository root is resolved from the
allowed environment aliases (``MIGRATION_REPO_ROOT``, ``MIGRATION_REPO``,
``REPO_ROOT``, ``REPO``) and defaults to ``/workspace/repo``.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "shared", "integration"))

import integration  # noqa: E402


def main(argv):
    report = argv[1] if len(argv) > 1 else "/logs/result.json"
    repo = argv[2] if len(argv) > 2 else None
    return integration.main("target", report, repo)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
