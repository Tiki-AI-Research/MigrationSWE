#!/usr/bin/env python3
"""Execute the BATCH-F2P-0001 target probe against a Go target tree.

This runner is *private authoring* support: it materializes a throwaway copy of
the reference target, injects the Go probe (``f2p_probe_test.go``) and runs it
so that every observation's PASS is backed by a real execution on the Go 1.23.0
toolchain. The reference target itself is never modified or copied into a public
asset.

Usage:
    run_probe.py <report.json> [target_dir]

``target_dir`` defaults to ``/input/target``.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BATCH_ID = "BATCH-F2P-0001"
RESULT_SCHEMA_VERSION = "migration-result:v1"

OBSERVATION_IDS = [
    "F2P-001", "F2P-003", "F2P-004", "F2P-005", "F2P-006", "F2P-007",
    "F2P-008", "F2P-013", "F2P-016", "F2P-022", "F2P-024", "F2P-002",
    "F2P-009", "F2P-010", "F2P-011", "F2P-012", "F2P-014", "F2P-015",
    "F2P-017", "F2P-018", "F2P-019", "F2P-020", "F2P-021", "F2P-023",
]

PROBE_FILENAME = "f2p_probe_test.go"
PROBE_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), PROBE_FILENAME)

TEST_LINE = re.compile(r"^--- (PASS|FAIL|SKIP): (TestF2P\d{3})")


def write_report(path, results):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    report = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")


def run(target_dir):
    workdir = tempfile.mkdtemp(prefix="f2p-target-")
    module = os.path.join(workdir, "itertools")
    shutil.copytree(target_dir, module, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    shutil.copy2(PROBE_SOURCE, os.path.join(module, PROBE_FILENAME))

    proc = subprocess.run(
        ["go", "test", "-run", "TestF2P", "-v", "."],
        cwd=module,
        capture_output=True,
        text=True,
    )
    output = proc.stdout + "\n" + proc.stderr

    seen = {}
    for line in output.splitlines():
        match = TEST_LINE.match(line.strip())
        if not match:
            continue
        status, test_name = match.group(1), match.group(2)
        obs = "F2P-" + test_name[len("TestF2P"):]
        seen[obs] = (status == "PASS", line.strip())

    results = []
    for obs in OBSERVATION_IDS:
        if obs in seen:
            passed, detail = seen[obs]
        else:
            passed, detail = False, "probe test did not report a result (build or run failure)"
        results.append({"observation_id": obs, "passed": bool(passed), "detail": detail})
    shutil.rmtree(workdir, ignore_errors=True)
    return results, proc.returncode, output


def main(argv):
    report = argv[1] if len(argv) > 1 else "/logs/result.json"
    target_dir = argv[2] if len(argv) > 2 else "/input/target"
    results, rc, output = run(target_dir)
    write_report(report, results)
    sys.stderr.write(output)
    return 0 if rc == 0 and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
