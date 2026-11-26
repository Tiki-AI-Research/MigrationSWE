#!/usr/bin/env python3
"""DIFF-033 / R033 target probe.

Builds a throwaway Cargo binary that path-depends on the reference target's
`pev2-core` crate, asks it to parse every node-type string in the shared
corpus, and asserts the produced node maps contain the expected normalized
fields. Every potentially long-running command is wrapped in an explicit
`timeout`; the probe never waits indefinitely.
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

CARGO_TIMEOUT_SEC = 900


def repo_root_from(script: Path) -> Path:
    for parent in [script, *script.parents]:
        candidate = parent / "shared" / "batches"
        if candidate.is_dir():
            canonical = Path(os.environ.get("PEV2_ASSET_ROOT", "")).resolve(strict=False)
            return canonical if os.environ.get("PEV2_ASSET_ROOT") else parent
    return Path(os.environ.get("PEV2_ASSET_ROOT", Path.cwd())).resolve(strict=False)


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    wrapped = ["timeout", str(CARGO_TIMEOUT_SEC), *cmd]
    proc = subprocess.run(
        wrapped,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-4000:])
    return proc.returncode, proc.stdout


def subset(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(subset(actual.get(key), value) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(subset(a, e) for a, e in zip(actual, expected))
    return actual == expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-repo", default=os.environ.get("PEV2_TARGET_REPO", "/input/target"))
    parser.add_argument("--corpus", default=None)
    parser.add_argument("--probe-src", default=None)
    args = parser.parse_args()

    script = Path(__file__).resolve()
    root = repo_root_from(script)
    corpus_path = Path(args.corpus) if args.corpus else (
        root / "shared" / "batches" / "BATCH-DIFFERENTIAL-0002" / "node-type-cases.json"
    )
    probe_src = Path(args.probe_src) if args.probe_src else (
        script.parent / "node_type_probe.rs"
    )
    target_repo = Path(args.target_repo)
    crate = target_repo / "crates" / "pev2-core"
    if not (crate / "src" / "lib.rs").is_file():
        print(f"target crate not found at {crate}", file=sys.stderr)
        return 2

    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="pev2-diff033-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "src").mkdir()
        shutil.copyfile(probe_src, tmp_path / "src" / "main.rs")
        (tmp_path / "Cargo.toml").write_text(
            "[package]\n"
            'name = "diff033-probe"\n'
            'version = "0.0.0"\n'
            'edition = "2021"\n\n'
            "[dependencies]\n"
            f'pev2-core = {{ path = "{crate}" }}\n'
            'serde_json = "1"\n',
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.setdefault("CARGO_NET_OFFLINE", "true")
        env["CARGO_TARGET_DIR"] = str(tmp_path / "target")

        code, out = run(
            ["cargo", "run", "--quiet", "--offline", "--", str(corpus_path)],
            tmp_path,
            env,
        )
        if code != 0:
            print("DIFF-033 target probe: cargo run failed", file=sys.stderr)
            return code

        results = json.loads(out.strip().splitlines()[-1])
        actual_by_input = {item["input"]: item["actual"] for item in results}

    failures: list[str] = []
    for case in corpus["cases"]:
        actual = actual_by_input.get(case["input"])
        if actual is None:
            failures.append(f"missing result for {case['input']!r}")
        elif not subset(actual, case["expect"]):
            failures.append(f"{case['input']!r}: {actual} does not match {case['expect']}")

    if failures:
        print("DIFF-033 target probe FAILED:")
        for failure in failures:
            print("  -", failure)
        return 1
    print(f"DIFF-033 target probe PASSED ({len(corpus['cases'])} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
