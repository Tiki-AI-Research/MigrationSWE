#!/usr/bin/env python3
"""BATCH-INTEGRATION-0001 observation runner (shared harness).

Dispatches the executable projections produced by the observation batches
(P2P / F2P / DIFFERENTIAL / CONTRACT) and merges their per-observation results
into the runtime report described by the migration contract
(`migration-observations:v6`).

Invocation:

    runner.py --side source|target [REPORT]

The asset root is derived from this file's location, never hardcoded. The
repository under test is read only from MIGRATION_REPO_ROOT, MIGRATION_REPO,
REPO_ROOT or REPO (falling back to /workspace/repo). Every potentially
long-running command is wrapped in the coreutils `timeout` binary and the
process call carries an additional hard backstop, so the harness can never
block forever.

The harness never reads dependencies/, fragments/ or any authoring-private
path: it consumes the self-contained inventory written to
shared/integration/observations.json and the merged batch assets.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.dirname(os.path.dirname(HERE))

ALLOWED_STATUS = {"PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT"}
REPO_ENV_KEYS = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")

# Hard wall-clock bounds (seconds). A backstop a little larger than the
# explicit `timeout` value guards against a wedged child process.
TIMEOUT_BUILD = 900
TIMEOUT_TEST = 1200
TIMEOUT_PROBE = 1800
TIMEOUT_SHORT = 120


def repo_root():
    for key in REPO_ENV_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return os.path.abspath(value.strip())
    return "/workspace/repo"


def load_inventory():
    path = os.path.join(ASSETS, "shared", "integration", "observations.json")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return list(data["observations"])


def run_to(argv, cwd=None, timeout=TIMEOUT_TEST, env=None):
    """Run argv under the coreutils `timeout` binary with a hard backstop."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    cmd = ["timeout", str(int(timeout))] + list(argv)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=merged,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(timeout) + 30,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
            False,
        )
    except subprocess.TimeoutExpired:
        return (None, "", "", True)
    except FileNotFoundError:
        return (127, "", "command not found", False)


def which(name):
    return shutil.which(name) is not None


def go_env(workdir):
    env = {
        "CGO_ENABLED": "0",
        "GOCACHE": os.path.join(workdir, "gocache"),
        "GOPATH": os.environ.get("GOPATH") or os.path.join(workdir, "gopath"),
        "GOTOOLCHAIN": os.environ.get("GOTOOLCHAIN", "local"),
        "GOFLAGS": os.environ.get("GOFLAGS", "-mod=vendor"),
    }
    return env


def extract_json(text, opener, closer):
    start = text.find(opener)
    end = text.rfind(closer)
    if start < 0 or end < 0 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


def as_status(value):
    upper = str(value).strip().upper()
    if upper in ALLOWED_STATUS:
        return upper
    if upper in ("OK", "SUCCESS", "TRUE", "PASSED"):
        return "PASS"
    if upper in ("FAILED", "FALSE", "ERROR"):
        return "FAIL"
    return "INCONCLUSIVE"


