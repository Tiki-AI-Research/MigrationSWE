#!/usr/bin/env python3
"""F2P target harness for BATCH-F2P-0002.

Builds and runs the Rust F2P probe against the migrated target repository and
reports, per observation, whether the target postcondition holds.

Every potentially long-running command is wrapped in an explicit ``timeout``;
the harness never waits indefinitely. The probe is copied into a throwaway
working tree so the inspected repository is never mutated.

Usage:
    python3 harness.py [REPORT] [--repo REPO_PATH]

When REPORT is omitted the JSON result is printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BATCH_ID = "BATCH-F2P-0002"
HERE = Path(__file__).resolve().parent
PROBE_SRC = HERE / "f2p_probe.rs"

BUILD_TIMEOUT = 900
RUN_TIMEOUT = 600
COPY_TIMEOUT = 300

OBSERVATIONS = [
    "F2P-028", "F2P-040", "F2P-041", "F2P-042", "F2P-047", "F2P-027",
    "F2P-030", "F2P-031", "F2P-036", "F2P-037", "F2P-045", "F2P-046",
    "F2P-025", "F2P-026", "F2P-029", "F2P-032", "F2P-033", "F2P-034",
    "F2P-035", "F2P-038", "F2P-039", "F2P-043", "F2P-044", "F2P-048",
]


def run(cmd: list[str], *, cwd: str | None, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _has_registry(cargo_home: str | None) -> bool:
    if not cargo_home:
        return False
    cache = Path(cargo_home) / "registry" / "cache"
    try:
        return cache.is_dir() and any(cache.iterdir())
    except OSError:
        return False


def resolve_repo(explicit: str | None) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env_repo = os.environ.get("TARGET_REPO")
    if env_repo:
        candidates.append(Path(env_repo))
    candidates.extend([Path("/workspace/repo"), Path("/input/target")])
    for candidate in candidates:
        if (candidate / "Cargo.toml").is_file() and (candidate / "crates").is_dir():
            return candidate
    raise SystemExit("target repository (Cargo.toml + crates/) not found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?", default=None)
    parser.add_argument("--repo", default=None)
    args = parser.parse_args()

    result = {"batch_id": BATCH_ID, "kind": "F2P", "observations": {}}

    try:
        repo = resolve_repo(args.repo)
    except SystemExit as exc:
        payload = {"batch_id": BATCH_ID, "error": str(exc), "observations": {}}
        _emit(payload, args.report)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="f2p-0002-"))
    try:
        dst = workdir / "repo"
        copied = run(
            ["timeout", str(COPY_TIMEOUT), "cp", "-a", str(repo), str(dst)],
            cwd=None,
            timeout=COPY_TIMEOUT + 30,
        )
        if copied.returncode != 0:
            raise RuntimeError(f"copy failed: {copied.stderr.strip()}")

        examples = dst / "crates" / "pev2-core" / "examples"
        examples.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROBE_SRC, examples / "f2p_probe.rs")

        env = dict(os.environ)
        env.setdefault("CARGO_NET_OFFLINE", "true")
        cargo_home = env.get("CARGO_HOME")
        if not _has_registry(cargo_home) and _has_registry("/opt/migration/dependencies/cargo"):
            env["CARGO_HOME"] = "/opt/migration/dependencies/cargo"

        manifest = str(dst / "Cargo.toml")
        proc = subprocess.run(
            [
                "timeout", str(BUILD_TIMEOUT),
                "cargo", "run", "--offline",
                "--manifest-path", manifest, "--example", "f2p_probe",
            ],
            cwd=str(dst),
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT + 30,
            env=env,
            check=False,
        )

        if proc.returncode != 0:
            payload = {
                "batch_id": BATCH_ID,
                "error": "probe build/run failed",
                "stderr": proc.stderr[-4000:],
                "observations": {obs: {"ok": False, "detail": "probe build/run failure"} for obs in OBSERVATIONS},
            }
            _emit(payload, args.report)
            return 3

        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "{}"
        probe = json.loads(line)
        for obs in OBSERVATIONS:
            result["observations"][obs] = probe.get(obs, {"ok": False, "detail": "missing from probe output"})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    _emit(result, args.report)
    failed = [obs for obs in OBSERVATIONS if not result["observations"][obs].get("ok")]
    return 0 if not failed else 1


def _emit(payload: dict, report: str | None) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    if report:
        report_path = Path(report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
