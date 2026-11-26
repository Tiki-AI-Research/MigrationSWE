#!/usr/bin/env python3
"""Target-side harness for BATCH-DIFFERENTIAL-0001.

Runs the differential probe (``differential_probe.rs``) against the Rust
candidate and reports one gate result per shared-corpus observation.

Every potentially long command is wrapped in an explicit ``timeout`` and the
probe is restricted with ``--test differential_probe`` so no unrelated or
blocking test case can run. The harness never waits indefinitely.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

BATCH_ID = "BATCH-DIFFERENTIAL-0001"
KIND = "DIFFERENTIAL"
SCHEMA_VERSION = "migration-asset-fragment:v2"
BUILD_TIMEOUT_SEC = 1800
RUN_TIMEOUT_SEC = 600

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SHARED = ROOT / "shared" / "batches" / BATCH_ID
sys.path.insert(0, str(SHARED))

try:
    import corpus as corpus_mod  # type: ignore
except Exception:  # pragma: no cover - fallback when shared module is absent
    corpus_mod = None

RESULT_RE = re.compile(r"(DIFF-\d{3})\s+(PASS|FAIL)\s*(.*)$")


def observation_ids() -> list[str]:
    if corpus_mod is not None:
        return corpus_mod.observation_ids(corpus_mod.load_corpus())
    return [f"DIFF-{i:03d}" for i in range(1, 33)]


def cargo_manifest(repo: Path) -> Path:
    return repo / "crates" / "pev2-core" / "Cargo.toml"


def timeout_cmd(seconds: int, argv: list[str]) -> list[str]:
    exe = shutil.which("timeout")
    if exe:
        return [exe, "--signal=TERM", "--kill-after=30", str(seconds), *argv]
    return argv


def run(argv: list[str], cwd: Path, timeout_sec: int) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("CARGO_NET_OFFLINE", "true")
    return subprocess.run(
        timeout_cmd(timeout_sec, argv),
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_sec + 60,
        errors="replace",
    )


def main() -> int:
    report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    repo = Path(os.environ.get("MIGRATION_REPO", "/workspace/repo"))
    manifest = cargo_manifest(repo)
    ids = observation_ids()

    results: dict[str, dict] = {oid: {"observation_id": oid, "status": "error"} for oid in ids}
    exit_code = None
    detail = ""

    probe_src = HERE / "differential_probe.rs"
    probe_dst = repo / "crates" / "pev2-core" / "tests" / "differential_probe.rs"

    if not manifest.exists():
        detail = f"cargo manifest not found: {manifest}"
    elif not probe_src.exists():
        detail = f"probe source not found: {probe_src}"
    else:
        probe_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(probe_src, probe_dst)
        try:
            argv = [
                "cargo",
                "test",
                "--offline",
                "--manifest-path",
                str(manifest),
                "--test",
                "differential_probe",
                "--",
                "--nocapture",
                "--test-threads=1",
            ]
            proc = run(argv, cwd=repo, timeout_sec=BUILD_TIMEOUT_SEC)
            exit_code = proc.returncode
            for line in proc.stdout.splitlines():
                m = RESULT_RE.search(line.strip())
                if not m:
                    continue
                oid, status, rest = m.group(1), m.group(2), m.group(3).strip()
                if oid in results:
                    results[oid] = {
                        "observation_id": oid,
                        "status": "pass" if status == "PASS" else "fail",
                        "detail": rest,
                    }
            missing = [oid for oid in ids if results[oid]["status"] == "error"]
            if missing:
                detail = "no probe result for: " + ", ".join(missing)
        except subprocess.TimeoutExpired:
            detail = "cargo test exceeded the explicit timeout"
        finally:
            try:
                if probe_dst.exists():
                    probe_dst.unlink()
            except OSError:
                pass

    ordered = [results[oid] for oid in ids]
    passed = sum(1 for r in ordered if r["status"] == "pass")
    failed = sum(1 for r in ordered if r["status"] == "fail")
    errors = sum(1 for r in ordered if r["status"] == "error")
    report = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "kind": KIND,
        "repo": str(repo),
        "exit_code": exit_code,
        "detail": detail,
        "summary": {"passed": passed, "failed": failed, "errors": errors, "total": len(ordered)},
        "results": ordered,
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0 if failed == 0 and errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
