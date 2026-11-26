#!/usr/bin/env python3
"""Shared integration harness for the Cobra -> Kong arkade migration.

The harness reads the self-contained observation inventory from
``shared/integration/observations.json`` and executes the per-batch runtime
probes that were authored by the P2P, F2P and DIFFERENTIAL batches.  It emits a
``migration-observations:v6`` report containing the real source or target
observation for every required identifier.

Only files that are part of the published asset root are used.  Authoring
paths such as ``dependencies/`` and ``fragments/`` are never read.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import f2p0003


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ASSET_ROOT = os.path.dirname(os.path.dirname(HERE))
OBSERVATIONS_FILE = os.path.join(HERE, "observations.json")
REPO_ENV_KEYS = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")
VALID_STATUS = {"PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return None


def write_json(path, value):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def load_inventory():
    payload = read_json(OBSERVATIONS_FILE)
    if not isinstance(payload, list):
        return []
    inventory = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        observation_id = item.get("id")
        kind = item.get("kind")
        batch_id = item.get("batch_id")
        if observation_id and kind and batch_id:
            inventory.append(
                {"id": observation_id, "kind": kind, "batch_id": batch_id}
            )
    return inventory


def repository_root():
    for key in REPO_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return os.path.abspath(value)
    return "/workspace/repo"


def build_binary(repo, temporary):
    binary = os.path.join(temporary, "arkade")
    env = os.environ.copy()
    env.setdefault("CGO_ENABLED", "0")
    attempts = (
        ["go", "build", "-mod=vendor", "-o", binary, "."],
        ["go", "build", "-o", binary, "."],
    )
    last_output = ""
    for command in attempts:
        try:
            process = subprocess.run(
                command,
                cwd=repo,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_output = repr(exc)
            continue
        if process.returncode == 0 and os.path.isfile(binary):
            return binary, None
        last_output = (process.stdout or "") + (process.stderr or "")
    return None, last_output


def execute(command, cwd, timeout):
    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except OSError as exc:
        return None, repr(exc)
    return process, None


def json_from_stdout(process):
    if process is None:
        return None
    text = process.stdout or ""
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


class Collector:
    def __init__(self, inventory):
        self.inventory = inventory
        self.kinds = {item["id"]: item["kind"] for item in inventory}
        self.statuses = {item["id"]: "INCONCLUSIVE" for item in inventory}
        self.values = {
            item["id"]: {"result": "INCONCLUSIVE"} for item in inventory
        }

    def set(self, observation_id, status, value=None):
        if observation_id not in self.statuses:
            return
        if status not in VALID_STATUS:
            status = "INCONCLUSIVE"
        self.statuses[observation_id] = status
        if value is None:
            value = {"result": status}
        self.values[observation_id] = value

    def fail(self, observation_ids, status="INFRA_ERROR"):
        for observation_id in observation_ids:
            self.set(observation_id, status, {"result": status})


def groups_by_batch(inventory):
    grouped = {}
    for item in inventory:
        grouped.setdefault(item["batch_id"], []).append(item["id"])
    return grouped


def run_p2p(asset_root, repo, side, temporary, collector, ids):
    script = os.path.join(
        asset_root, "shared", "batches", "BATCH-P2P-0001", "probe.py"
    )
    if not os.path.isfile(script):
        collector.fail(ids)
        return
    output = os.path.join(temporary, "p2p.json")
    process, error = execute(
        [
            sys.executable,
            script,
            "--repo",
            repo,
            "--side",
            side,
            "--out",
            output,
        ],
        asset_root,
        2400,
    )
    if error == "TIMEOUT":
        collector.fail(ids, "TIMEOUT")
        return
    if error:
        collector.fail(ids)
        return
    payload = read_json(output)
    if not isinstance(payload, dict):
        collector.fail(ids)
        return
    for observation_id in ids:
        item = payload.get(observation_id)
        status = item.get("result") if isinstance(item, dict) else None
        if status in VALID_STATUS:
            collector.set(observation_id, status, {"result": status})
        else:
            collector.set(
                observation_id, "INCONCLUSIVE", {"result": "INCONCLUSIVE"}
            )


def run_f2p0001(asset_root, repo, binary, side, temporary, collector, ids):
    script = os.path.join(
        asset_root, "shared", "batches", "BATCH-F2P-0001", "probe.py"
    )
    if not os.path.isfile(script):
        collector.fail(ids)
        return
    process, error = execute(
        [sys.executable, script, repo, binary, side],
        asset_root,
        2400,
    )
    if error == "TIMEOUT":
        collector.fail(ids, "TIMEOUT")
        return
    payload = json_from_stdout(process)
    if not isinstance(payload, dict):
        collector.fail(ids)
        return
    for observation_id in ids:
        item = payload.get(observation_id)
        status = item.get("status") if isinstance(item, dict) else None
        if status in VALID_STATUS:
            collector.set(observation_id, status, {"result": status})
        else:
            collector.set(
                observation_id, "INCONCLUSIVE", {"result": "INCONCLUSIVE"}
            )


def run_f2p0002(asset_root, repo, binary, side, temporary, collector, ids):
    script = os.path.join(
        asset_root, "shared", "batches", "BATCH-F2P-0002", "run_probes.py"
    )
    if not os.path.isfile(script):
        collector.fail(ids)
        return
    process, error = execute(
        [
            sys.executable,
            script,
            "--repo",
            repo,
            "--binary",
            binary,
            "--side",
            side,
        ],
        asset_root,
        2400,
    )
    if error == "TIMEOUT":
        collector.fail(ids, "TIMEOUT")
        return
    payload = json_from_stdout(process)
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, dict):
        collector.fail(ids)
        return
    for observation_id in ids:
        status = observations.get(observation_id)
        if status in VALID_STATUS:
            collector.set(observation_id, status, {"result": status})
        else:
            collector.set(
                observation_id, "INCONCLUSIVE", {"result": "INCONCLUSIVE"}
            )


def run_f2p0003(repo, binary, collector, ids):
    results = f2p0003.evaluate(repo, binary)
    for observation_id in ids:
        item = results.get(observation_id)
        status = item.get("status") if isinstance(item, dict) else None
        if status in VALID_STATUS:
            collector.set(observation_id, status, {"result": status})
        else:
            collector.set(
                observation_id, "INCONCLUSIVE", {"result": "INCONCLUSIVE"}
            )


def run_differential0001(asset_root, binary, temporary, collector, ids):
    script = os.path.join(
        asset_root, "shared", "batches", "BATCH-DIFFERENTIAL-0001", "probe.py"
    )
    fixture = os.path.join(
        asset_root,
        "fixtures",
        "batches",
        "BATCH-DIFFERENTIAL-0001",
        "source-normalized.json",
    )
    if not os.path.isfile(script) or not os.path.isfile(fixture):
        collector.fail(ids)
        return
    actual_path = os.path.join(temporary, "diff0001-actual.json")
    process, error = execute(
        [
            sys.executable,
            script,
            "--binary",
            binary,
            "--emit",
            actual_path,
        ],
        asset_root,
        2400,
    )
    if error == "TIMEOUT":
        collector.fail(ids, "TIMEOUT")
        return
    actual = read_json(actual_path)
    expected = read_json(fixture)
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        collector.fail(ids)
        return
    for observation_id in ids:
        observed = actual.get(observation_id)
        wanted = expected.get(observation_id)
        if observed is None or wanted is None:
            collector.set(
                observation_id,
                "INCONCLUSIVE",
                {"result": "INCONCLUSIVE"},
            )
            continue
        status = "PASS" if canonical(observed) == canonical(wanted) else "FAIL"
        collector.set(observation_id, status, observed)


def run_differential0002(asset_root, repo, temporary, collector, ids):
    script = os.path.join(
        asset_root, "shared", "batches", "BATCH-DIFFERENTIAL-0002", "probe.py"
    )
    fixture = os.path.join(
        asset_root,
        "fixtures",
        "batches",
        "BATCH-DIFFERENTIAL-0002",
        "baseline.json",
    )
    if not os.path.isfile(script) or not os.path.isfile(fixture):
        collector.fail(ids)
        return
    output = os.path.join(temporary, "diff0002-observed.json")
    process, error = execute(
        [sys.executable, script, "--repo", repo, "--out", output],
        asset_root,
        2400,
    )
    if error == "TIMEOUT":
        collector.fail(ids, "TIMEOUT")
        return
    actual_document = read_json(output)
    baseline_document = read_json(fixture)
    actual = (
        actual_document.get("observations")
        if isinstance(actual_document, dict)
        else None
    )
    baseline = (
        baseline_document.get("observations")
        if isinstance(baseline_document, dict)
        else None
    )
    if not isinstance(actual, dict) or not isinstance(baseline, dict):
        collector.fail(ids)
        return
    for observation_id in ids:
        observed = actual.get(observation_id)
        wanted = baseline.get(observation_id)
        if observed is None or wanted is None:
            collector.set(
                observation_id,
                "INCONCLUSIVE",
                {"result": "INCONCLUSIVE"},
            )
            continue
        status = "PASS" if canonical(observed) == canonical(wanted) else "FAIL"
        collector.set(observation_id, status, observed)


def build_report(inventory, collector):
    observations = []
    for item in inventory:
        observation_id = item["id"]
        status = collector.statuses.get(observation_id, "INCONCLUSIVE")
        value = collector.values.get(observation_id, {"result": status})
        observations.append(
            {
                "id": observation_id,
                "kind": item["kind"],
                "status": status,
                "value": {"type": "json", "value": value},
            }
        )
    return {
        "schema_version": "migration-observations:v6",
        "observations": observations,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", required=True, choices=("source", "target"))
    parser.add_argument("--report", required=True)
    parser.add_argument("--asset-root")
    args = parser.parse_args()

    asset_root = os.path.abspath(
        args.asset_root or DEFAULT_ASSET_ROOT
    )
    inventory = load_inventory()
    if not inventory:
        print("observation inventory is missing or empty", file=sys.stderr)
        return 1

    collector = Collector(inventory)
    grouped = groups_by_batch(inventory)
    repo = repository_root()

    with tempfile.TemporaryDirectory(prefix="arkade-integration-") as temporary:
        binary, build_error = build_binary(repo, temporary)
        if binary is None:
            collector.fail([item["id"] for item in inventory])
        else:
            if "BATCH-P2P-0001" in grouped:
                run_p2p(
                    asset_root,
                    repo,
                    args.side,
                    temporary,
                    collector,
                    grouped["BATCH-P2P-0001"],
                )
            if "BATCH-F2P-0001" in grouped:
                run_f2p0001(
                    asset_root,
                    repo,
                    binary,
                    args.side,
                    temporary,
                    collector,
                    grouped["BATCH-F2P-0001"],
                )
            if "BATCH-F2P-0002" in grouped:
                run_f2p0002(
                    asset_root,
                    repo,
                    binary,
                    args.side,
                    temporary,
                    collector,
                    grouped["BATCH-F2P-0002"],
                )
            if "BATCH-F2P-0003" in grouped:
                run_f2p0003(
                    repo,
                    binary,
                    collector,
                    grouped["BATCH-F2P-0003"],
                )
            if "BATCH-DIFFERENTIAL-0001" in grouped:
                run_differential0001(
                    asset_root,
                    binary,
                    temporary,
                    collector,
                    grouped["BATCH-DIFFERENTIAL-0001"],
                )
            if "BATCH-DIFFERENTIAL-0002" in grouped:
                run_differential0002(
                    asset_root,
                    repo,
                    temporary,
                    collector,
                    grouped["BATCH-DIFFERENTIAL-0002"],
                )

    report = build_report(inventory, collector)
    write_json(args.report, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
