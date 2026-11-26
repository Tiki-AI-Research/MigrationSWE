#!/usr/bin/env python3
"""Target-side F2P probe harness for BATCH-F2P-0001.

Migration: pev2 plan parsing / metric computation, TypeScript 5.9.3 -> Rust 1.83.0.

The harness executes every atomic F2P observation in
``fixtures/batches/BATCH-F2P-0001/f2p_probes.rs`` against the candidate
repository's native ``pev2-core`` crate and reports the raw observed status per
observation.  Every potentially long-running command is wrapped in an explicit
``timeout`` and all cargo invocations run ``--offline``; the harness can never
wait forever.

Usage:
    python3 harness.py <report.json> [repo]

``repo`` defaults to ``/workspace/repo``.  The report is a JSON object::

    {"batch_id": "BATCH-F2P-0001", "side": "target",
     "probes": [{"observation_id": "F2P-001", "status": "PASS", ...}, ...],
     "summary": {"total": 24, "pass": 24, "fail": 0, "infra_error": 0}}
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

BATCH_ID = "BATCH-F2P-0001"
PROBE_REL = os.path.join("fixtures", "batches", BATCH_ID, "f2p_probes.rs")
MANIFEST_REL = os.path.join("shared", "batches", BATCH_ID, "observations.json")
CARGO_MANIFEST_REL = os.path.join("crates", "pev2-core", "Cargo.toml")
PROBE_TEST_BIN = "f2p_probes"
BUILD_TIMEOUT_SEC = 900
TEST_TIMEOUT_SEC = 900

# Fallback mapping used only when the shared manifest cannot be located; keeps
# the harness runnable when a single batch fragment is assembled in isolation.
FALLBACK_TESTS = [
    ("F2P-001", "f2p_001_invalid_plan_literal"),
    ("F2P-002", "f2p_002_cleanup_psql_frame"),
    ("F2P-003", "f2p_003_fractional_rows_marker"),
    ("F2P-004", "f2p_004_never_executed_zeroed"),
    ("F2P-005", "f2p_005_is_verbose_cte_output"),
    ("F2P-006", "f2p_006_fractional_rows_decimal"),
    ("F2P-007", "f2p_007_cleanup_pipe_frame"),
    ("F2P-008", "f2p_008_cleanup_wrapping_quotes"),
    ("F2P-009", "f2p_009_from_text_invalid"),
    ("F2P-010", "f2p_010_cleanup_query_plan_header"),
    ("F2P-011", "f2p_011_cleanup_trailing_rowcount"),
    ("F2P-012", "f2p_012_is_verbose_descendant_output"),
    ("F2P-013", "f2p_013_node_type_entry_identity"),
    ("F2P-014", "f2p_014_modifytable_operation_json"),
    ("F2P-015", "f2p_015_max_estimate_factor"),
    ("F2P-016", "f2p_016_is_verbose_nested_cte"),
    ("F2P-017", "f2p_017_from_text_empty"),
    ("F2P-018", "f2p_018_insert_text"),
    ("F2P-019", "f2p_019_never_executed_seq_scan"),
    ("F2P-020", "f2p_020_never_executed_nested_loop"),
    ("F2P-021", "f2p_021_from_json_doubled_quotes"),
    ("F2P-022", "f2p_022_from_text_garbage"),
    ("F2P-023", "f2p_023_cleanup_plus_continuation"),
    ("F2P-024", "f2p_024_from_text_rootless"),
]


def find_asset_root(start):
    cur = os.path.abspath(start)
    for _ in range(10):
        if os.path.exists(os.path.join(cur, PROBE_REL)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def load_observations(asset_root):
    if asset_root is not None:
        manifest = os.path.join(asset_root, MANIFEST_REL)
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            pairs = [
                (item["observation_id"], item["test"])
                for item in data.get("observations", [])
            ]
            if pairs:
                return pairs
        except (OSError, ValueError, KeyError):
            pass
    return list(FALLBACK_TESTS)


def cargo_env():
    env = os.environ.copy()
    env.setdefault("CARGO_NET_OFFLINE", "true")
    prefetch = "/opt/migration/dependencies/cargo"
    cargo_home = env.get("CARGO_HOME")
    if (not cargo_home or not os.path.isdir(os.path.join(cargo_home, "registry"))) and os.path.isdir(prefetch):
        env["CARGO_HOME"] = prefetch
    return env


def run_bounded(cmd, cwd, timeout_sec):
    """Run ``cmd`` under an explicit ``timeout`` wrapper; never waits forever."""
    started = time.time()
    try:
        proc = subprocess.run(
            ["timeout", str(timeout_sec)] + list(cmd),
            cwd=cwd,
            env=cargo_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return proc.returncode, proc.stdout, time.time() - started
    except OSError as exc:
        return 127, "spawn-error: {0}".format(exc), time.time() - started


def parse_results(output):
    results = {}
    ok_re = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+ok\b", re.MULTILINE)
    fail_re = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED\b", re.MULTILINE)
    for name in ok_re.findall(output):
        results[name] = "PASS"
    for name in fail_re.findall(output):
        results[name] = "FAIL"
    return results


def write_report(path, report):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")


def build_report(pairs, statuses, detail, default_status="INFRA_ERROR"):
    probes = []
    for observation_id, test in pairs:
        status = statuses.get(test, default_status)
        probes.append(
            {
                "observation_id": observation_id,
                "status": status,
                "test": test,
                "detail": detail.get(test, ""),
            }
        )
    summary = {
        "total": len(probes),
        "pass": sum(1 for p in probes if p["status"] == "PASS"),
        "fail": sum(1 for p in probes if p["status"] == "FAIL"),
        "infra_error": sum(1 for p in probes if p["status"] == "INFRA_ERROR"),
    }
    return {
        "batch_id": BATCH_ID,
        "side": "target",
        "technology": {"name": "Rust", "version": "1.83.0"},
        "probes": probes,
        "summary": summary,
    }


def main():
    report_path = sys.argv[1] if len(sys.argv) > 1 else "/logs/result.json"
    repo = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.environ.get("REPO_PATH", "/workspace/repo")
    )
    repo = os.path.abspath(repo)

    asset_root = find_asset_root(os.path.dirname(os.path.abspath(__file__)))
    pairs = load_observations(asset_root)

    if asset_root is None:
        write_report(
            report_path,
            build_report(
                pairs,
                {},
                {test: "asset-root/probe-fixture-not-found" for _, test in pairs},
            ),
        )
        return 0

    probe_src = os.path.join(asset_root, PROBE_REL)
    cargo_manifest = os.path.join(repo, CARGO_MANIFEST_REL)

    if not os.path.exists(cargo_manifest):
        # No native target capability in the candidate: a direct semantic FAIL,
        # not an infrastructure error.
        write_report(
            report_path,
            build_report(
                pairs,
                {},
                {test: "target-capability-absent:{0}".format(CARGO_MANIFEST_REL) for _, test in pairs},
                default_status="FAIL",
            ),
        )
        return 0

    tests_dir = os.path.join(repo, "crates", "pev2-core", "tests")
    os.makedirs(tests_dir, exist_ok=True)
    dest = os.path.join(tests_dir, PROBE_TEST_BIN + ".rs")
    backup = dest + ".bak-f2p-harness"
    had_existing = os.path.exists(dest)
    try:
        if had_existing:
            shutil.copyfile(dest, backup)
        shutil.copyfile(probe_src, dest)

        build_code, build_out, _ = run_bounded(
            ["cargo", "build", "--offline"], repo, BUILD_TIMEOUT_SEC
        )
        if build_code != 0:
            write_report(
                report_path,
                build_report(
                    pairs,
                    {},
                    {test: "cargo-build-failed(exit=%d)" % build_code for _, test in pairs},
                ),
            )
            return 0

        test_code, test_out, _ = run_bounded(
            [
                "cargo",
                "test",
                "--offline",
                "--test",
                PROBE_TEST_BIN,
                "--",
                "--test-threads=1",
            ],
            repo,
            TEST_TIMEOUT_SEC,
        )
        results = parse_results(test_out)

        detail = {}
        for _, test in pairs:
            if test not in results:
                if test_code == 124:
                    detail[test] = "timeout(%ds)" % TEST_TIMEOUT_SEC
                elif test_code != 0 and "COMPILE" in test_out.upper():
                    detail[test] = "probe-compile-failed"
                else:
                    detail[test] = "not-executed"

        # A non-zero exit because individual assertions failed is a semantic
        # FAIL, not an infrastructure error; only invoke/build problems are
        # classified as INFRA_ERROR by build_report's default.
        write_report(report_path, build_report(pairs, results, detail))
        return 0
    finally:
        if had_existing and os.path.exists(backup):
            shutil.move(backup, dest)
        elif os.path.exists(dest):
            os.remove(dest)


if __name__ == "__main__":
    sys.exit(main())
