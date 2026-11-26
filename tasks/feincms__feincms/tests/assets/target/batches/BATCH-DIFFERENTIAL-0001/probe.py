"""Target-side probe entry point for BATCH-DIFFERENTIAL-0001.

Runs the shared differential probes against the reference target checkout.
This module is intentionally *not* ``target/runner.py``; the top level runner is
owned by the INTEGRATION batch.  It exposes the same callable surface as the
shared probes (``run``/``collect``/``OBSERVATION_IDS``/``PROBES``) so that an
integration entry point can dispatch it either as a subprocess or by importing
it.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH_ID = os.path.basename(HERE)
SHARED_DIR = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "shared", "batches", BATCH_ID)
)
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

import probes  # noqa: E402

SIDE = "target"
DEFAULT_REPO = os.environ.get("MIGRATION_TARGET_REPO", "/input/target")

OBSERVATION_IDS = probes.OBSERVATION_IDS
PROBES = probes.PROBES
collect = probes.collect


def run(repo_root=None, side=SIDE):
    return probes.run(repo_root or DEFAULT_REPO, side)


def _parse(argv):
    repo = None
    report = None
    positional = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--repo", "--report") and index + 1 < len(argv):
            if arg == "--repo":
                repo = argv[index + 1]
            else:
                report = argv[index + 1]
            index += 2
            continue
        if arg.startswith("--repo="):
            repo = arg.split("=", 1)[1]
        elif arg.startswith("--report="):
            report = arg.split("=", 1)[1]
        else:
            positional.append(arg)
        index += 1
    if repo is None and positional:
        repo = positional[0]
    if report is None and len(positional) > 1:
        report = positional[1]
    return repo, report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    repo, report = _parse(argv)
    if repo is None:
        repo = DEFAULT_REPO
    if report is None:
        report = os.environ.get("MIGRATION_REPORT")
    payload = run(repo, SIDE)
    if report:
        probes.harness.write_report(report, payload)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
