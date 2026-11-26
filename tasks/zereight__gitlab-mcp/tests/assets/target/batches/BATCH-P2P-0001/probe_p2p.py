"""Target-side P2P probe for BATCH-P2P-0001.

Runs against the migrated Python repository. For every P2P observation owned by
this batch it executes the migrated package's own test suite (or hashes the
preserved root artifacts) and emits a raw PASS/FAIL plus the normalized
projection shared with the source side.

The projection is never a one-sided status value: root-artifact observations
emit the same sha256 digest map the source probe emits, and behavior
observations emit the shared canonical value declared in ``probes.json``.

Usage:
    python probe_p2p.py [--repo <target-root>] [--probes <probes.json>] [--out <file>] [--python <interpreter>]

Environment fallbacks: P2P_REPO_ROOT, P2P_PROBES, P2P_OUT, P2P_PYTHON.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROBES = os.path.abspath(
    os.path.join(HERE, "..", "..", "..", "shared", "batches", "BATCH-P2P-0001", "probes.json")
)
MCP_SERVER_REL = "mcp-server"
_DISCOVERY_CACHE = {}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_map(repo, paths):
    return {path: sha256(os.path.join(repo, path)) for path in paths}


def _discover_suite(repo, python):
    """Run the ported suite and independent required assertions once and cache it.

    The migration contract fixes the ported suite location and behaviour, not
    the names of its modules or test classes, so the ``target_unittest`` checks
    use public contract assertions and discovery, while candidate-added test
    outcomes remain recorded diagnostics rather than extra task requirements.
    """
    if repo in _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE[repo]
    integration = os.path.abspath(os.path.join(HERE, "../../../shared/integration"))
    completed = subprocess.run(
        [python, os.path.join(integration, "public_suite.py"), "--repo", repo, "--python", python],
        cwd=repo, capture_output=True, text=True,
    )
    try:
        details = json.loads(completed.stdout)
    except ValueError:
        details = {"ok": False, "error": completed.stderr[-4000:]}
    result = {"ok": completed.returncode == 0 and details.get("ok") is True,
              "returncode": completed.returncode, "details": details,
              "tail": [] if details.get("ok") else [json.dumps(details, ensure_ascii=False)]}
    _DISCOVERY_CACHE[repo] = result
    return result


def run_unittest(repo, tests, python):
    result = dict(_discover_suite(repo, python))
    result["tests"] = tests
    return result


def run_check(repo, check, python):
    if check["kind"] == "file_digest":
        return {"ok": True, "value": digest_map(repo, check["paths"])}
    if check["kind"] == "target_unittest":
        return run_unittest(repo, check["tests"], python)
    raise ValueError("unsupported target check kind: {}".format(check["kind"]))


def main():
    parser = argparse.ArgumentParser(description="Target-side P2P probe.")
    parser.add_argument("--repo", default=os.environ.get("P2P_REPO_ROOT", os.getcwd()))
    parser.add_argument("--probes", default=os.environ.get("P2P_PROBES", DEFAULT_PROBES))
    parser.add_argument("--out", default=os.environ.get("P2P_OUT"))
    parser.add_argument("--python", default=os.environ.get("P2P_PYTHON", sys.executable))
    args = parser.parse_args()

    with open(args.probes, "r", encoding="utf-8") as handle:
        spec = json.load(handle)

    report = {"side": "target", "repo": args.repo, "observations": {}}
    for probe in spec["probes"]:
        check = run_check(args.repo, probe["target"], args.python)
        status = "PASS" if check["ok"] else "FAIL"
        if probe["projection"] == "sha256":
            value = check["value"]
        else:
            value = probe["canonical_value"]
        report["observations"][probe["observation_id"]] = {
            "status": status,
            "value": value,
            "raw": check,
            "reason": "\n".join(check.get("tail", [])) if not check["ok"] else "",
        }

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
