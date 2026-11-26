#!/usr/bin/env python3
"""Target probe for BATCH-DIFFERENTIAL-0001.

Builds the post-migration Go implementation from the reference target
(``/input/target`` by default, overridable with ``TARGET_REPO``) and drives it
through the shared differential corpus. The result is a normalized observation
per case: process exit code, ordered GitHub REST requests, action outputs and
the normalized ``::error::`` diagnostic.

Every potentially long command (``go build`` and each case subprocess) is
wrapped in an explicit timeout, so this probe can never block forever.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH_ID = "BATCH-DIFFERENTIAL-0001"
ASSET_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SHARED = os.path.join(ASSET_ROOT, "shared", "batches", BATCH_ID)
sys.path.insert(0, os.path.join(SHARED, "lib"))

import harness  # noqa: E402

BUILD_TIMEOUT = 300
CASE_TIMEOUT = 60


def build_target(repo_root: str) -> tuple[str, str]:
    """Build the candidate's real action executable.

    The migration may place Go under ``src/``/``cmd/`` (or any other layout),
    so the probe discovers the actual main package with ``go list`` instead of
    assuming a flat top-level ``package main``.
    """
    build_dir = tempfile.mkdtemp(prefix="diff-target-build-")
    binary = os.path.join(build_dir, "release-action")
    env = dict(os.environ)

    candidates = [
        os.environ.get("GOMODCACHE", ""),
        "/opt/migration/dependencies/gomodcache",
        "/workspace/repo/.gomodcache",
    ]
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "github.com/bmatcuk/doublestar")):
            env["GOMODCACHE"] = candidate
            break

    env.setdefault("GOPROXY", "off")
    env.setdefault("GOFLAGS", "-mod=mod")
    env["GOCACHE"] = os.path.join(build_dir, ".gocache")

    import_path = harness.discover_main_package(repo_root, env=env, timeout=BUILD_TIMEOUT)
    if not import_path:
        raise RuntimeError("cannot identify the Go action executable")

    completed = subprocess.run(
        ["go", "build", "-o", binary, import_path],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"go build failed:\n{completed.stderr}")
    return binary, build_dir


def run(repo_root: str, report_path: str) -> dict:
    binary, build_dir = build_target(repo_root)
    cases = harness.load_cases(os.path.join(SHARED, "corpus", "cases.json"))

    results = {}
    try:
        for case in cases:
            results[case["id"]] = harness.evaluate_case(case, [binary], repo_root, timeout=CASE_TIMEOUT)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

    report = {
        "schema_version": "migration-differential-report:v1",
        "batch_id": BATCH_ID,
        "runtime": "target",
        "cases": results,
        "observations": harness.results_by_observation(cases, results),
    }
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    return report


def main() -> int:
    repo_root = os.environ.get("TARGET_REPO", "/input/target")
    report_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/diff-target-report.json"
    report = run(repo_root, report_path)
    executed = sum(1 for r in report["cases"].values() if "exit_code" in r)
    static = sum(1 for r in report["cases"].values() if r.get("kind") == "static")
    print(f"target probe: {executed} http + {static} static cases executed on {repo_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
