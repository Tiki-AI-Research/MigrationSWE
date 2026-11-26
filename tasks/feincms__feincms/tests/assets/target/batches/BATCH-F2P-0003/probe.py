"""Run every BATCH-F2P-0003 target postcondition against the migrated tree.

The probe resolves the repository to inspect without hardcoding an outcome:
it accepts an explicit repository path, honours ``MIGRATION_REPO``, and
otherwise selects the first existing checkout (``/input/target`` during
authoring or ``/workspace/repo`` at runtime).  The exact same predicate module
is used by the source probe, so a PASS on one side cannot be produced by a
role-specific branch.
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ASSETS_ROOT = Path(os.environ.get("F2P0003_ASSETS_ROOT", HERE.parents[3]))
SHARED = ASSETS_ROOT / "shared" / "batches" / "BATCH-F2P-0003"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

from harness import (  # noqa: E402
    DEFAULT_REPO,
    OBSERVATION_IDS,
    parse_probe_arguments,
    run_batch,
    write_report,
)

SIDE = "target"


def run(repo_root=None, report=None):
    """Return (results, report_path) for every owned observation."""
    repo_root = repo_root or DEFAULT_REPO
    results = run_batch(repo_root)
    if report:
        write_report(report, results)
    return results, report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    repo_root, report = parse_probe_arguments(SIDE, argv)
    if report is None:
        report = os.environ.get(
            "MIGRATION_REPORT", "/tmp/batch-f2p-0003-target.json"
        )
    results = run_batch(repo_root)
    try:
        write_report(report, results)
    except OSError:
        pass
    print(json.dumps(results, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
