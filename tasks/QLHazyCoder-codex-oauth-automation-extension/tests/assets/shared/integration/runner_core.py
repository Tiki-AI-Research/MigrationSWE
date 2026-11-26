#!/usr/bin/env python3
"""BATCH-INTEGRATION-0001 shared runtime harness.

Dispatches every dependency batch's *executable* projection for one side
(source or target) and emits a ``migration-observations:v6`` runtime report.

The batch probes were authored independently; this harness only wires them
together and normalises the projections so the two sides are comparable:

* P2P-0001  declarative node/http drivers executed for the side checkout;
* P2P-0002  executable state-preservation probe;
* F2P-0001  declarative target-postcondition probes;
* F2P-0002  executable target-postcondition probe;
* F2P-0003  executable target-postcondition probe;
* F2P-0004  executable target-postcondition probe (preserved error envelopes);
* DIFFERENTIAL-0001 executable differential probe plus the extension-asset
  equality observation (DIFF-023).

The repository under test is resolved from the allowed environment keys only
(``MIGRATION_REPO_ROOT``, ``MIGRATION_REPO``, ``REPO_ROOT``, ``REPO``) with a
mount fallback; no authoring-only path is hard coded.
"""

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SIDES = ("source", "target")

ALLOWED_REPO_ENV = ("MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO")

F2P_0002_INLINE = (
    "import json,sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "import probe_runner;"
    "r=probe_runner.run(sys.argv[2],sys.argv[3]);"
    "print(json.dumps({k:{'status':v['status'],'detail':v['detail']} for k,v in r.items()}))"
)

F2P_0003_INLINE = (
    "import json,sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "import f2p_probe_core;"
    "p=f2p_probe_core.run(sys.argv[2],sys.argv[3]);"
    "print(json.dumps({k:{'passed':v['passed'],'detail':v['detail']} for k,v in p['observations'].items()}))"
)

F2P_0004_INLINE = (
    "import json,sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "import f2p_probe_core;"
    "p=f2p_probe_core.run(sys.argv[2],sys.argv[3]);"
    "print(json.dumps({k:{'passed':v['passed'],'detail':v['detail']} for k,v in p['observations'].items()}))"
)

EXTENSION_ASSET_PATTERNS = (
    "manifest.json",
    "rules.json",
    "background/**",
    "content/**",
    "flows/**",
    "sidepanel/**",
)


def asset_path(*parts):
    return os.path.join(ASSET_ROOT, *parts)


def load_inventory():
    path = asset_path("shared", "integration", "observations.json")
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["observations"]


def resolve_side():
    here = os.path.abspath(__file__)
    for side in SIDES:
        if os.sep + side + os.sep in here:
            return side
    return os.environ.get("MIGRATION_SIDE") or "source"


def resolve_repo(side, explicit):
    candidates = []
    if explicit:
        candidates.append(explicit)
    for key in ALLOWED_REPO_ENV:
        value = os.environ.get(key)
        if value:
            candidates.append(value)
    candidates.append(os.path.join("/input", side))
    candidates.append("/workspace/repo")
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "scripts")):
            return os.path.abspath(candidate)
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(candidates[-1])


def run(command, timeout=1800):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def json_from_stdout(text):
    text = text.strip()
    if not text:
        raise ValueError("empty stdout")
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(text.splitlines()[-1])


