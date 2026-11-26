#!/usr/bin/env python3
"""Shared integration harness for the rubyfmt migration asset set.

The harness runs the concrete probes contributed by the P2P, F2P, and
differential batches against either side of the migration and emits a
``migration-observations:v6`` report.  It is intentionally self contained: at
runtime the asset root only contains ``shared/``, ``source/``, ``target/``,
``fixtures/`` and this entry point.
"""

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ASSET_ROOT = pathlib.Path(__file__).resolve().parent
INVENTORY_PATH = ASSET_ROOT / "shared" / "integration" / "observations.json"
REPO_ENV_NAMES = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")
DEFAULT_REPO = pathlib.Path("/workspace/repo")

P2P_BATCH = "BATCH-P2P-0001"
DIFF_BATCH = "BATCH-DIFFERENTIAL-0001"
F2P_BATCHES = ("BATCH-F2P-0001", "BATCH-F2P-0002", "BATCH-F2P-0003")


def resolve_repo():
    for name in REPO_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return pathlib.Path(value).resolve()
    return DEFAULT_REPO.resolve()


def detect_side(repo):
    if (repo / "go.mod").is_file():
        return "target"
    if (repo / "Cargo.toml").is_file():
        return "source"
    return "source"


def load_inventory():
    with INVENTORY_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        items = data.get("observations", [])
    else:
        items = data
    inventory = []
    for item in items:
        inventory.append(
            {
                "id": item["id"],
                "kind": item["kind"],
                "batch_id": item["batch_id"],
            }
        )
    return inventory


def run(command, cwd=None, env=None, timeout=1800):
    try:
        return subprocess.run(
            command,
            cwd=None if cwd is None else str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, b"", str(exc).encode())
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, b"", b"command timed out")


def tail(result, limit=600):
    text = (result.stderr or result.stdout or b"").decode("utf-8", "replace")
    text = text.strip()
    return text[-limit:]


def find_executable(name, extra=()):
    found = shutil.which(name)
    if found:
        return found
    for candidate in extra:
        path = pathlib.Path(os.path.expanduser(candidate))
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def find_go():
    return find_executable("go", ("/usr/local/go/bin/go", "/usr/lib/go/bin/go"))


def find_cargo():
    extras = ["/tmp/cargo/bin/cargo", "~/.cargo/bin/cargo", "/usr/local/cargo/bin/cargo"]
    cargo_home = os.environ.get("CARGO_HOME")
    if cargo_home:
        extras.insert(0, str(pathlib.Path(cargo_home) / "bin" / "cargo"))
    return find_executable("cargo", tuple(extras))


def build_environment():
    env = dict(os.environ)
    env.update(
        {
            "CGO_ENABLED": "1",
            "GOFLAGS": "-mod=vendor",
            "GOTOOLCHAIN": "local",
            "GOCACHE": tempfile.mkdtemp(prefix="rubyfmt-gocache-"),
        }
    )
    return env


def build_binary(side, repo, work):
    """Return (binary, available, infra_error, reason)."""
    if side == "target":
        go = find_go()
        if go is None:
            return None, False, True, "go toolchain unavailable"
        output = work / "rubyfmt-target"
        result = run(
            [go, "build", "-mod=vendor", "-buildvcs=false", "-o", str(output), "./cmd/rubyfmt"],
            cwd=repo,
            env=build_environment(),
            timeout=1800,
        )
        if result.returncode != 0 or not output.is_file():
            return None, False, False, "go build failed: " + tail(result)
        return output, True, False, ""

    cargo = find_cargo()
    if cargo is None:
        return None, False, True, "cargo toolchain unavailable"
    target_dir = work / "cargo-target"
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    result = run([cargo, "build", "--release"], cwd=repo, env=env, timeout=1800)
    output = target_dir / "release" / "rubyfmt-main"
    if result.returncode != 0 or not output.is_file():
        return None, False, False, "cargo build failed: " + tail(result)
    return output, True, False, ""


def native_suite(side, repo):
    if side == "target":
        tool = find_go()
        if tool is None:
            return {}
        command = [tool, "test", "./..."]
        result = run(command, cwd=repo, env=build_environment(), timeout=1800)
        return {"P2P-015": {"result": "PASS" if result.returncode == 0 else "FAIL", "detail": "go test ./..."}}
    tool = find_cargo()
    if tool is None:
        return {}
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = tempfile.mkdtemp(prefix="rubyfmt-cargo-tests-")
    command = [tool, "test", "--no-fail-fast"]
    result = run(command, cwd=repo, env=env, timeout=1800)
    return {"P2P-015": {"result": "PASS" if result.returncode == 0 else "FAIL", "detail": "cargo test --no-fail-fast"}}