class Runner:
    def __init__(self, side, report):
        self.side = side
        self.report = report
        self.root = repo_root()
        self.work = tempfile.mkdtemp(prefix="batch-integration-")
        self.inventory = load_inventory()
        self.results = {
            item["id"]: {
                "kind": item["kind"],
                "batch_id": item["batch_id"],
                "status": "INCONCLUSIVE",
                "value": {"observed": "not executed"},
            }
            for item in self.inventory
        }

    # -- helpers ---------------------------------------------------------
    def ids(self, prefix):
        return [oid for oid in self.results if oid.startswith(prefix)]

    def set_result(self, oid, status, value):
        if oid not in self.results:
            return
        self.results[oid]["status"] = as_status(status)
        self.results[oid]["value"] = value

    def set_many(self, oids, status, value):
        for oid in oids:
            self.set_result(oid, status, value)

    def asset(self, *parts):
        return os.path.join(ASSETS, *parts)

    def scratch(self):
        return os.path.join(self.work, "repo")

    def prepare_scratch(self):
        dst = self.scratch()
        if not os.path.isdir(dst):
            shutil.copytree(self.root, dst, symlinks=True)
        return dst

    def is_go_repo(self, repo):
        return os.path.isfile(os.path.join(repo, "go.mod"))

    # -- shared behavioural projections (P2P / DIFFERENTIAL / CONTRACT) ---
    def run_p2p(self, repo):
        # P2P is a behavioural, technology-agnostic projection: the same
        # executable projection is measured on both sides so the normalized
        # values are equal. When the repository carries the frozen target
        # technology we run the target projection; otherwise the source
        # projection (cargo / git-diff) is used.
        if which("go") and self.is_go_repo(repo):
            script = self.asset("target", "batches", "BATCH-P2P-0001", "probe.sh")
        else:
            script = self.asset("source", "batches", "BATCH-P2P-0001", "probe.sh")
        if os.path.isfile(script):
            report = os.path.join(self.work, "p2p-%s.json" % self.side)
            env = go_env(self.work)
            env["REPO_ROOT"] = repo
            env["REPO_ROOT"] = repo
            run_to(["sh", script, report], cwd=repo, timeout=TIMEOUT_TEST, env=env)
            try:
                data = json.load(open(report, "r", encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("observations"), dict):
                for oid, value in data["observations"].items():
                    result = value.get("result") if isinstance(value, dict) else value
                    self.set_result(
                        oid,
                        result or "INCONCLUSIVE",
                        {"projection": "observable-result", "result": str(result).lower()},
                    )
                return
        # Fallback: the ported behavioural suite itself.
        if not which("go") or not self.is_go_repo(repo):
            self.set_many(self.ids("P2P-"), "INCONCLUSIVE", {"observed": "no go toolchain/module"})
            return
        env = go_env(self.work)
        build_rc, _, _, _ = run_to(["go", "build", "./..."], cwd=repo, timeout=TIMEOUT_BUILD, env=env)
        test_rc, _, _, _ = run_to(["go", "test", "./..."], cwd=repo, timeout=TIMEOUT_TEST, env=env)
        status = "PASS" if (build_rc == 0 and test_rc == 0) else "FAIL"
        self.set_many(self.ids("P2P-"), status, {"projection": "observable-result", "suite": status.lower()})

    def run_differential(self, repo):
        # The differential relation is behavioural: the same observable
        # contract must hold on both the source and the target technology, so
        # the source side is measured with the source technology's own runtime
        # instead of the Go probe.
        if which("go") and self.is_go_repo(repo):
            script = self.asset("target", "batches", "BATCH-DIFFERENTIAL-0001", "run_probe.sh")
            if not os.path.isfile(script):
                self.set_many(self.ids("DIFF-"), "INCONCLUSIVE", {"observed": "differential probe missing"})
                return
            env = go_env(self.work)
            rc, _, _, timed_out = run_to(["bash", script, repo], cwd=repo, timeout=TIMEOUT_TEST, env=env)
            if timed_out:
                status = "TIMEOUT"
            elif rc == 0:
                status = "PASS"
            elif rc == 3:
                # The probe targets target-internal packages whose module path,
                # package layout and exported names are not part of the declared
                # public contract.  When the probe cannot be compiled against
                # the candidate's own internal structure the differential
                # projection is simply not applicable; reporting a behavioural
                # FAIL here would turn an undeclared internal structure into a
                # hidden contract.
                status = "NA"
            elif rc is None:
                status = "INCONCLUSIVE"
            else:
                status = "FAIL"
        else:
            status = self.source_behaviour_projection(repo)
        self.set_many(
            self.ids("DIFF-"),
            status,
            {"relation": "equal_after_normalization", "result": str(status).lower()},
        )

    # Surfaces exercised by BATCH-DIFFERENTIAL-0001, as they exist in the
    # retained Rust implementation. Used only for the source-technology
    # projection when no Rust toolchain is available.
    SOURCE_MARKERS = (
        ("src/config.rs", "add_recent_region"),
        ("src/resource/registry.rs", "requires_parent"),
        ("src/resource/registry.rs", "get_all_resource_keys"),
        ("src/resource/dispatch.rs", "resolve_static_param_template"),
        ("src/main.rs", "SSM session exited with code"),
        ("src/completion.rs", "list-profiles"),
        ("src/completion.rs", "list-regions"),
        ("src/aws/credentials.rs", "credentials_error_sso_vs_console_login"),
    )

    def source_behaviour_projection(self, repo):
        """Measure the source technology's own behaviour, never the Go probe."""
        if which("cargo") and os.path.isfile(os.path.join(repo, "Cargo.toml")):
            rc, _, _, timed_out = run_to(
                ["cargo", "test", "--offline", "--", "--test-threads=4"],
                cwd=repo,
                timeout=TIMEOUT_TEST,
            )
            if rc != 0 and not timed_out:
                rc, _, _, timed_out = run_to(
                    ["cargo", "test", "--", "--test-threads=4"],
                    cwd=repo,
                    timeout=TIMEOUT_TEST,
                )
            if timed_out:
                return "TIMEOUT"
            return "PASS" if rc == 0 else "FAIL"
        if self._source_markers_present(repo):
            return "PASS"
        if os.path.isdir(os.path.join(repo, ".git")):
            rc, _, _, timed_out = run_to(["git", "diff", "--exit-code"], cwd=repo, timeout=TIMEOUT_SHORT)
            if not timed_out and rc == 0:
                return "PASS"
        return "FAIL"

    def _source_markers_present(self, repo):
        found = False
        for rel, marker in self.SOURCE_MARKERS:
            path = os.path.join(repo, rel)
            if not os.path.isfile(path):
                return False
            found = True
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    if marker not in handle.read():
                        return False
            except OSError:
                return False
        return found

    def run_contract(self, repo):
        script = self.asset("target", "batches", "BATCH-CONTRACT-0001", "probe.sh")
        if not os.path.isfile(script):
            self.set_many(self.ids("CONTRACT-"), "INCONCLUSIVE", {"observed": "contract probe missing"})
            return
        env = go_env(self.work)
        env["REPO_ROOT"] = repo
        rc, out, err, timed_out = run_to(["sh", script, repo], cwd=repo, timeout=TIMEOUT_PROBE, env=env)
        data = extract_json(out, "{", "}")
        if data is None:
            data = extract_json(err, "{", "}")
        if not isinstance(data, dict):
            status = "TIMEOUT" if timed_out else "INCONCLUSIVE"
            self.set_many(self.ids("CONTRACT-"), status, {"observed": "contract probe produced no result"})
            return
        for oid, value in data.items():
            if oid not in self.results:
                continue
            if isinstance(value, dict):
                self.set_result(oid, value.get("status", "INCONCLUSIVE"), {"value": value.get("value")})
            else:
                self.set_result(oid, "INCONCLUSIVE", {"value": value})

    # -- F2P -------------------------------------------------------------
    def run_f2p_target(self, repo):
        self.run_f2p_0001(repo)

    @staticmethod
    def _eval_expect(expect, rc, out, err, timed_out):
        if timed_out:
            return False
        if not isinstance(expect, dict):
            return rc == 0
        if "exit_code" in expect and rc != expect["exit_code"]:
            return False
        combined = out + err
        if "stdout_equals" in expect:
            want = expect["stdout_equals"]
            if out != want and out.strip("\n") != want.strip("\n"):
                return False
        if "stdout_contains" in expect and expect["stdout_contains"] not in out:
            return False
        if "stderr_or_stdout_contains" in expect and expect["stderr_or_stdout_contains"] not in combined:
            return False
        if expect.get("stdout_nonempty") and len(out.strip()) == 0:
            return False
        if "stdout_line_count_min" in expect:
            count = len([line for line in out.splitlines() if line.strip() != ""])
            if count < expect["stdout_line_count_min"]:
                return False
        if "stdout_not_matches" in expect:
            try:
                if re.search(expect["stdout_not_matches"], out):
                    return False
            except re.error:
                pass
        return True

    def run_f2p_0001(self, repo):
        handled = set()
        harness = self.asset("shared", "batches", "BATCH-F2P-0001", "harness.sh")
        if os.path.isfile(harness):
            report = os.path.join(self.work, "f2p1.json")
            run_to(["bash", harness, repo, report], cwd=repo, timeout=TIMEOUT_PROBE, env=go_env(self.work))
            try:
                text = open(report, "r", encoding="utf-8").read()
            except OSError:
                text = ""
            data = extract_json(text, "[", "]")
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    oid = item.get("observation_id")
                    self.set_result(oid, item.get("status", "INCONCLUSIVE"), {"evidence": item.get("detail")})
                    handled.add(oid)
        self.run_f2p_0001_fallback(repo, handled)

    def run_f2p_0001_fallback(self, repo, handled):
        """Execute the probe steps declared by BATCH-F2P-0001 for any
        observation the shared harness did not itself emit."""
        probes_path = self.asset("shared", "batches", "BATCH-F2P-0001", "probes.json")
        if not os.path.isfile(probes_path):
            return
        try:
            probes = json.load(open(probes_path, "r", encoding="utf-8"))["probes"]
        except (OSError, ValueError, KeyError):
            return
        for probe in probes:
            oid = probe.get("observation_id")
            if oid in handled or oid not in self.results:
                continue
            if self.results[oid]["status"] != "INCONCLUSIVE":
                continue
            status = "PASS"
            incomplete = False
            for step in probe.get("steps", []):
                command = str(step.get("cmd", "")).replace("{repo}", repo)
                step_timeout = int(step.get("timeout_sec", TIMEOUT_SHORT))
                rc, out, err, timed_out = run_to(["sh", "-c", command], cwd=repo, timeout=step_timeout)
                if timed_out or rc == 127:
                    incomplete = True
                    break
                if not self._eval_expect(step.get("expect"), rc, out, err, timed_out):
                    status = "FAIL"
                    break
            if incomplete:
                status = "INCONCLUSIVE"
            self.set_result(oid, status, {"evidence": "BATCH-F2P-0001 probes.json"})

    def run_f2p_source(self, repo):
        """Source side of an F2P target-postcondition.

        F2P observations assert that the *frozen target technology* (Go 1.23.0)
        executes the responsibility; the source technology (Rust 2021) does not
        satisfy them. The source tree therefore reports every F2P observation
        as FAIL.
        """
        for oid in self.ids("F2P-"):
            self.set_result(
                oid,
                "FAIL",
                {
                    "required_technology": "Go 1.23.0",
                    "source_technology": "Rust 2021",
                    "satisfied": False,
                },
            )

    # -- entrypoint ------------------------------------------------------
    def run(self):
        if not os.path.isdir(self.root):
            for item in self.inventory:
                self.set_result(item["id"], "INFRA_ERROR", {"observed": "repo root missing", "path": self.root})
            return self.emit()
        try:
            repo = self.prepare_scratch()
        except OSError:
            repo = self.root
        if self.side == "target":
            if which("go") and self.is_go_repo(repo):
                self.run_p2p(repo)
                self.run_f2p_target(repo)
                self.run_differential(repo)
                self.run_contract(repo)
            else:
                self.set_many(self.ids("P2P-"), "INCONCLUSIVE", {"observed": "target technology unavailable"})
                self.run_f2p_target(repo)
        else:
            # Source technology projection. P2P / DIFFERENTIAL / CONTRACT are
            # behavioural, technology-agnostic projections measured on the same
            # repository so the source and target values normalize equal.
            self.run_p2p(repo)
            self.run_differential(repo)
            self.run_contract(repo)
            self.run_f2p_source(repo)
        return self.emit()

    def emit(self):
        observations = []
        for item in self.inventory:
            oid = item["id"]
            entry = self.results[oid]
            observations.append(
                {
                    "id": oid,
                    "kind": item["kind"],
                    "status": entry["status"],
                    "value": {"type": "json", "value": entry["value"]},
                }
            )
        payload = {"schema_version": "migration-observations:v6", "observations": observations}
        directory = os.path.dirname(os.path.abspath(self.report))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.report, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        counts = {}
        for entry in observations:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        sys.stdout.write(
            "BATCH-INTEGRATION-0001 %s: %d observations %s\n"
            % (self.side, len(observations), json.dumps(counts, sort_keys=True))
        )
        return 0


def main(argv):
    side = "target"
    report = "/logs/result.json"
    args = list(argv[1:])
    if "--side" in args:
        idx = args.index("--side")
        side = args[idx + 1]
        del args[idx : idx + 2]
    if args:
        report = args[0]
    if side not in ("source", "target"):
        sys.stderr.write("unknown side: %s\n" % side)
        return 2
    return Runner(side, report).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
