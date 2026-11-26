#!/usr/bin/env python3
"""Executable public-contract probe for the Go target (BATCH-CONTRACT-0001).

Runs the observation cases declared in
fixtures/batches/BATCH-CONTRACT-0001/contract-cases.json against the migrated
Go repository and prints, per observation, the normalized observable value.

The repository may be mounted read-only, so the build/tests run against a
temporary writable copy when necessary. Every potentially long-running command
(go build / go test) is wrapped in the coreutils `timeout` binary and in an
explicit subprocess timeout, so the probe can never block forever.
"""

import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))

sys.path.insert(0, os.path.join(ROOT, "shared", "batches", "BATCH-CONTRACT-0001"))
import normalize  # noqa: E402

FIXTURE = os.path.join(ROOT, "fixtures", "batches", "BATCH-CONTRACT-0001", "contract-cases.json")
TEST_TIMEOUT = os.environ.get("CONTRACT_TEST_TIMEOUT", "900")
BUILD_TIMEOUT = os.environ.get("CONTRACT_BUILD_TIMEOUT", "600")
PASS_RE = re.compile(r"^--- (PASS|FAIL): ([A-Za-z0-9_]+)")
SSM_TESTS = ("TestReportSSMExitReports254Hint", "TestReportSSMExitReportsOtherCodes")


def resolve_repo():
    """Locate the migrated Go repository, tolerating either container layout."""
    candidates = []
    if os.environ.get("CONTRACT_REPO"):
        candidates.append(os.environ["CONTRACT_REPO"])
    if len(sys.argv) > 1:
        candidates.append(sys.argv[1])
    candidates += ["/workspace/repo", "/input/target"]
    for cand in candidates:
        if cand and os.path.isfile(os.path.join(cand, "go.mod")) and os.path.isfile(
            os.path.join(cand, "main.go")
        ):
            return cand
    return candidates[-1]


REPO = resolve_repo()


def load_cases():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def build(work):
    out = os.path.join(tempfile.mkdtemp(prefix="taws-contract-bin-"), "taws")
    res = normalize.run(
        ["timeout", BUILD_TIMEOUT, "go", "build", "-o", out, "."],
        cwd=work,
        timeout=int(BUILD_TIMEOUT) + 30,
    )
    return out if res["exit"] == 0 else None


def install_ssm_test(work):
    src = os.path.join(HERE, "ssm_exit_contract_test.go")
    dst = os.path.join(work, "ssm_exit_contract_test.go")
    if os.path.exists(src):
        shutil.copyfile(src, dst)
    return dst


def parse_tests(text):
    found = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        m = PASS_RE.match(line.strip())
        if m:
            found[m.group(2)] = m.group(1)
    return found


def run_package_tests(work):
    verdicts = {}
    res = normalize.run(
        ["timeout", TEST_TIMEOUT, "go", "test", "-count=1", "-v", "./internal/..."],
        cwd=work,
        timeout=int(TEST_TIMEOUT) + 30,
    )
    verdicts.update(parse_tests(res["stdout"]))
    dst = install_ssm_test(work)
    try:
        res2 = normalize.run(
            ["timeout", TEST_TIMEOUT, "go", "test", "-count=1", "-v", "."],
            cwd=work,
            timeout=int(TEST_TIMEOUT) + 30,
        )
        verdicts.update(parse_tests(res2["stdout"]))
    finally:
        if os.path.exists(dst):
            os.remove(dst)
    return verdicts


def run_cli(binary, commands, work):
    normalized = []
    for cmd in commands:
        argv = ["timeout", "60", binary] + list(cmd["argv"])
        res = normalize.run(argv, cwd=work, timeout=120)
        normalized.append({"argv": cmd["argv"], "value": normalize.normalize_cli(cmd.get("capture", "exit_only"), res)})
    return normalized


def test_value(oid, verdicts, wanted):
    ok = all(verdicts.get(name) == "PASS" for name in wanted) if wanted else False
    if oid == "CONTRACT-004":
        invalid = verdicts.get("TestLoadInvalidDoesNotPanic") == "PASS"
        missing = verdicts.get("TestLoadMissingFileLogsWarningAndUsesDefaults") == "PASS"
        return {"invalid_yaml_defaults": invalid, "missing_file_defaults": missing}
    if oid == "CONTRACT-014":
        return {"starts_with_defaults": ok}
    if oid == "CONTRACT-019":
        hint = verdicts.get("TestReportSSMExitReports254Hint") == "PASS"
        other = verdicts.get("TestReportSSMExitReportsOtherCodes") == "PASS"
        return {"exit_code_reported": hint and other, "hint_254": hint}
    return {"behaviour_ok": ok}


def main():
    cases = load_cases()
    work = normalize.writable_copy(REPO, "taws-target-")
    binary = build(work)
    if binary is None:
        normalize.emit(
            {c["observation_id"]: normalize.entry(False, {"build_failed": True}) for c in cases}
        )
        return 0

    test_verdicts = run_package_tests(work)
    results = {}
    for case in cases:
        oid = case["observation_id"]
        probe = case.get("probe")
        expected = case.get("expected")
        wanted = case.get("target", {}).get("tests", [])
        if probe == "cli":
            values = [r["value"] for r in run_cli(binary, case.get("commands", []), work)]
            value = normalize.cli_value(oid, values, expected)
            results[oid] = normalize.entry(all(value.values()), value)
        elif probe == "suite":
            res = normalize.run(
                ["timeout", TEST_TIMEOUT, "go", "test", "-count=1", "./..."],
                cwd=work,
                timeout=int(TEST_TIMEOUT) + 30,
            )
            value = {"suite_ok": res["exit"] == 0}
            results[oid] = normalize.entry(value["suite_ok"], value)
        elif probe in ("tests", "mixed", "composite"):
            value = test_value(oid, test_verdicts, wanted)
            if probe == "composite" and case.get("commands"):
                cli = normalize.cli_value(oid, [r["value"] for r in run_cli(binary, case["commands"], work)], expected)
                value = dict(value)
                value.update(cli)
            results[oid] = normalize.entry(all(value.values()), value)
        else:
            results[oid] = normalize.entry(False, {"behaviour_ok": False})
    normalize.emit(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