def run_p2p(side, repo, binary, work, suite=None):
    probe = ASSET_ROOT / "shared" / "batches" / P2P_BATCH / "p2p_probe.py"
    if not probe.is_file() or binary is None:
        return None, "P2P probe unavailable"
    output = work / "p2p-results.json"
    env = dict(os.environ)
    env["SUITE_META"] = json.dumps(suite or {})
    result = run(
        [
            sys.executable,
            str(probe),
            "--binary",
            str(binary),
            "--repo",
            str(repo),
            "--side",
            side,
            "--out",
            str(output),
        ],
        env=env,
        timeout=1800,
    )
    if not output.is_file():
        return None, "P2P probe produced no report: " + tail(result)
    with output.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("results", {}), ""


def run_differential(repo, binary, work):
    corpus = ASSET_ROOT / "shared" / "batches" / DIFF_BATCH / "corpus.py"
    expected_path = ASSET_ROOT / "fixtures" / "batches" / DIFF_BATCH / "expected-results.json"
    if not corpus.is_file() or binary is None:
        return None, {}, "differential probe unavailable"
    expected = {}
    if expected_path.is_file():
        with expected_path.open("r", encoding="utf-8") as handle:
            expected = json.load(handle)
    output = work / "differential-results.json"
    result = run(
        [
            sys.executable,
            str(corpus),
            "--binary",
            str(binary),
            "--repo",
            str(repo),
            "--output",
            str(output),
            "--expected",
            str(expected_path),
        ],
        timeout=1800,
    )
    if not output.is_file():
        return None, expected, "differential probe produced no report: " + tail(result)
    with output.open("r", encoding="utf-8") as handle:
        actual = json.load(handle)
    return actual, expected, ""


def load_json(path):
    try:
        with pathlib.Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def run_f2p(batch_id, side, repo, binary, work):
    probe = ASSET_ROOT / side / "batches" / batch_id / "probe.py"
    if not probe.is_file():
        return {}, "probe missing: " + str(probe)

    if batch_id == "BATCH-F2P-0001":
        output = work / (batch_id + ".json")
        command = [sys.executable, str(probe), "--repo", str(repo), "--json", str(output)]
        if side == "target" and binary is not None:
            command.extend(["--binary", str(binary)])
        result = run(command, timeout=1800)
        payload = load_json(output)
        if not isinstance(payload, dict):
            return {}, "probe produced no report: " + tail(result)
        return payload.get("results", {}), ""

    if batch_id == "BATCH-F2P-0002":
        output = work / (batch_id + ".json")
        result = run([sys.executable, str(probe), str(repo), str(output)], timeout=1800)
        payload = load_json(output)
        if not isinstance(payload, dict):
            return {}, "probe produced no report: " + tail(result)
        return payload.get("observations", {}), ""

    if batch_id == "BATCH-F2P-0003":
        command = [sys.executable, str(probe), "--repo", str(repo), "--json"]
        if side == "target" and binary is not None:
            command.extend(["--binary", str(binary)])
        result = run(command, timeout=1800)
        text = (result.stdout or b"").decode("utf-8", "replace").strip()
        try:
            payload = json.loads(text)
        except ValueError:
            return {}, "probe produced no report: " + tail(result)
        if not isinstance(payload, list):
            return {}, "probe produced an unexpected report"
        return {item["observation_id"]: item for item in payload}, ""

    return {}, "unknown F2P batch: " + batch_id


def status_of(result):
    if not result:
        return None
    return result.get("status") or result.get("result")


FIXTURE_P2P = {
    "P2P-001": "fixtures/small/rspec_its_actual.rb",
    "P2P-008": "fixtures/small/rspec_its_actual.rb",
    "P2P-018": "fixtures/small/rspec_its_actual.rb",
    "P2P-030": "fixtures/small/rspec_its_expected.rb",
}


