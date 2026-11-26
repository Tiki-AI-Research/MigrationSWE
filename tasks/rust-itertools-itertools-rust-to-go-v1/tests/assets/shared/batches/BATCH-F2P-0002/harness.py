"""Shared F2P probe harness for BATCH-F2P-0002.

This module is a tests asset. It executes the same Go probe
(``target/batches/BATCH-F2P-0002/f2p_probe_test.go``) against both the source
tree (``/input/source``) and the reference target tree (``/input/target``) and
emits one ``migration-result:v1`` report per side.

The probe file is added to the repository under test with ``go test -overlay``
so the read-only authoring mounts are executed in place and never modified. The
target side compiles the Go module and runs one ``TestF2P_<id>`` function per
observation. The source side runs the very same command; because the Rust
source tree carries no Go implementation of the frozen contract, the target
postcondition is not satisfied and every observation reports ``FAIL``. Only a
failure to even reach the compiler (missing toolchain, missing asset, timeout,
crash) is reported as ``INFRA_ERROR``; any other failed probe build is a
semantic ``FAIL`` because the repository does not provide the targeted Go
package or symbol.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

BATCH_ID = "BATCH-F2P-0002"
RESULT_SCHEMA_VERSION = "migration-result:v1"
TEST_PASS_RE = re.compile(r"^\s*--- PASS: (TestF2P_\d+)", re.MULTILINE)
TEST_FAIL_RE = re.compile(r"^\s*--- FAIL: (TestF2P_\d+)", re.MULTILINE)


def asset_root():
    """Return the asset root (the directory holding shared/, target/, ...)."""
    return pathlib.Path(__file__).resolve().parents[3]


def load_observations(root=None):
    root = pathlib.Path(root) if root else asset_root()
    path = root / "fixtures" / "batches" / BATCH_ID / "observations.json"
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def probe_path(root=None):
    root = pathlib.Path(root) if root else asset_root()
    return root / "target" / "batches" / BATCH_ID / "f2p_probe_test.go"


def go_env():
    env = dict(os.environ)
    env.setdefault("GOTOOLCHAIN", "local")
    env.setdefault("GOFLAGS", "")
    env.setdefault("GOPROXY", "off")
    env.setdefault("GOSUMDB", "off")
    env.setdefault("GOCACHE", tempfile.mkdtemp(prefix="f2p-gocache-"))
    return env


def go_available():
    return shutil.which("go") is not None


def _run(cmd, cwd):
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=go_env(),
        capture_output=True,
        text=True,
        check=False,
    )


INFRA_FAILURE_MARKERS = (
    "command not found",
    "[Errno 2]",
    "executable file not found",
    "not found; is the shared asset assembled",
    "not found; is the fixture asset assembled",
    "dependency snapshot is missing",
    "timeout",
    "timed out",
    "TimeoutExpired",
    "Expecting value",
    "JSONDecodeError",
    "Traceback (most recent call last)",
)


def _classify_infra(output):
    """Return True when the probe could not even reach or run the compiler.

    A missing toolchain, a missing probe asset, a timeout, or a crashed harness
    is an infrastructure fault. Any other failed probe build means the tree does
    not provide the targeted Go package or symbol, which is a semantic ``FAIL``.
    """
    text = str(output or "")
    return any(marker in text for marker in INFRA_FAILURE_MARKERS)


def run_side(repo, root=None):
    """Execute the frozen target postconditions against ``repo``.

    Returns a list of result dicts ``{observation_id, passed, detail}``.
    """
    root = pathlib.Path(root) if root else asset_root()
    spec = load_observations(root)
    observations = spec["observations"]
    repo = pathlib.Path(repo)

    if not go_available():
        return [
            {"observation_id": obs["id"], "passed": False,
             "detail": "INFRA_ERROR: go toolchain not available"}
            for obs in observations
        ]

    overlay = pathlib.Path(tempfile.mkdtemp(prefix="f2p-overlay-")) / "overlay.json"
    with open(overlay, "w", encoding="utf-8") as handle:
        json.dump({"Replace": {str(repo / "f2p_probe_test.go"): str(probe_path(root))}}, handle)

    build = _run(["go", "build", "./..."], repo)
    test = _run(
        ["go", "test", "-vet=off", "-overlay=" + str(overlay), "-run", "TestF2P_", "-v", "./"],
        repo,
    )
    combined = (build.stdout + build.stderr + test.stdout + test.stderr)

    passed = set(TEST_PASS_RE.findall(test.stdout))
    failed = set(TEST_FAIL_RE.findall(test.stdout))
    built_ok = build.returncode == 0 and test.returncode == 0

    results = []
    for obs in observations:
        name = obs["go_test"]
        if name in passed:
            results.append({"observation_id": obs["id"], "passed": True,
                            "detail": "target postcondition satisfied by Go execution path"})
        elif name in failed:
            results.append({"observation_id": obs["id"], "passed": False,
                            "detail": "target postcondition not satisfied"})
        elif _classify_infra(combined):
            results.append({"observation_id": obs["id"], "passed": False,
                            "detail": "INFRA_ERROR: probe could not be built or executed"})
        else:
            results.append({"observation_id": obs["id"], "passed": False,
                            "detail": "target Go implementation absent in this tree"})
    _ = built_ok
    return results


def emit(report_path, results):
    report = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "results": list(results),
    }
    report_path = pathlib.Path(report_path)
    if report_path.parent:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(side, repo, report_path, root=None):
    results = run_side(repo, root=root)
    emit(report_path, results)
    print(json.dumps({"side": side, "batch_id": BATCH_ID,
                      "passed": sum(1 for r in results if r["passed"]),
                      "total": len(results)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: harness.py <side> <repo> <report>")
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
