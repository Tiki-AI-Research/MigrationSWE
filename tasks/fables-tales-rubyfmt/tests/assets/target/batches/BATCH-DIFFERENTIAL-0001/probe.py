#!/usr/bin/env python3
"""Target probe for BATCH-DIFFERENTIAL-0001.

Builds the Go ``rubyfmt`` executable from the reference target and executes the
shared differential corpus.  The probe exits non-zero if any observation
deviates from the source-normalized expectation.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parents[3]
SHARED_CORPUS = (
    ASSET_ROOT / "shared" / "batches" / "BATCH-DIFFERENTIAL-0001" / "corpus.py"
)
EXPECTED = (
    ASSET_ROOT
    / "fixtures"
    / "batches"
    / "BATCH-DIFFERENTIAL-0001"
    / "expected-results.json"
)


def build(repo, output):
    env = dict(os.environ)
    env.update(
        {
            "CGO_ENABLED": env.get("CGO_ENABLED", "1"),
            "GOFLAGS": env.get("GOFLAGS", "-mod=vendor"),
            "GOTOOLCHAIN": env.get("GOTOOLCHAIN", "local"),
            "GOCACHE": env.get("GOCACHE", tempfile.mkdtemp(prefix="rubyfmt-gocache-")),
        }
    )
    command = [
        "go",
        "build",
        "-mod=vendor",
        "-buildvcs=false",
        "-o",
        str(output),
        "./cmd/rubyfmt",
    ]
    completed = subprocess.run(command, cwd=str(repo), env=env)
    if completed.returncode != 0:
        raise SystemExit("go build failed with exit code %d" % completed.returncode)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="/input/target")
    parser.add_argument("--output")
    parser.add_argument("--expected", default=str(EXPECTED))
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--binary")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    workdir = Path(tempfile.mkdtemp(prefix="rubyfmt-target-probe-"))
    binary = Path(args.binary) if args.binary else workdir / "rubyfmt"
    if not args.no_build:
        build(repo, binary)

    output = args.output or str(workdir / "target-results.json")
    command = [
        sys.executable,
        str(SHARED_CORPUS),
        "--binary",
        str(binary),
        "--repo",
        str(repo),
        "--output",
        output,
        "--expected",
        args.expected,
    ]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