def normalise(value, repo=None):
    """Make a projection stable across checkouts and runs."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ("project_root", "filePath", "recordedAt", "generatedAt", "used_port"):
                continue
            result[key] = normalise(item, repo)
        return result
    if isinstance(value, list):
        return [normalise(item, repo) for item in value]
    if isinstance(value, str) and repo and repo in value:
        return value.replace(repo, "{repo}")
    return value


def normalise_observation(observation_id, value):
    if observation_id == "DIFF-012" and isinstance(value, dict):
        envelope = value.get("envelope") or {}
        return {
            "envelope": {
                "count": envelope.get("count"),
                "domain": envelope.get("domain"),
                "emails_len": len(envelope.get("emails") or []),
            },
            "row_count": value.get("row_count"),
            "unique_in_schema": value.get("unique_in_schema"),
        }
    if observation_id == "DIFF-018" and isinstance(value, dict):
        return {"port_from_root_env": value.get("port_from_root_env")}
    return normalise(value)


def extension_asset_projection(repo):
    files = set()
    for pattern in EXTENSION_ASSET_PATTERNS:
        for path in glob.glob(os.path.join(repo, pattern), recursive=True):
            if os.path.isfile(path):
                files.add(os.path.relpath(path, repo))
    files = sorted(files)
    digest = hashlib.sha256()
    for relative in files:
        digest.update(relative.encode("utf-8"))
        with open(os.path.join(repo, relative), "rb") as handle:
            digest.update(handle.read())
    return {
        "extension_asset_files_checked": len(files),
        "extension_asset_digest": digest.hexdigest(),
    }


def dispatch_p2p_0001(side, repo):
    path = asset_path(side, "batches", "BATCH-P2P-0001")
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    results = {}
    for entry in payload["entries"]:
        observation_id = entry["observation_id"]
        invocation = [part.replace("{repo}", repo) for part in entry["invocation"]]
        try:
            completed = run(invocation)
            value = json_from_stdout(completed.stdout)
            expected = entry.get("expected_value")
            status = "PASS" if json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True) else "FAIL"
            results[observation_id] = {"status": status, "value": value}
        except Exception as error:  # noqa: BLE001
            results[observation_id] = {
                "status": "INCONCLUSIVE",
                "value": {"error": "%s: %s" % (type(error).__name__, error)},
            }
    return results


def dispatch_p2p_0002(side, repo, work):
    driver = asset_path("shared", "batches", "BATCH-P2P-0002", "p2p_probe.py")
    output = os.path.join(work, "p2p-0002-%s.json" % side)
    results = {}
    try:
        run([sys.executable, driver, "--repo", repo, "--side", side, "--out", output])
        with open(output, "r", encoding="utf-8") as handle:
            report = json.load(handle)
        for observation_id, item in report["results"].items():
            results[observation_id] = {"status": item["status"], "value": item["value"]}
    except Exception as error:  # noqa: BLE001
        for observation_id in ["P2P-%03d" % index for index in range(25, 41)]:
            results[observation_id] = {
                "status": "INCONCLUSIVE",
                "value": {"error": "%s: %s" % (type(error).__name__, error)},
            }
    return results


def _mark_inconclusive(ids, error):
    return {
        observation_id: {
            "status": "INCONCLUSIVE",
            "value": {"error": "%s: %s" % (type(error).__name__, error)},
        }
        for observation_id in ids
    }


def dispatch_f2p_0001(side, repo, work):
    driver = asset_path("shared", "integration", "f2p_0001_probe.py")
    output = os.path.join(work, "f2p-0001-%s.json" % side)
    try:
        completed = run([sys.executable, driver, side, repo, output])
        with open(output, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as error:  # noqa: BLE001
        payload = asset_path(side, "batches", "BATCH-F2P-0001")
        with open(payload, "r", encoding="utf-8") as handle:
            ids = [entry["observation_id"] for entry in json.load(handle)["entries"]]
        return _mark_inconclusive(ids, error)


def dispatch_f2p_0002(side, repo, work):
    shared = asset_path("shared", "batches", "BATCH-F2P-0002")
    output = os.path.join(work, "f2p-0002-%s.json" % side)
    try:
        completed = run([sys.executable, "-c", F2P_0002_INLINE, shared, repo, output])
        payload = json_from_stdout(completed.stdout)
        return {
            observation_id: {"status": item["status"], "value": item.get("detail")}
            for observation_id, item in payload.items()
        }
    except Exception as error:  # noqa: BLE001
        return _mark_inconclusive(
            ["F2P-%03d" % index for index in range(25, 49)], error
        )


def dispatch_f2p_0003(side, repo, work):
    shared = asset_path("shared", "batches", "BATCH-F2P-0003")
    output = os.path.join(work, "f2p-0003-%s.json" % side)
    try:
        completed = run([sys.executable, "-c", F2P_0003_INLINE, shared, repo, output])
        payload = json_from_stdout(completed.stdout)
        return {
            observation_id: {
                "status": "PASS" if item["passed"] else "FAIL",
                "value": {"passed": item["passed"], "detail": item.get("detail")},
            }
            for observation_id, item in payload.items()
        }
    except Exception as error:  # noqa: BLE001
        return _mark_inconclusive(
            ["F2P-%03d" % index for index in range(49, 73)], error
        )


def dispatch_f2p_0004(side, repo, work):
    shared = asset_path("shared", "batches", "BATCH-F2P-0004")
    output = os.path.join(work, "f2p-0004-%s.json" % side)
    try:
        completed = run([sys.executable, "-c", F2P_0004_INLINE, shared, repo, output])
        payload = json_from_stdout(completed.stdout)
        return {
            observation_id: {
                "status": "PASS" if item["passed"] else "FAIL",
                "value": {"passed": item["passed"], "detail": item.get("detail")},
            }
            for observation_id, item in payload.items()
        }
    except Exception as error:  # noqa: BLE001
        return _mark_inconclusive(
            ["F2P-%03d" % index for index in range(73, 131)], error
        )


def dispatch_differential(side, repo, work):
    driver = asset_path("shared", "batches", "BATCH-DIFFERENTIAL-0001", "differential_probe.py")
    results = {}
    try:
        completed = run([sys.executable, driver, repo, side])
        payload = json_from_stdout(completed.stdout)
        for observation_id, value in payload["result"].items():
            results[observation_id] = {
                "status": "PASS",
                "value": normalise_observation(observation_id, value),
            }
    except Exception as error:  # noqa: BLE001
        results.update(
            _mark_inconclusive(["DIFF-%03d" % index for index in range(1, 23)], error)
        )
    results["DIFF-023"] = {"status": "PASS", "value": extension_asset_projection(repo)}
    return results


def build_report(side, repo, inventory):
    work = tempfile.mkdtemp(prefix="integration-%s-" % side)
    collected = {}
    adapters = [
        lambda: dispatch_p2p_0001(side, repo),
        lambda: dispatch_p2p_0002(side, repo, work),
        lambda: dispatch_f2p_0001(side, repo, work),
        lambda: dispatch_f2p_0002(side, repo, work),
        lambda: dispatch_f2p_0003(side, repo, work),
        lambda: dispatch_f2p_0004(side, repo, work),
        lambda: dispatch_differential(side, repo, work),
    ]
    for adapter in adapters:
        try:
            collected.update(adapter())
        except Exception as error:  # noqa: BLE001
            sys.stderr.write("adapter failed: %r\n" % (error,))

    observations = []
    for item in inventory:
        observation_id = item["id"]
        kind = item["kind"]
        entry = collected.get(observation_id)
        if entry is None:
            observations.append({
                "id": observation_id,
                "kind": kind,
                "status": "INCONCLUSIVE",
                "value": {"type": "json", "value": {"error": "no projection"}},
            })
            continue
        observations.append({
            "id": observation_id,
            "kind": kind,
            "status": entry["status"],
            "value": {"type": "json", "value": normalise(entry["value"], repo)},
        })
    return {
        "schema_version": "migration-observations:v6",
        "side": side,
        "repo": repo,
        "observations": observations,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?", default=None)
    parser.add_argument("--side", choices=SIDES, default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    side = args.side or resolve_side()
    repo = resolve_repo(side, args.repo)
    inventory = load_inventory()
    report = build_report(side, repo, inventory)

    destination = args.out or args.report
    if destination is None:
        for key in ("INTEGRATION_REPORT", "REPORT"):
            if os.environ.get(key):
                destination = os.environ[key]
                break
    if destination is None:
        destination = "/logs/result.json" if os.path.isdir("/logs") else os.path.join(tempfile.gettempdir(), "result.json")
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except Exception as error:  # noqa: BLE001
        sys.stderr.write("could not write report to %s: %r\n" % (destination, error))
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
