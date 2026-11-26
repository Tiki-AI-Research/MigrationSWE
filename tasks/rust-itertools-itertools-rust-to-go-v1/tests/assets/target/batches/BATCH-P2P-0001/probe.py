#!/usr/bin/env python3
"""Target-side P2P probe for BATCH-P2P-0001.

The target side is the migrated Go module.  The probe executes the Go test
suite of a real target repository (``go test ./...``) and maps every
observation to the resulting stable projection.  The target probe must be
executed against a concrete repository before its status is reported; it never
writes results without running the tests.

Usage:  probe.py [REPO] [REPORT]
        REPO    defaults to /input/target
        REPORT  optional output path for the JSON result envelope
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "shared", "batches", "BATCH-P2P-0001"))

import projection  # noqa: E402


def _go_env():
    env = dict(os.environ)
    env.setdefault("CGO_ENABLED", "0")
    env.setdefault("GOFLAGS", "-mod=mod")
    env.setdefault("GOPROXY", "off")
    env.setdefault("GOTOOLCHAIN", "local")
    return env


def run_target(repo):
    if not os.path.isfile(os.path.join(repo, "go.mod")):
        return False, "go.mod not found"
    completed = subprocess.run(
        ["go", "test", "./..."],
        cwd=repo,
        env=_go_env(),
        capture_output=True,
        text=True,
    )
    sys.stderr.write(completed.stderr)
    detail = "go test ./... rc=%d" % completed.returncode
    return completed.returncode == 0, detail


def main(argv):
    repo = argv[1] if len(argv) > 1 else "/input/target"
    report_path = argv[2] if len(argv) > 2 else None

    if not os.path.isdir(repo):
        sys.stderr.write("target repo not found: %s\n" % repo)
        return 2

    corpus = projection.load_corpus()
    passed, detail = run_target(repo)
    report = projection.build_report(corpus, passed)
    report["side"] = "target"
    report["native_detail"] = detail

    text = json.dumps(report, indent=2, sort_keys=True)
    if report_path:
        projection.write_report(report_path, report)
    else:
        sys.stdout.write(text + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
