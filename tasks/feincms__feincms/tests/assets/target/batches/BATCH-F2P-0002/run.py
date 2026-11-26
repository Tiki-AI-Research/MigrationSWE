#!/usr/bin/env python3
"""Execute the BATCH-F2P-0002 F2P predicates against the target repository.

The same predicates are shipped for both sides; this entry point only fixes the
repository under observation.  Every command runs through the shared harness
which enforces an explicit timeout, so this entry point can never block
forever.
"""

import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CHECK_REL = os.path.join(
    "shared", "batches", "BATCH-F2P-0002", "checks.py"
)
HARNESS_REL = os.path.join(
    "shared", "batches", "BATCH-F2P-0002", "harness.py"
)
REPO_ROOT = os.environ.get("REPO_ROOT", "/input/target")


def _load(rel_path, module_name):
    path = os.path.join(ASSET_ROOT, rel_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=REPO_ROOT)
    parser.add_argument(
        "--report",
        default=os.environ.get("MIGRATION_REPORT", "/logs/result.json"),
    )
    args = parser.parse_args(argv)

    checks = _load(CHECK_REL, "f2p0002_target_checks")
    harness = _load(HARNESS_REL, "f2p0002_target_harness")

    results = checks.run_checks(args.repo)
    harness.write_report(args.report, results)

    failed = [result for result in results if not result.passed]
    for result in results:
        sys.stderr.write(
            "{observation}: {status}\n".format(
                observation=result.observation_id,
                status="PASS" if result.passed else "FAIL",
            )
        )
    sys.stderr.write(
        "{total} probes, {failed} failed\n".format(
            total=len(results), failed=len(failed)
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
