#!/usr/bin/env python3
"""Execute the BATCH-CONTRACT-0001 public-contract probes on the target repo.

Usage: python3 probe.py [--repo /workspace/repo] [--report /logs/result.json]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys

SIDE = "target"
HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
HARNESS_PATH = os.path.join(
    ASSET_ROOT, "shared", "batches", "BATCH-CONTRACT-0001", "contract_harness.py"
)


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "batch_contract_0001_harness", HARNESS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo", default=os.environ.get("MIGRATION_REPO", "/workspace/repo")
    )
    parser.add_argument(
        "--report", default=os.environ.get("MIGRATION_REPORT", "/logs/result.json")
    )
    args = parser.parse_args(argv)

    harness = _load_harness()
    results = harness.collect(os.path.abspath(args.repo))
    harness.write_report(args.report, results)
    failed = [item[0] for item in results if not item[1]]
    print(
        f"[{SIDE}] contract probes: {len(results) - len(failed)}/{len(results)} ok"
    )
    if failed:
        print(f"[{SIDE}] failed: {', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
