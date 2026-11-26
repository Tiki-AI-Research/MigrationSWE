#!/usr/bin/env python3
"""Target-side probes for BATCH-F2P-0003.

Evaluates each F2P observation as a target-only postcondition against a copy of
the Go reference target. Every observation is expected to PASS. The probes run
real Go builds/tests; results are never hardcoded per role.

This file is a per-batch authoring asset, not the public harness entry point.
The INTEGRATION batch assembles the final ``harness.py`` from it.
"""

import argparse
import importlib.util
import os
import sys
from pathlib import Path

BATCH_ID = "BATCH-F2P-0003"


def load_shared():
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "shared" / "batches" / BATCH_ID / "probes.py"
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("f2p_probes", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit("shared probes.py for %s not found" % BATCH_ID)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="/input/target")
    parser.add_argument("--out", default="/logs/f2p-0003-target.json")
    args = parser.parse_args(argv)

    probes = load_shared()
    test_file = str(Path(__file__).resolve().parent / probes.GO_TEST_FILE)
    if not os.path.isfile(test_file):
        raise SystemExit("missing Go observation test file: %s" % test_file)

    results, transcript = probes.evaluate(args.target, test_file)
    report = probes.make_report(results)
    probes.write_report(args.out, report)

    for line in transcript:
        print(line)
    failures = [r for r in report["results"] if r["status"] != "PASS"]
    for entry in report["results"]:
        print("  %-8s %s  %s" % (entry["observation_id"], entry["status"], entry["detail"]))
    print("target report: %s" % args.out)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
