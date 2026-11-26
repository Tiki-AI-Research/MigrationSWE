#!/usr/bin/env python3
"""Target-side integration entry point.

Invoked by the target runner registry as::

    python3 <assets>/run_tests.py <report>

It dispatches every dependency batch's executable projection against the
repository under evaluation (``MIGRATION_REPO`` / ``/workspace/repo``) and
writes the unified ``migration-observations:v6`` runtime report.
"""

import os
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.path.join(HERE, "shared", "integration")
if SHARED not in sys.path:
    sys.path.insert(0, SHARED)

import integration  # noqa: E402


if __name__ == "__main__":
    sys.exit(integration.main(sys.argv[1:], "target"))