def verify_fixture(repo, binary, rel_actual):
    """Run the formatter on a fixture pair and return a stable projection."""
    if binary is None:
        return None
    root = pathlib.Path(repo)
    actual = root / rel_actual
    if not actual.is_file():
        return {"result": "FAIL"}
    if rel_actual.endswith("_actual.rb"):
        expected = pathlib.Path(str(actual)[: -len("_actual.rb")] + "_expected.rb")
    else:
        expected = actual
    if not expected.is_file():
        return {"result": "FAIL"}
    expected_bytes = expected.read_bytes()
    first = run([str(binary), str(actual)], cwd=repo, timeout=300)
    second = run([str(binary), str(expected)], cwd=repo, timeout=300)
    formatted = first.stdout or b""
    ok = (
        first.returncode == 0
        and formatted == expected_bytes
        and second.returncode == 0
        and (second.stdout or b"") == expected_bytes
    )
    return {
        "result": "PASS" if ok else "FAIL",
        "formatted_sha256": hashlib.sha256(formatted).hexdigest(),
        "formatted_length": len(formatted),
    }


def build_report(side, repo):
    work = pathlib.Path(tempfile.mkdtemp(prefix="rubyfmt-integration-"))
    inventory = load_inventory()
    binary, available, infra_error, build_reason = build_binary(side, repo, work)
    suite = native_suite(side, repo)

    p2p_results, p2p_error = run_p2p(side, repo, binary, work, suite)
    fixture_results = {
        observation_id: verify_fixture(repo, binary, rel_actual)
        for observation_id, rel_actual in FIXTURE_P2P.items()
    }
    diff_actual, diff_expected, diff_error = run_differential(repo, binary, work)

    f2p_results = {}
    f2p_errors = {}
    for batch_id in F2P_BATCHES:
        results, error = run_f2p(batch_id, side, repo, binary, work)
        f2p_results[batch_id] = results
        f2p_errors[batch_id] = error

    observations = []
    for item in inventory:
        observation_id = item["id"]
        kind = item["kind"]
        batch_id = item["batch_id"]
        reason = ""

        if kind == "P2P":
            raw = (p2p_results or {}).get(observation_id)
            direct = fixture_results.get(observation_id)
            raw_status = status_of(raw)
            direct_status = status_of(direct)

            if observation_id in FIXTURE_P2P:
                source = direct if direct_status is not None else raw
                status = direct_status or raw_status
                if status is None:
                    status = "INCONCLUSIVE"
                value = {"result": status}
                if source and source.get("formatted_sha256"):
                    value["formatted_sha256"] = source["formatted_sha256"]
                    value["formatted_length"] = source.get("formatted_length")
            else:
                status = raw_status
                if status is None:
                    status = "INCONCLUSIVE"
                value = {"result": status}
        elif kind == "DIFFERENTIAL":
            if diff_actual is None or observation_id not in diff_actual:
                status = "INCONCLUSIVE" if infra_error else "FAIL"
                value = {"result": status}
            else:
                value = diff_actual[observation_id]
                status = "PASS" if diff_expected.get(observation_id) == value else "FAIL"
        elif kind == "F2P":
            raw = f2p_results.get(batch_id, {}).get(observation_id)
            status = status_of(raw)
            reason = str((raw or {}).get("reason") or (raw or {}).get("detail") or f2p_errors.get(batch_id) or "")
            if status is None:
                status = "INCONCLUSIVE"
                value = {"observed": status}
            else:
                value = {
                    "observed": status,
                    "check": raw.get("check") or batch_id,
                }
        else:
            status = "INCONCLUSIVE"
            value = {"observed": status}

        observations.append(
            {
                "id": observation_id,
                "kind": kind,
                "status": status,
                "reason": reason,
                "value": {"type": "json", "value": value},
            }
        )

    shutil.rmtree(work, ignore_errors=True)
    return {
        "schema_version": "migration-observations:v6",
        "side": side,
        "observations": observations,
    }


def main(argv=None, side_override=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    report_path = pathlib.Path(argv[0]) if argv else pathlib.Path("/logs/result.json")
    repo = resolve_repo()
    side = side_override or detect_side(repo)
    report = build_report(side, repo)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    failures = [item for item in report["observations"] if item["status"] not in ("PASS", "FAIL", "NA")]
    print(
        json.dumps(
            {
                "side": side,
                "report": str(report_path),
                "observations": len(report["observations"]),
                "unexpected": len(failures),
            }
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
