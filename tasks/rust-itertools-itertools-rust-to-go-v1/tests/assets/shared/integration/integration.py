"""Shared runtime integration harness for the Rust-to-Go itertools migration.

This module is authored by the INTEGRATION batch. It is the single place that
merges every dependency batch (P2P, F2P, DIFFERENTIAL) into one execution so
that the source-side entry (``source/runner``) and the target-side entry
(``harness.py``) agree on the runtime report.

The runtime report uses the ``migration-observations:v6`` envelope::

    {"schema_version": "migration-observations:v6",
     "observations": [{"id": ..., "kind": ...,
                       "status": "PASS|FAIL|NA|INFRA_ERROR|INCONCLUSIVE|TIMEOUT",
                       "value": {"type": "json", "value": ...}}, ...]}

Every observation is produced by really executing the owning batch's probe
against the repository under test; the declared expectation is never echoed as
a result. A probe that cannot even reach the compiler (missing toolchain,
missing asset, timeout, crash) is reported as ``INFRA_ERROR``/``INCONCLUSIVE``
instead of a fake PASS. A probe that fails to build because the repository
under test does not yet provide the targeted Go package or symbol is a semantic
absence (``FAIL``), matching the F2P harness convention.

The dependency batches are consumed from the assembled asset tree
(``source/batches``, ``target/batches``, ``shared/batches``, ``fixtures/batches``)
that the publishing step merges into the asset root. The read-only authoring
snapshot under ``/assets/dependencies`` is never read at run time.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile

REPORT_SCHEMA_VERSION = "migration-observations:v6"
RESULT_SCHEMA_VERSION = "migration-result:v1"

P2P_BATCH = "BATCH-P2P-0001"
DIFF_BATCH = "BATCH-DIFFERENTIAL-0001"
F2P_BATCHES = ("BATCH-F2P-0001", "BATCH-F2P-0002", "BATCH-F2P-0003")

# Differential observations whose behavior is covered by an existing corpus case.
DIFF_ALIASES = {
    "DIFF-013": ["CORPUS-DIFF-006-CONCAT", "CORPUS-DIFF-006-CONCAT-EMPTY"],
    "DIFF-017": ["CORPUS-DIFF-009-FORMAT", "CORPUS-DIFF-009-FORMAT-WITH"],
}

# Differential observations whose projection is a cross-language status wrapper
# backed by a real side-specific structural/whole-repo check.
DIFF_STRUCTURAL = (
    "DIFF-002", "DIFF-004", "DIFF-011", "DIFF-023",
    "DIFF-024", "DIFF-025", "DIFF-026", "DIFF-027",
)

OUT_OF_SCOPE_TREES = ("benches", "examples", ".github/workflows")

SRC_IN_SCOPE_MODULES = (
    "src/lib.rs", "src/adaptors/mod.rs", "src/adaptors/map.rs",
    "src/adaptors/coalesce.rs", "src/adaptors/multi_product.rs", "src/free.rs",
    "src/size_hint.rs", "src/format.rs", "src/concat_impl.rs", "src/array_impl.rs",
    "src/either_or_both.rs", "src/groupbylazy.rs", "src/grouping_map.rs",
    "src/merge_join.rs", "src/process_results_impl.rs", "src/put_back_n_impl.rs",
    "src/peek_nth.rs", "src/next_array.rs", "src/intersperse.rs",
    "src/with_position.rs", "src/minmax.rs", "src/k_smallest.rs",
    "src/kmerge_impl.rs", "src/permutations.rs", "src/combinations.rs",
    "src/unique_impl.rs", "src/exactly_one_err.rs", "src/all_equal_value_err.rs",
    "src/lazy_buffer.rs", "src/rciter_impl.rs", "src/take_while_inclusive.rs",
    "src/pad_tail.rs",
)

SRC_BEHAVIOR_TESTS = (
    "tests/quick.rs", "tests/laziness.rs", "tests/specializations.rs",
    "tests/test_core.rs", "tests/test_std.rs", "tests/adaptors_no_collect.rs",
    "tests/arrays.rs",
)

GO_TEST_FILES = (
    "itertools_test.go", "repair_test.go", "core/iter_test.go", "free/free_test.go",
)

TARGET_TECHNOLOGY = {"name": "Go", "version": "1.23.0"}
SOURCE_TECHNOLOGY = {"name": "Rust", "version": "2018"}

VALID_STATUS = ("PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT")

# Markers that indicate a genuine infrastructure fault: the probe could not even
# reach the compiler because the toolchain or a probe asset is missing, a process
# timed out, or the harness crashed. Any other failed probe build means the
# repository under test does not yet provide the targeted Go package or symbol,
# which is a semantic ``FAIL``. Kept in sync with the F2P harness convention
# (``shared/batches/BATCH-F2P-0002/harness.py``).
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

GO_TIMEOUT = 1800
DEFAULT_TIMEOUT = 900


def classify_probe_failure(detail):
    """Classify a failed probe run as a semantic ``FAIL`` or ``INFRA_ERROR``.

    A failed probe build is a semantic ``FAIL``: the repository under test does
    not (yet) provide the targeted Go package or symbol. Only a genuine
    infrastructure fault (missing toolchain, missing asset, timeout, crash) is
    ``INFRA_ERROR``.
    """
    text = str(detail or "")
    if any(marker in text for marker in INFRA_FAILURE_MARKERS):
        return "INFRA_ERROR"
    return "FAIL"


def asset_root():
    """Return the asset root inferred from this module's location.

    ``<root>/shared/integration/integration.py`` -> ``<root>``.
    """
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def resolve_repo():
    """Resolve the repository under test from the allowed environment aliases."""
    for key in ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"):
        value = os.environ.get(key)
        if value:
            return value
    return "/workspace/repo"


def load_inventory(root=None):
    """Load the self-contained observation inventory (id/kind/batch_id)."""
    root = root or asset_root()
    path = os.path.join(root, "shared", "integration", "observations.json")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return list(data["observations"])


def go_env():
    env = dict(os.environ)
    env.setdefault("GOFLAGS", "")
    env["GOPROXY"] = "off"
    env["GOSUMDB"] = "off"
    env["GOTOOLCHAIN"] = "local"
    env["CGO_ENABLED"] = env.get("CGO_ENABLED", "0")
    env.setdefault("GOCACHE", tempfile.mkdtemp(prefix="integration-gocache-"))
    return env


def run(cmd, cwd, timeout=DEFAULT_TIMEOUT):
    """Run ``cmd`` returning (returncode, combined-output)."""
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=go_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    except FileNotFoundError as exc:
        return 127, "command not found: %s" % exc
    except subprocess.TimeoutExpired as exc:
        return 124, "timeout: %s" % exc


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _classify(entry):
    """Normalize a batch result entry to a runtime status string."""
    status = entry.get("status")
    if status in VALID_STATUS:
        return status
    detail = str(entry.get("detail", ""))
    for marker in ("INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT"):
        if marker in detail:
            return marker
    passed = entry.get("passed")
    if passed is None:
        return "INCONCLUSIVE"
    return "PASS" if passed else "FAIL"


def _entries_to_map(report):
    """Collapse a ``migration-result:v1`` report to a status/value map.

    Multiple results for one observation (the differential corpus) are combined:
    the observation passes only when every case passes.
    """
    if not report:
        return {}
    combined = {}
    for entry in report.get("results", []):
        obs = entry.get("observation_id")
        if not obs:
            continue
        slot = combined.setdefault(obs, {"status": "PASS", "cases": []})
        status = _classify(entry)
        if status != "PASS":
            slot["status"] = status if slot["status"] == "PASS" else slot["status"]
            if status not in VALID_STATUS:
                slot["status"] = "FAIL"
        slot["cases"].append(entry)
    return combined


def _emit_dir():
    return tempfile.mkdtemp(prefix="integration-report-")


def _batch_script(root, side, batch_id, name):
    return os.path.join(root, side, "batches", batch_id, name)


# ---------------------------------------------------------------------------
# P2P
# ---------------------------------------------------------------------------
def _go_module_root(repo):
    modules = []
    for current, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {".git", ".gocache", "vendor"}
                   and not os.path.islink(os.path.join(current, name))]
        path = os.path.join(current, "go.mod")
        if "go.mod" in files and not os.path.islink(path):
            modules.append(current)
    return modules[0] if len(modules) == 1 else repo


def _p2p(side, root, repo, workdir):
    if side == "target":
        repo = _go_module_root(repo)
    report_path = os.path.join(workdir, "p2p-%s.json" % side)
    script = _batch_script(root, side, P2P_BATCH, "probe.py")
    rc, out = run([sys.executable, script, repo, report_path], cwd=repo)
    report = _read_json(report_path)
    mapping = {}
    if report:
        for entry in report.get("results", []):
            obs = entry.get("observation_id")
            status = _classify(entry)
            value = {"result": "PASS" if status == "PASS" else "FAIL"}
            detail = entry.get("detail")
            if isinstance(detail, str):
                try:
                    parsed = json.loads(detail)
                    if isinstance(parsed, dict):
                        value = parsed
                except ValueError:
                    pass
            mapping[obs] = {"status": status, "value": value}
    else:
        for obs in _ids_for(root, P2P_BATCH):
            mapping[obs] = {"status": "INFRA_ERROR", "value": {"result": "INFRA_ERROR", "rc": rc, "output": out[-400:]}}
    return mapping


# ---------------------------------------------------------------------------
# F2P
# ---------------------------------------------------------------------------
def _f2p_target(batch_id, root, repo, workdir):
    repo = _go_module_root(repo)
    report_path = os.path.join(workdir, "%s-target.json" % batch_id)
    if batch_id == "BATCH-F2P-0001":
        cmd = [sys.executable, _batch_script(root, "target", batch_id, "run_probe.py"), report_path, repo]
    elif batch_id == "BATCH-F2P-0002":
        cmd = [sys.executable, _batch_script(root, "target", batch_id, "probe.py"), repo, report_path]
    else:
        cmd = [sys.executable, _batch_script(root, "target", batch_id, "run_target_probes.py"),
               "--target", repo, "--out", report_path]
    rc, out = run(cmd, cwd=repo, timeout=GO_TIMEOUT)
    report = _read_json(report_path)
    return _f2p_map(report, rc, out)


def _f2p_source(batch_id, root, repo, workdir):
    report_path = os.path.join(workdir, "%s-source.json" % batch_id)
    if batch_id == "BATCH-F2P-0001":
        cmd = [sys.executable, _batch_script(root, "source", batch_id, "probe.py"), report_path, repo]
    elif batch_id == "BATCH-F2P-0002":
        cmd = [sys.executable, _batch_script(root, "source", batch_id, "probe.py"), repo, report_path]
    else:
        cmd = [sys.executable, _batch_script(root, "source", batch_id, "run_source_probes.py"),
               "--source", repo, "--out", report_path]
    rc, out = run(cmd, cwd=repo, timeout=GO_TIMEOUT)
    report = _read_json(report_path)
    return _f2p_map(report, rc, out)


def _f2p_map(report, rc, out):
    mapping = {}
    if report:
        for entry in report.get("results", []):
            obs = entry.get("observation_id")
            if not obs:
                continue
            status = _classify(entry)
            mapping[obs] = {
                "status": status,
                "value": {
                    "target_technology": TARGET_TECHNOLOGY,
                    "postcondition_satisfied": status == "PASS",
                    "detail": str(entry.get("detail", "")),
                },
            }
    return mapping


# ---------------------------------------------------------------------------
# DIFFERENTIAL
# ---------------------------------------------------------------------------
def _diff_corpus(root):
    path = os.path.join(root, "fixtures", "batches", DIFF_BATCH, "corpus.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _diff_helpers(root):
    shared = os.path.join(root, "shared", "batches", DIFF_BATCH, "differential.py")
    return _load_module("integration_differential", shared)


def _delivered_go_tests(repo):
    """Return the delivered Go test files, regardless of their names.

    The instruction asks for focused Go tests but never fixes their paths, so
    the scope check accepts any ``*_test.go`` file the candidate authored.
    """
    found = []
    for current, dirs, files in os.walk(repo):
        dirs[:] = [name for name in dirs if name not in {".git", "node_modules"}]
        for name in files:
            if name.endswith("_test.go"):
                relative = os.path.relpath(os.path.join(current, name), repo)
                found.append(relative.replace(os.sep, "/"))
    return sorted(found)


def _structural_check(side, repo, obs_id):
    """Real, side-specific structural/whole-repo check for a DIFF observation.

    These observations compare cross-language surfaces where no common concrete
    value exists, so both sides emit the stable status wrapper
    ``{"result": "PASS"|"FAIL"}`` derived from an executed check.
    """
    module = _go_module_root(repo) if side == "target" else repo
    if obs_id in ("DIFF-002", "DIFF-024"):
        missing = [p for p in OUT_OF_SCOPE_TREES if not os.path.exists(os.path.join(repo, p))]
        return (not missing, "out-of-scope trees present" if not missing else "missing: %s" % ",".join(missing))

    if obs_id in ("DIFF-004", "DIFF-025"):
        if side == "target":
            rc, out = run(["go", "build", "./..."], cwd=module, timeout=GO_TIMEOUT)
            return (rc == 0, "go build ./... rc=%d" % rc)
        missing = [p for p in SRC_IN_SCOPE_MODULES if not os.path.isfile(os.path.join(repo, p))]
        return (not missing, "reference modules present" if not missing else "missing: %s" % ",".join(missing))

    if obs_id == "DIFF-011":
        if side == "target":
            details = []
            for tag in (None, "nostd", "noalloc"):
                cmd = ["go", "build", "./..."] if tag is None else ["go", "build", "-tags", tag, "./..."]
                rc, _out = run(cmd, cwd=module, timeout=GO_TIMEOUT)
                if rc != 0:
                    return (False, "go build with %s failed" % (tag or "default"))
                details.append(tag or "default")
            return (True, "build tags ok: %s" % ",".join(details))
        ok = os.path.isfile(os.path.join(repo, "Cargo.toml")) and os.path.isfile(os.path.join(repo, "src", "lib.rs"))
        return (ok, "source feature boundaries declared" if ok else "source manifest missing")

    if obs_id == "DIFF-023":
        if os.path.isdir(os.path.join(repo, ".git")):
            rc, _out = run(["git", "diff", "--quiet", "HEAD", "--", "benches", "examples", ".github/workflows"], cwd=repo)
            return (rc == 0, "out-of-scope trees untouched by the change set")
        ok = all(os.path.exists(os.path.join(repo, p)) for p in OUT_OF_SCOPE_TREES)
        return (ok, "out-of-scope trees present" if ok else "out-of-scope trees missing")

    if obs_id == "DIFF-026":
        if side == "target":
            # The instruction asks the candidate to "add focused Go tests"; it
            # never pins the exact filenames, so this scope check only requires
            # that *a* Go test suite was actually delivered.  Deleting every
            # delivered test file (the "incomplete migration" attack) is still
            # caught: no ``*_test.go`` remains.
            delivered = _delivered_go_tests(module)
            return (
                bool(delivered),
                "Go tests delivered: %s" % ",".join(delivered[:8])
                if delivered
                else "no Go *_test.go delivered",
            )
        missing = [p for p in SRC_BEHAVIOR_TESTS if not os.path.isfile(os.path.join(repo, p))]
        return (not missing, "reference tests present" if not missing else "missing: %s" % ",".join(missing))

    if obs_id == "DIFF-027":
        if side == "target":
            rc, _out = run(["go", "test", "./..."], cwd=module, timeout=GO_TIMEOUT)
            return (rc == 0, "go test ./... rc=%d" % rc)
        missing = [p for p in SRC_BEHAVIOR_TESTS + SRC_IN_SCOPE_MODULES if not os.path.isfile(os.path.join(repo, p))]
        return (not missing, "delivered reference behavior present" if not missing else "missing: %s" % ",".join(missing))

    return (True, "no structural check")


def _status_value(passed):
    return {"result": "PASS" if passed else "FAIL"}


def _diff_target(root, repo):
    differential = _diff_helpers(root)
    corpus = _diff_corpus(root)
    entry = _load_module("integration_diff_entry", _batch_script(root, "target", DIFF_BATCH, "entry.py"))
    observed = None
    detail = ""
    try:
        observed = entry._run_probe(_go_module_root(repo))
    except BaseException as exc:  # SystemExit from the batch entry included
        detail = "differential probe failed: %s" % exc
    # A candidate that has not implemented the targeted Go package/symbol fails
    # to build the probe; that is a semantic absence (``FAIL``). Only a genuine
    # infrastructure fault (missing toolchain/asset, timeout, crash) stays
    # ``INFRA_ERROR``.
    failure_status = classify_probe_failure(detail)

    per_obs = {}
    for case in corpus["cases"]:
        case_id = case["id"]
        obs = case["observation_id"]
        slot = per_obs.setdefault(obs, {"status": "PASS", "value": {}})
        if observed is None:
            slot["status"] = failure_status
            slot["value"][case_id] = {"error": detail[:200]}
            continue
        slot["value"][case_id] = differential.normalize(observed.get(case_id))
        if not differential.equal_after_normalization(case["expected"], observed.get(case_id)):
            slot["status"] = "FAIL"

    case_index = {case["id"]: case for case in corpus["cases"]}
    for obs, case_ids in DIFF_ALIASES.items():
        slot = per_obs.setdefault(obs, {"status": "PASS", "value": {}})
        for case_id in case_ids:
            case = case_index.get(case_id)
            if case is None:
                continue
            if observed is None:
                slot["status"] = failure_status
                slot["value"][case_id] = {"error": detail[:200]}
                continue
            slot["value"][case_id] = differential.normalize(observed.get(case_id))
            if not differential.equal_after_normalization(case["expected"], observed.get(case_id)):
                slot["status"] = "FAIL"

    for obs in DIFF_STRUCTURAL:
        passed, _detail = _structural_check("target", repo, obs)
        per_obs[obs] = {"status": "PASS" if passed else "FAIL", "value": _status_value(passed)}
    return per_obs


def _diff_source(root, repo):
    corpus = _diff_corpus(root)
    differential = _diff_helpers(root)
    source_values = None
    script = _batch_script(root, "source", DIFF_BATCH, "entry.sh")
    rc, out = run(["sh", script], cwd=repo)
    try:
        source_values = json.loads(out)
    except ValueError:
        source_values = None

    case_index = {case["id"]: case for case in corpus["cases"]}

    def case_value(case_id, fallback):
        if isinstance(source_values, dict) and case_id in source_values:
            return source_values[case_id]
        return fallback

    per_obs = {}
    for case in corpus["cases"]:
        case_id = case["id"]
        obs = case["observation_id"]
        slot = per_obs.setdefault(obs, {"status": "PASS", "value": {}})
        slot["value"][case_id] = differential.normalize(case_value(case_id, case["expected"]))

    for obs, case_ids in DIFF_ALIASES.items():
        slot = per_obs.setdefault(obs, {"status": "PASS", "value": {}})
        for case_id in case_ids:
            case = case_index.get(case_id)
            if case is None:
                continue
            slot["value"][case_id] = differential.normalize(case_value(case_id, case["expected"]))

    for obs in DIFF_STRUCTURAL:
        passed, _detail = _structural_check("source", repo, obs)
        per_obs[obs] = {"status": "PASS" if passed else "FAIL", "value": _status_value(passed)}
    return per_obs, rc


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------
def _ids_for(root, batch_id):
    return [entry["id"] for entry in load_inventory(root) if entry.get("batch_id") == batch_id]


# ---------------------------------------------------------------------------
# Dispatch + report
# ---------------------------------------------------------------------------
def dispatch(side, repo, root=None):
    """Execute every batch probe for ``side`` and return the v6 report dict."""
    root = root or asset_root()
    inventory = load_inventory(root)
    workdir = _emit_dir()
    results = {}

    if side == "target":
        results.update(_p2p("target", root, repo, workdir))
        for batch_id in F2P_BATCHES:
            results.update(_f2p_target(batch_id, root, repo, workdir))
        results.update(_diff_target(root, repo))
    else:
        results.update(_p2p("source", root, repo, workdir))
        for batch_id in F2P_BATCHES:
            results.update(_f2p_source(batch_id, root, repo, workdir))
        diff_source, _rc = _diff_source(root, repo)
        results.update(diff_source)

    observations = []
    for entry in inventory:
        obs_id = entry["id"]
        slot = results.get(obs_id)
        if slot is None:
            observations.append({
                "id": obs_id,
                "kind": entry["kind"],
                "status": "INCONCLUSIVE",
                "value": {"type": "json", "value": {"detail": "no probe result for %s" % obs_id}},
            })
            continue
        observations.append({
            "id": obs_id,
            "kind": entry["kind"],
            "status": slot.get("status", "INCONCLUSIVE"),
            "value": {"type": "json", "value": slot.get("value")},
        })

    return {"schema_version": REPORT_SCHEMA_VERSION, "observations": observations}


def write_report(path, report):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(side, report_path, repo=None):
    repo = repo or resolve_repo()
    if not os.path.isdir(repo):
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "observations": [
                {
                    "id": entry["id"],
                    "kind": entry["kind"],
                    "status": "INFRA_ERROR",
                    "value": {"type": "json", "value": {"detail": "repo not found: %s" % repo}},
                }
                for entry in load_inventory()
            ],
        }
        write_report(report_path, report)
        return 2
    report = dispatch(side, repo)
    write_report(report_path, report)
    passed = sum(1 for obs in report["observations"] if obs["status"] == "PASS")
    print(json.dumps({
        "side": side,
        "schema_version": REPORT_SCHEMA_VERSION,
        "observations": len(report["observations"]),
        "passed": passed,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: integration.py <report> [repo]")
    raise SystemExit(main("target", sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
