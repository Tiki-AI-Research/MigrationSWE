#!/usr/bin/env python3
"""Rebuild baseline evidence from measured base and gold runs.

Run inside the evaluator image, where the offline npm cache, Node 14 worker,
network namespaces, and /opt/sharing-base are available.  A failed node or
unavailable sandbox stops before either asset is replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from sandbox import cleanup_candidate_workspace, prepare_candidate_workspace
from test_integrity import evaluate_test_integrity
from verifier import _differential_case_ids, _load_baseline_evidence, _prepare_test_workspace, run_node


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def script_for(workspace: Path, node_id: str) -> tuple[Path, str]:
    group, stem = node_id.split("::", 1)
    script = f"suite/{group}/{'behavior-oracle' if group == 'differential' else stem}.js"
    path = workspace / script
    if not path.is_file():
        raise ValueError(f"missing test script for {node_id}: {path}")
    return path, script


def measured_side(workspace: Path, node_id: str, repo: Path, base: Path) -> dict:
    script, display = script_for(workspace, node_id)
    scratch = None
    try:
        scratch, candidate = prepare_candidate_workspace(repo)
        result = run_node(
            workspace, script, timeout=180,
            env_extra={"SHARING_BASE_DIR": str(base)} if node_id.startswith("differential::") else {},
            candidate_repo=candidate,
        )
    finally:
        if scratch is not None:
            cleanup_candidate_workspace(scratch)
    status = result.get("status")
    if status in {"infrastructure_error", "candidate_timeout"}:
        raise RuntimeError(f"{node_id} could not be measured: {result}")
    return {
        "status": status,
        "exit_code": result.get("exit_code"),
        "command": ["node", display],
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    tests_root = Path(__file__).resolve().parent.parent
    parser.add_argument("--base-repo", type=Path, default=Path("/opt/sharing-base"))
    parser.add_argument(
        "--solution-patch", type=Path,
        default=tests_root / "verifier" / "fixtures" / "official_reference.patch",
    )
    parser.add_argument("--tests-root", type=Path, default=tests_root)
    parser.add_argument("--work-root", type=Path, default=Path("/var/lib/harbor-evaluator"))
    parser.add_argument(
        "--if-stale", action="store_true",
        help="keep measured evidence when it already matches the current tests and gold patch",
    )
    args = parser.parse_args()
    tests_root = args.tests_root.resolve()
    solution_patch = args.solution_patch.resolve(strict=True)
    fixture_hashes = json.loads((tests_root / "verifier" / "fixtures" / "sha256.json").read_text(encoding="utf-8"))
    if solution_patch == (tests_root / "verifier" / "fixtures" / "official_reference.patch"):
        if digest(solution_patch) != fixture_hashes["official_reference.patch"]:
            raise ValueError("official reference patch hash does not match its fixture record")
    config = json.loads((tests_root / "config.json").read_text(encoding="utf-8"))
    test_patch = tests_root / "verifier" / "test.patch"
    if digest(test_patch) != config.get("test_patch_sha256"):
        raise ValueError("test.patch does not match config.json")
    node_ids = [
        *config["f2p"],
        *config["hard_gate_behavior"]["p2p"],
        *config["hard_gate_behavior"]["differential"],
    ]
    official_reference = (tests_root / "verifier" / "fixtures" / "official_reference.patch").resolve()
    if args.if_stale and solution_patch == official_reference:
        evidence = _load_baseline_evidence(config, tests_root, node_ids)
        if node_ids and all(item.get("provenance") == "current" for item in evidence.values()):
            print("baseline evidence already matches the current tests and gold patch", flush=True)
            return 0
    base = args.base_repo.resolve(strict=True)
    if not (base / "node_modules").is_dir():
        raise ValueError("base repository must have its locked dependencies installed")
    args.work_root.mkdir(parents=True, exist_ok=True)
    case_ids = _differential_case_ids(tests_root)
    with tempfile.TemporaryDirectory(prefix="baseline-refresh-", dir=args.work_root) as temporary:
        root = Path(temporary)
        workspace = root / "tests"
        prepared = _prepare_test_workspace(test_patch, workspace)
        if prepared.get("success") is not True:
            raise RuntimeError(f"cannot prepare current test suite: {prepared}")
        gold = root / "gold"
        shutil.copytree(base, gold, ignore=shutil.ignore_patterns(".git", "node_modules"))
        subprocess.run(["git", "apply", "--check", "--binary", str(solution_patch)], cwd=gold, check=True)
        subprocess.run(["git", "apply", "--binary", str(solution_patch)], cwd=gold, check=True)
        integrity = evaluate_test_integrity(gold, base_dir=base)
        if integrity.get("success") is not True:
            raise RuntimeError(f"reference test contract failed before baseline measurement: {integrity}")
        install_env = dict(os.environ, NPM_CONFIG_OFFLINE="true")
        subprocess.run(
            ["npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=gold, env=install_env, check=True,
        )
        evidence = {}
        for node_id in node_ids:
            base_run = measured_side(workspace, node_id, base, base)
            gold_run = measured_side(workspace, node_id, gold, base)
            f2p = node_id.startswith("f2p::")
            if f2p:
                if base_run["status"] != "candidate_failure" or base_run["exit_code"] == 0:
                    raise RuntimeError(f"F2P node did not fail on base: {node_id}: {base_run}")
            elif base_run["status"] != "passed" or base_run["exit_code"] != 0:
                raise RuntimeError(f"behavior node failed on base: {node_id}: {base_run}")
            if gold_run["status"] != "passed" or gold_run["exit_code"] != 0:
                raise RuntimeError(f"node failed on gold: {node_id}: {gold_run}")
            result_ids = case_ids if node_id.startswith("differential::") else [node_id.split("::", 1)[1]]
            item = {}
            for side, run in (("base", base_run), ("gold", gold_run)):
                passed = run["status"] == "passed"
                item.update({
                    f"{side}_status": "failed_as_expected" if side == "base" and f2p else run["status"],
                    f"{side}_passed": len(result_ids) if passed else 0,
                    f"{side}_total": len(result_ids),
                    f"{side}_exit_code": run["exit_code"],
                    f"{side}_command": run["command"],
                    f"{side}_results": [{"id": case_id, "pass": passed} for case_id in result_ids],
                })
            evidence[node_id] = item
            print(f"measured {node_id}: base={base_run['status']} gold={gold_run['status']}", flush=True)
    baseline = {
        "schema_version": "harbor-migration-baseline/v1",
        "task_id": config["task_id"],
        "base_commit": config["base_commit"],
        "base_tree_hash": config["base_tree_hash"],
        "test_patch_sha256": config["test_patch_sha256"],
        "gold_patch_sha256": digest(solution_patch),
        "evidence": evidence,
    }
    baseline_path = tests_root / "assets" / "baseline-node-results.json"
    manifest_path = tests_root / "assets" / "assets-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    encoded = (json.dumps(baseline, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest["assets"]["baseline_node_results"]["sha256"] = hashlib.sha256(encoded).hexdigest()
    baseline_path.write_bytes(encoded)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"updated {baseline_path} and {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
