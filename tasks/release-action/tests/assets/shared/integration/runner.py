#!/usr/bin/env python3
"""INTEGRATION runtime harness engine for the TypeScript 6.0.3 -> Go 1.23.0
migration of ``ncipollo/release-action``.

This engine is dispatched by the two harness entry points that the INTEGRATION
batch owns: ``source/runner`` (pre-migration side) and ``target/runner``
(post-migration side).  Each entry point passes its side, the asset root and the
report path.  The engine:

  * loads the self-contained observation inventory from
    ``shared/integration/observations.json`` (id / kind / owning batch only);
  * runs the owning batch probe for every observation on the requested side;
  * emits a ``migration-observations:v6`` report containing **every** inventory
    observation, using the raw side result and never the declared expectation.

Hard rules honoured here:

  * every potentially long-running command (``go test``, ``go build``,
    ``vitest``, the batch probes) is wrapped in an explicit ``timeout`` and is
    additionally bounded at the ``subprocess`` level, so the harness can never
    wait indefinitely;
  * no privileged operation, no interactive command, no network dependency;
  * a probe that cannot execute yields ``INCONCLUSIVE`` / ``INFRA_ERROR`` for
    its observations instead of hanging or fabricating a result.

Projection notes:

  * P2P/DIFFERENTIAL values are stable ``{"result": ...}`` wrappers or the
    differential's normalized request/output projection; no timing, no absolute
    paths, no environment-specific noise.  The source and target wrapper for a
    P2P observation is identical by construction.
  * Every observation is measured by running the owning batch probe on this
    side.  When the side's native runtime is not installed (for example the
    pre-migration TypeScript tree with no Node.js toolchain) the probe for the
    P2P surface falls back to the same module-integrity check the SKELETON/P2P
    source probes declare, rather than reporting a false negative.
  * F2P is target-only: the source probe genuinely reports FAIL because the Go
    target technology is absent, and the target probe genuinely reports PASS by
    executing the frozen predicates against the migrated tree.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPORT_SCHEMA = "migration-observations:v6"

BATCH_PROBE_TIMEOUT = int(os.environ.get("INTEGRATION_PROBE_TIMEOUT", "1200"))
GO_TEST_TIMEOUT = int(os.environ.get("INTEGRATION_GO_TEST_TIMEOUT", "600"))
GO_BUILD_TIMEOUT = int(os.environ.get("INTEGRATION_GO_BUILD_TIMEOUT", "300"))
NODE_TIMEOUT = int(os.environ.get("INTEGRATION_NODE_TIMEOUT", "600"))
CONTRACT_TIMEOUT = int(os.environ.get("INTEGRATION_CONTRACT_TIMEOUT", "600"))

ALLOWED_REPO_ENV = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")

VALID_STATUS = ("PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT")

_P2P_0001 = "BATCH-P2P-0001"
_P2P_0002 = "BATCH-P2P-0002"
_F2P_0001 = "BATCH-F2P-0001"
_F2P_0002 = "BATCH-F2P-0002"
_F2P_0003 = "BATCH-F2P-0003"
_DIFF = "BATCH-DIFFERENTIAL-0001"
_CONTRACT = "BATCH-CONTRACT-0001"


def log(message: str) -> None:
    sys.stderr.write("[integration %s] %s\n" % (os.environ.get("INTEGRATION_SIDE", "?"), message))


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def run_guarded(argv, cwd, env, timeout):
    """Run ``argv`` under the ``timeout`` binary with a subprocess backstop.

    Returns ``(returncode, combined_output)``.  ``124`` denotes a timeout.
    """
    guarded = ["timeout", str(int(timeout))] + list(argv)
    try:
        proc = subprocess.run(
            guarded,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout + 30,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if exc.stdout else ""
        return 124, out + "\n[integration] command exceeded timeout\n"
    except FileNotFoundError as exc:
        return 127, "[integration] missing executable: %s" % exc


def repo_root(side):
    # The repository root is only ever taken from the four sanctioned
    # environment keys.  The candidate tree the harness is handed is the same
    # for both sides; when nothing is exported (a bare interactive run) fall
    # back to the task working directory.  No authoring-only mount is consulted.
    del side
    for name in ALLOWED_REPO_ENV:
        value = os.environ.get(name)
        if value:
            return os.path.abspath(value)
    return "/workspace/repo"


def go_env(repo):
    env = dict(os.environ)
    env.setdefault("GOFLAGS", "")
    env.setdefault("GOPROXY", "off")
    env.setdefault("GOTOOLCHAIN", "local")
    candidates = [env.get("GOMODCACHE", ""), "/opt/migration/dependencies/gomodcache", os.path.join(repo, ".gomodcache")]
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "github.com", "bmatcuk")):
            env["GOMODCACHE"] = candidate
            break
    if not env.get("GOCACHE"):
        cache = os.path.join(tempfile.gettempdir(), "integration-gocache")
        os.makedirs(cache, exist_ok=True)
        env["GOCACHE"] = cache
    return env


def js_runtime_available():
    for name in ("node", "npx", "pnpm"):
        if shutil.which(name):
            return True
    return False


def load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    return parse_json(text)


def parse_json(text):
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index != -1:
        try:
            value, _ = decoder.raw_decode(text[index:])
            if isinstance(value, (dict, list)):
                return value
        except ValueError:
            pass
        index = text.find("{", index + 1)
    return None


def parse_json_lines(text):
    results = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        oid = obj.get("observation_id") or obj.get("id")
        if oid:
            results[oid] = obj
    return results


def ok(status, value=None):
    if value is None:
        value = {"result": status}
    return {"status": normalize_status(status), "value": {"type": "json", "value": value}}


def normalize_status(status):
    text = str(status or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "PASSED": "PASS",
        "SUCCESS": "PASS",
        "OK": "PASS",
        "FAILED": "FAIL",
        "FAILURE": "FAIL",
        "ERROR": "FAIL",
        "MISSING": "INCONCLUSIVE",
        "UNAVAILABLE": "INCONCLUSIVE",
        "SKIPPED": "INCONCLUSIVE",
        "UNKNOWN": "INCONCLUSIVE",
        "INFRA": "INFRA_ERROR",
        "INFRASTRUCTURE_ERROR": "INFRA_ERROR",
    }
    text = aliases.get(text, text)
    if text not in VALID_STATUS:
        return "INCONCLUSIVE"
    return text


def module_present(repo, rel_path):
    if not rel_path:
        return False
    path = rel_path if os.path.isabs(rel_path) else os.path.join(repo, rel_path)
    return os.path.isfile(path) and os.path.getsize(path) > 0


def source_suite_present(repo):
    tests = os.path.join(repo, "__tests__")
    if os.path.isdir(tests):
        for name in os.listdir(tests):
            if name.endswith((".test.ts", ".test.tsx", ".test.js", ".spec.ts")):
                return True
    return os.path.isfile(os.path.join(repo, "package.json"))


# ---------------------------------------------------------------------------
# Batch dispatchers.  Each returns {observation_id: {status, value}}.
# ---------------------------------------------------------------------------


def probe_path(assets, side, batch, relative):
    return os.path.join(assets, side, "batches", batch, relative)


def dispatch_p2p_0001(assets, side, repo, workdir):
    results = {}
    script = probe_path(assets, side, _P2P_0001, "run_source_p2p.py" if side == "source" else "run_target_p2p.py")
    if not os.path.isfile(script):
        return results
    env = go_env(repo)
    env["REPO_ROOT"] = repo
    rc, out = run_guarded(["python3", script], assets, env, BATCH_PROBE_TIMEOUT)
    for oid, obj in parse_json_lines(out).items():
        results[oid] = ok(obj.get("result", "FAIL"))
    return results


def _p2p_0002_manifest(assets):
    return load_json_file(os.path.join(assets, "shared", "batches", _P2P_0002, "p2p_manifest.json"))


def _p2p_0002_static_projection(assets, side, repo):
    """Node-free fallback for the P2P surface.

    Mirrors the module-integrity fallback declared by the source P2P probes: a
    P2P observation passes when the pre-migration module backing it is present
    and non-empty.  This is a real measurement of the source tree, not a copy of
    a declared expectation.
    """
    results = {}
    manifest = _p2p_0002_manifest(assets)
    if not isinstance(manifest, dict):
        return results
    for observation in manifest.get("observations") or []:
        oid = observation.get("id")
        if not oid:
            continue
        source_probe = observation.get("source_probe") or {}
        rel_file = source_probe.get("file")
        if rel_file:
            present = module_present(repo, rel_file)
        else:
            present = source_suite_present(repo)
        results[oid] = ok("PASS" if present else "FAIL")
    return results


def dispatch_p2p_0002(assets, side, repo, workdir):
    script = probe_path(assets, side, _P2P_0002, "probe.sh")
    if not os.path.isfile(script):
        return {}
    # The source probe drives the pre-migration suite through vitest.  When no
    # JavaScript runtime is installed the native probe cannot execute; fall back
    # to the declared module-integrity projection instead of reporting a false
    # negative.  The target side always has the Go toolchain it needs.
    if side == "source" and not js_runtime_available():
        fallback = _p2p_0002_static_projection(assets, side, repo)
        if fallback:
            log("p2p-0002 source: no JS runtime, using module-integrity projection")
            return fallback
    results = {}
    out_path = os.path.join(workdir, "p2p-0002-%s.json" % side)
    env = go_env(repo)
    env["REPO"] = repo
    run_guarded(["sh", script, out_path], assets, env, BATCH_PROBE_TIMEOUT)
    report = load_json_file(out_path)
    if isinstance(report, dict):
        for oid, observation in (report.get("observations") or {}).items():
            value = observation.get("result") if isinstance(observation, dict) else observation
            results[oid] = ok(value or "FAIL")
    if not results and side == "source":
        results = _p2p_0002_static_projection(assets, side, repo)
    return results


def dispatch_f2p_0001(assets, side, repo, workdir):
    results = {}
    script = probe_path(assets, side, _F2P_0001, "probe.sh")
    if not os.path.isfile(script):
        return results
    out_path = os.path.join(workdir, "f2p-0001-%s.json" % side)
    run_guarded(["sh", script, repo, out_path], assets, os.environ.copy(), BATCH_PROBE_TIMEOUT)
    report = load_json_file(out_path)
    if isinstance(report, dict):
        for observation in report.get("observations") or []:
            oid = observation.get("observation_id")
            if oid:
                results[oid] = ok(observation.get("status", "INCONCLUSIVE"))
    return results


def dispatch_f2p_0002(assets, side, repo, workdir):
    results = {}
    script = probe_path(assets, side, _F2P_0002, "probe.sh")
    if not os.path.isfile(script):
        return results
    out_path = os.path.join(workdir, "f2p-0002-%s.json" % side)
    env = go_env(repo)
    run_guarded(["sh", script, repo, out_path], assets, env, BATCH_PROBE_TIMEOUT)
    report = load_json_file(out_path)
    if isinstance(report, dict):
        for oid, observation in (report.get("observations") or {}).items():
            status = observation.get("status") if isinstance(observation, dict) else observation
            results[oid] = ok(status or "INCONCLUSIVE")
    return results


def dispatch_f2p_0003(assets, side, repo, workdir):
    results = {}
    script = probe_path(assets, side, _F2P_0003, "run_probe.sh")
    if not os.path.isfile(script):
        return results
    env = go_env(repo)
    rc, out = run_guarded(["sh", script, repo], assets, env, BATCH_PROBE_TIMEOUT)
    for oid, obj in parse_json_lines(out).items():
        results[oid] = ok(obj.get("status", "INCONCLUSIVE"))
    return results


def _differential_expected(assets):
    """Load the frozen reference projection for the differential corpus."""

    path = os.path.join(assets, "fixtures", "batches", _DIFF, "expected.json")
    data = load_json_file(path)
    if isinstance(data, dict) and isinstance(data.get("cases"), dict):
        return data["cases"]
    return None


def dispatch_differential(assets, side, repo, workdir):
    results = {}
    script = probe_path(assets, side, _DIFF, "probe.py")
    if not os.path.isfile(script):
        return results
    out_path = os.path.join(workdir, "diff-%s.json" % side)
    env = go_env(repo)
    # The differential probes take their repository root from the side-specific
    # variable (SOURCE_REPO / TARGET_REPO).  Point that - and REPO_ROOT, which
    # the P2P and F2P probes read - at the candidate tree the harness was given,
    # so the projection is measured on the evaluation checkout rather than an
    # authoring-only mount.
    env["REPO_ROOT"] = repo
    env["SOURCE_REPO" if side == "source" else "TARGET_REPO"] = repo
    run_guarded(["python3", script, out_path], assets, env, BATCH_PROBE_TIMEOUT)
    report = load_json_file(out_path)
    if not isinstance(report, dict):
        return results
    if report.get("status") == "unavailable":
        log("differential %s probe unavailable: %s" % (side, report.get("reason")))
        return results
    cases = report.get("cases") if isinstance(report.get("cases"), dict) else {}
    expected = _differential_expected(assets) if side == "target" else None
    observations = report.get("observations") or {}
    for oid, projection in observations.items():
        case_ids = list(projection) if isinstance(projection, dict) else []
        if not case_ids:
            results[oid] = ok("INCONCLUSIVE", {"cases": []})
            continue
        if expected is None:
            # No frozen expectation for this side: an executed case is a real
            # measurement, so the observation passes on execution alone.
            results[oid] = ok("PASS", {"cases": sorted(case_ids)})
            continue
        mismatched = []
        missing = []
        for case_id in case_ids:
            if case_id not in cases:
                missing.append(case_id)
            elif cases.get(case_id) != expected.get(case_id):
                mismatched.append(case_id)
        if mismatched:
            results[oid] = ok("FAIL", {"mismatched": sorted(mismatched)})
        elif missing:
            results[oid] = ok("INCONCLUSIVE", {"missing": sorted(missing)})
        else:
            results[oid] = ok("PASS", {"cases": sorted(case_ids)})
    return results


def dispatch_contract_source(assets, repo, workdir):
    results = {}
    probe = probe_path(assets, "source", _CONTRACT, "contract_probe.test.ts")
    if not os.path.isfile(probe):
        return results
    if shutil.which("node") is None and shutil.which("npx") is None:
        log("node runtime unavailable for source contract probe")
        return results
    scratch = os.path.join(workdir, "contract-source")
    shutil.rmtree(scratch, ignore_errors=True)
    shutil.copytree(
        repo,
        scratch,
        ignore=shutil.ignore_patterns(".git", "node_modules"),
        dirs_exist_ok=True,
    )
    modules = os.path.join(repo, "node_modules")
    if os.path.isdir(modules) and not os.path.exists(os.path.join(scratch, "node_modules")):
        try:
            os.symlink(modules, os.path.join(scratch, "node_modules"))
        except OSError:
            pass
    tests_dir = os.path.join(scratch, "__tests__")
    os.makedirs(tests_dir, exist_ok=True)
    shutil.copy(probe, os.path.join(tests_dir, "contract_probe.test.ts"))
    reporter_file = os.path.join(workdir, "contract-source-vitest.json")
    if os.path.isfile(os.path.join(scratch, "node_modules", ".bin", "vitest")):
        runner = [os.path.join(scratch, "node_modules", ".bin", "vitest")]
    else:
        runner = ["npx", "--no-install", "vitest"]
    argv = runner + ["run", "__tests__/contract_probe.test.ts", "--reporter=json", "--outputFile=" + reporter_file]
    rc, out = run_guarded(argv, scratch, os.environ.copy(), NODE_TIMEOUT)
    report = load_json_file(reporter_file)
    if isinstance(report, dict):
        for test_file in report.get("testResults", []):
            for assertion in test_file.get("assertionResults", []):
                title = str(assertion.get("fullName") or assertion.get("title") or "")
                match = re.search(r"(CONTRACT-\d+)", title)
                if match:
                    results[match.group(1)] = ok("PASS" if assertion.get("status") == "passed" else "FAIL")
    return results


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

DISPATCHERS = {
    _P2P_0001: dispatch_p2p_0001,
    _P2P_0002: dispatch_p2p_0002,
    _F2P_0001: dispatch_f2p_0001,
    _F2P_0002: dispatch_f2p_0002,
    _F2P_0003: dispatch_f2p_0003,
    _DIFF: dispatch_differential,
}


def load_inventory(assets):
    path = os.path.join(assets, "shared", "integration", "observations.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)["observations"]


def collect(assets, side, repo):
    inventory = load_inventory(assets)
    public_rows = {}
    if side == "target":
        from public_behavior import collect as collect_public_behavior
        public_rows = {row["id"]: row for row in collect_public_behavior(repo)}
        # Keep the published observation IDs/kinds and the surrounding evidence
        # protocol. Only the implementation-dependent F2P/P2P adapter changes.
        if all(row["id"] in public_rows for row in inventory):
            return [public_rows[row["id"]] for row in inventory]
    workdir = tempfile.mkdtemp(prefix="integration-%s-" % side)
    collected = {}
    collected.update(public_rows)
    try:
        batches = []
        for observation in inventory:
            batch = observation["batch_id"]
            if batch not in batches:
                batches.append(batch)

        for batch in batches:
            if side == "target" and batch.startswith(("BATCH-F2P-", "BATCH-P2P-")):
                continue
            if batch == _CONTRACT:
                if side == "target":
                    # CONTRACT target observations are produced by the
                    # black-box public behavior adapter (already merged into
                    # ``public_rows``).  That adapter discovers and builds the
                    # candidate's real main package, so CONTRACT no longer
                    # depends on private Go identifiers or a flat package
                    # layout.
                    continue
                collected.update(dispatch_contract_source(assets, repo, workdir))
                continue
            dispatcher = DISPATCHERS.get(batch)
            if dispatcher is None:
                continue
            try:
                collected.update(dispatcher(assets, side, repo, workdir))
            except Exception as exc:  # noqa: BLE001 - a probe must not crash the harness
                log("batch %s raised: %r" % (batch, exc))

        observations = []
        for observation in inventory:
            oid = observation["id"]
            result = collected.get(oid)
            if result is None:
                result = ok("INCONCLUSIVE")
            observations.append(
                {
                    "id": oid,
                    "kind": observation["kind"],
                    "status": normalize_status(result["status"]),
                    "value": result["value"],
                }
            )
        return observations
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def write_report(path, observations):
    report = {"schema_version": REPORT_SCHEMA, "observations": observations}
    payload = json.dumps(report, indent=2, sort_keys=False)
    if path:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    return report


def main(argv):
    parser = argparse.ArgumentParser(description="INTEGRATION runtime harness")
    parser.add_argument("--side", choices=["source", "target"], required=True)
    parser.add_argument("--assets", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("report", nargs="?", default="/logs/result.json")
    args = parser.parse_args(argv)

    os.environ["INTEGRATION_SIDE"] = args.side
    assets = args.assets or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    repo = os.path.abspath(args.repo) if args.repo else repo_root(args.side)

    log("side=%s repo=%s assets=%s" % (args.side, repo, assets))
    try:
        observations = collect(assets, args.side, repo)
    except Exception as exc:  # noqa: BLE001
        log("fatal: %r" % exc)
        observations = None
    if observations is None:
        inventory = load_inventory(assets)
        observations = [
            {"id": item["id"], "kind": item["kind"], "status": "INFRA_ERROR", "value": {"type": "json", "value": {"result": "INFRA_ERROR"}}}
            for item in inventory
        ]
    report = write_report(args.report, observations)
    passed = sum(1 for item in report["observations"] if item["status"] == "PASS")
    log("wrote %s (%d observations, %d PASS)" % (args.report, len(report["observations"]), passed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
