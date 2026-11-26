from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from asset_integrity import verify_asset_manifest
from legacy_identity import (
    LEGACY_PACKAGES,
    is_production_source,
    legacy_loader_evidence,
    legacy_specifier_hits,
    load_lockfile_identity,
    production_sources,
)
from sandbox import (
    check_sandbox_tools,
    cleanup_candidate_workspace,
    network_isolated_command,
    prepare_candidate_workspace,
    run_candidate_command,
)


def run_node(
    repo: Path,
    script: Path,
    timeout: int = 360,
    env_extra: dict[str, str] | None = None,
    candidate_repo: Path | None = None,
) -> dict[str, Any]:
    """Run an evaluator-owned controller against a separate candidate copy."""
    env = dict(os.environ)
    env.update({"SHARING_REPO": str(candidate_repo or repo), "HARBOR_ISOLATED": "1"})
    if env_extra:
        env.update(env_extra)
    start_time = time.monotonic()
    command = ["node", str(script)]
    execution_command = network_isolated_command(command)
    try:
        result = subprocess.run(
            execution_command,
            cwd=repo,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        return {
            "status": "candidate_timeout",
            "success": False,
            "timed_out": True,
            "command": command,
            "execution_command": execution_command,
            "network_isolated": os.name != "nt",
            "exit_code": -1,
            "duration_ms": duration_ms,
            "error": str(exc),
            "stdout": "",
            "stderr": "",
        }
    except OSError as exc:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        return {
            "status": "infrastructure_error",
            "success": False,
            "timed_out": False,
            "command": command,
            "execution_command": execution_command,
            "network_isolated": os.name != "nt",
            "exit_code": -1,
            "duration_ms": duration_ms,
            "error": str(exc),
            "stdout": "",
            "stderr": "",
        }

    duration_ms = int((time.monotonic() - start_time) * 1000)
    try:
        display_script = script.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        display_script = script.name
    # Keep the complete bounded node report available to the summary parser.
    # Behavior suites emit one JSON result per case plus direct-regression
    # evidence; an 8 KiB tail can cut the opening of the canonical summary and
    # turn a valid candidate result into an infrastructure error.
    stdout = result.stdout[-50000:]
    stderr = result.stderr[-16000:]
    # Reports are portable artifacts. Do not retain the host checkout path in
    # command output or diagnostics.
    for absolute in (
        str(repo),
        str(repo.resolve()).replace("\\", "/"),
        *(
            [str(candidate_repo), str(candidate_repo.resolve()).replace("\\", "/")]
            if candidate_repo is not None
            else []
        ),
    ):
        stdout = stdout.replace(absolute, "<candidate-repo>")
        stderr = stderr.replace(absolute, "<candidate-repo>")
    sandbox_error = result.returncode != 0 and any(
        marker in stderr.lower()
        for marker in (
            "unshare failed",
            "operation not permitted",
            "spawn setpriv enoent",
            "spawn unshare enoent",
            "failed to mount /proc",
        )
    )
    return {
        "status": (
            "passed"
            if result.returncode == 0
            else ("infrastructure_error" if sandbox_error else "candidate_failure")
        ),
        "success": result.returncode == 0,
        "command": ["node", display_script],
        "execution_command": execution_command,
        "network_isolated": os.name != "nt",
        "exit_code": result.returncode,
        "timed_out": False,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "candidate_runtime": {
            "name": "node",
            "path": "/opt/node14/bin/node",
            "required_version": "v14.18.0",
        } if candidate_repo is not None else None,
    }


def scope_nodes(repo: Path, contract: dict[str, Any]) -> list[dict[str, Any]]:
    package_path = repo / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        package = {}

    deps = package.get("dependencies", {})
    # Check 1: fastify_dependencies
    has_fastify = "fastify" in deps and "@fastify/multipart" in deps
    has_legacy_express = any(k in deps for k in LEGACY_PACKAGES)
    alias_map: dict[str, str] = {}
    try:
        identity_findings, alias_map = load_lockfile_identity(repo)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        identity_findings = []
    if any(
        item.get("kind") in {"aliased_install", "alias_spec", "undeclared_legacy_install"}
        for item in identity_findings
    ):
        has_legacy_express = True
    fastify_dependencies_check = has_fastify and not has_legacy_express

    # Check 2: app_exports_contract
    export_probe = run_candidate_command(
        repo,
        [
            "node", "-e",
            "const app = require('./bin/app'); "
            "process.stdout.write('HARBOR_SCOPE_EXPORTS=' + JSON.stringify({"
            "start: typeof app.start === 'function', "
            "buildApp: typeof app.buildApp === 'function'"
            "}) + '\\n');",
        ],
        timeout=30,
    )
    app_exports_check = False
    if export_probe["status"] == "passed":
        export_lines = [
            line.removeprefix("HARBOR_SCOPE_EXPORTS=")
            for line in export_probe.get("stdout", "").splitlines()
            if line.startswith("HARBOR_SCOPE_EXPORTS=")
        ]
        if len(export_lines) == 1:
            try:
                exported = json.loads(export_lines[0])
            except json.JSONDecodeError:
                exported = {}
            app_exports_check = isinstance(exported, dict) and (
                exported.get("start") is True and exported.get("buildApp") is True
            )

    # Check 3: no_express_in_production
    bin_dir = repo / "bin"
    no_express_check = not any(
        item.get("kind") == "undeclared_legacy_install" for item in identity_findings
    )
    if bin_dir.is_dir():
        for path in production_sources(repo):
            if not is_production_source(path):
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if legacy_specifier_hits(content, alias_map) or legacy_loader_evidence(content, alias_map):
                no_express_check = False
                break
    else:
        no_express_check = False

    checks = {
        "fastify_dependencies": fastify_dependencies_check,
        "app_exports_contract": app_exports_check,
        "no_express_in_production": no_express_check,
    }

    nodes = []
    for unit in contract.get("migration_units", []):
        unit_id = unit.get("unit_id")
        if unit_id in checks:
            passed = bool(checks[unit_id])
            nodes.append({
                "node_id": f"scope::{unit_id}",
                "group": "migration_scope",
                "pass": passed,
                "status": "passed" if passed else "failed",
                "evidence": unit.get("verification"),
            })
    return nodes


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_test_workspace(test_patch: Path, workspace: Path) -> dict[str, Any]:
    """Apply the hidden test patch outside the candidate repository."""
    commands = [
        ["git", "init", "-q"],
        ["git", "apply", "--check", "--binary", str(test_patch)],
        ["git", "apply", "--binary", str(test_patch)],
    ]
    try:
        if workspace.exists():
            if not workspace.is_dir() or any(workspace.iterdir()):
                raise OSError("evaluator test workspace already exists and is not empty")
        else:
            workspace.mkdir(parents=True, mode=0o700, exist_ok=False)
        workspace.chmod(0o700)
    except OSError as exc:
        return {
            "success": False,
            "status": "infrastructure_error",
            "workspace": str(workspace),
            "commands": commands,
            "error": f"unable to create evaluator test workspace: {exc}",
        }

    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=60,
                check=False,
                env={
                    "PATH": os.environ.get(
                        "PATH",
                        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    ),
                    "HOME": "/root",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "success": False,
                "status": "infrastructure_error",
                "workspace": str(workspace),
                "commands": commands,
                "error": f"unable to apply evaluator test patch: {exc}",
            }
        if result.returncode != 0:
            return {
                "success": False,
                "status": "infrastructure_error",
                "workspace": str(workspace),
                "commands": commands,
                "exit_code": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
                "error": "evaluator test patch failed validation or application",
            }

    suite = workspace / "suite"
    try:
        files = [path for path in suite.rglob("*") if path.is_file()]
        if not files or any(path.is_symlink() for path in suite.rglob("*")):
            raise ValueError("test patch produced an empty suite or a symbolic link")
        if any(workspace not in path.resolve().parents for path in files):
            raise ValueError("test patch produced a file outside the evaluator workspace")
    except (OSError, ValueError) as exc:
        return {
            "success": False,
            "status": "infrastructure_error",
            "workspace": str(workspace),
            "commands": commands,
            "error": str(exc),
        }

    return {
        "success": True,
        "status": "passed",
        "workspace": str(workspace),
        "commands": commands,
        "file_count": len(files),
    }


def _canonical_controller_node(
    node_id: str,
    group: str,
    baseline_evidence: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    result_id = node_id.split("::", 1)[-1]
    passed = result.get("status") == "passed" and result.get("exit_code") == 0
    item = {
        "id": result_id,
        "pass": passed,
        "status": "passed" if passed else "failed",
        "exit_code": result.get("exit_code"),
    }
    return {
        "node_id": node_id,
        "group": group,
        "baseline_status": (
            baseline_evidence.get("base_status")
            if baseline_evidence.get("provenance") == "current" else "unverified"
        ),
        "baseline_evidence": baseline_evidence,
        "candidate_status": result.get("status", "infrastructure_error"),
        "transition": "F2P" if group == "f2p" else ("P2P" if group == "p2p" else "differential"),
        "failure_class": (
            None
            if passed
            else (
                "infrastructure_error"
                if result.get("status") == "infrastructure_error"
                else ("timeout" if result.get("timed_out") else "assertion_or_runtime")
            )
        ),
        "summary": {
            "total": 1,
            "passed": 1 if passed else 0,
            "failed": 0 if passed else 1,
            "results": [item],
        },
        "summary_error": result.get("error"),
        "duration_ms": result.get("duration_ms", 0),
        "command": result.get("command", []),
        "exit_code": result.get("exit_code"),
        "timed_out": bool(result.get("timed_out")),
        "pass": passed,
    }


def _differential_case_ids(tests_root: Path) -> list[str]:
    patch_text = (tests_root / "verifier" / "test.patch").read_text(encoding="utf-8")
    differential_patch = patch_text.split(
        "diff --git a/suite/differential/behavior-oracle.js b/suite/differential/behavior-oracle.js", 1
    )
    if len(differential_patch) != 2:
        raise ValueError("current test.patch has no differential behavior oracle")
    differential_patch = differential_patch[1].split("\ndiff --git ", 1)[0]
    ids = re.findall(r"^\+.*\bid:\s*['\"](C\d+)['\"]", differential_patch, re.MULTILINE)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("current differential suite has no unique case ids")
    return ids


def _load_baseline_evidence(config: dict[str, Any], tests_root: Path, node_ids: list[str]) -> dict[str, dict[str, Any]]:
    configured = Path(str(config.get("baseline_evidence", "")))
    candidates = [configured, tests_root / "assets" / "baseline-node-results.json"]
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        raise ValueError("baseline evidence asset is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("task_id") != config.get("task_id") or value.get("base_commit") != config.get("base_commit"):
        raise ValueError("baseline evidence identity does not match config.json")
    if value.get("base_tree_hash") != config.get("base_tree_hash"):
        raise ValueError("baseline evidence tree hash does not match config.json")
    gold_patch = tests_root / "verifier" / "fixtures" / "official_reference.patch"
    current_patch = value.get("test_patch_sha256") == config.get("test_patch_sha256")
    current_gold = gold_patch.is_file() and value.get("gold_patch_sha256") == _sha256(gold_patch)
    provenance = "current" if current_patch and current_gold else "stale"
    differential_case_ids = (
        _differential_case_ids(tests_root)
        if any(node_id.startswith("differential::") for node_id in node_ids) else []
    )
    evidence = value.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("baseline evidence must contain an evidence object")
    result: dict[str, dict[str, Any]] = {}
    for node_id in node_ids:
        item = evidence.get(node_id)
        if not isinstance(item, dict):
            raise ValueError(f"baseline evidence missing node: {node_id}")
        if node_id.startswith("f2p::"):
            # A migration F2P node must fail on the frozen base and pass on the
            # gold reference.
            expected = ("failed_as_expected", 0, 1, 1, 1, 1, 0)
            actual = (
                item.get("base_status"),
                item.get("base_passed"),
                item.get("base_total"),
                item.get("gold_passed"),
                item.get("gold_total"),
                item.get("base_exit_code"),
                item.get("gold_exit_code"),
            )
            if actual != expected or item.get("gold_status") != "passed":
                raise ValueError(f"invalid baseline/gold evidence for node: {node_id}")
        else:
            # Historical behavior evidence must be internally consistent.
            # A stale asset is labeled unverified and never determines the
            # candidate's current behavior result.
            if item.get("base_status") != "passed" or item.get("gold_status") != "passed":
                raise ValueError(f"invalid baseline/gold evidence for node: {node_id}")
            for side in ("base", "gold"):
                total = item.get(f"{side}_total")
                passed = item.get(f"{side}_passed")
                if (
                    not isinstance(total, int)
                    or isinstance(total, bool)
                    or total < 1
                    or passed != total
                    or item.get(f"{side}_exit_code") != 0
                ):
                    raise ValueError(f"invalid baseline/gold evidence for node: {node_id}")
        for side in ("base", "gold"):
            command = item.get(f"{side}_command")
            results = item.get(f"{side}_results")
            total = item[f"{side}_total"]
            passed = item[f"{side}_passed"]
            if not isinstance(command, list) or len(command) != 2 or command[0] != "node":
                raise ValueError(f"invalid {side} command evidence for node: {node_id}")
            if not isinstance(results, list) or len(results) != total:
                raise ValueError(f"invalid {side} per-result evidence for node: {node_id}")
            result_ids = [entry.get("id") for entry in results if isinstance(entry, dict)]
            if len(result_ids) != total or len(result_ids) != len(set(result_ids)):
                raise ValueError(f"invalid {side} result ids for node: {node_id}")
            if node_id.startswith("differential::") and provenance == "current" and result_ids != differential_case_ids:
                raise ValueError(f"baseline {side} differential cases differ from current test.patch")
            if sum(1 for entry in results if entry.get("pass") is True) != passed:
                raise ValueError(f"invalid {side} pass count for node: {node_id}")
        result[node_id] = {**item, "provenance": provenance}
    return result


def calculate_test_reward(completion_rates: dict[str, float | None]) -> float:
    applicable = [
        float(value)
        for value in completion_rates.values()
        if value is not None
    ]
    if not applicable:
        return 0.0
    if any(value < 0.0 or value > 1.0 for value in applicable):
        raise ValueError("completion rates must be within [0, 1]")
    return round(sum(applicable) / len(applicable), 6)


def calculate_verifier_score(
    scope_rate: float | None,
    f2p_rate: float | None,
) -> float | None:
    """Combine the migration-scope and F2P pass rates into the verifier score.

    ``None`` means the corresponding rate was not measured -- for example an
    evaluator infrastructure failure -- and the score stays unavailable.
    """
    if scope_rate is None or f2p_rate is None:
        return None
    for label, value in (("scope", scope_rate), ("f2p", f2p_rate)):
        if value < 0.0 or value > 1.0:
            raise ValueError(f"{label} pass rate must be within [0, 1]")
    return round(0.5 * scope_rate + 0.5 * f2p_rate, 6)


def calculate_regression_rate(f2p_nodes: list[dict[str, Any]]) -> float:
    """Compute the F2P regression completion from the executed behavior nodes.

    Only the migration-scope checks and the F2P nodes run in this verifier; the
    behavior-regression stage owns the remaining behavior nodes and reports
    them separately, so they must not dilute this denominator.
    """
    return (
        sum(1 for node in f2p_nodes if node["pass"]) / len(f2p_nodes)
        if f2p_nodes
        else 0.0
    )


# Ordered, first-match-wins mapping from a node's status/failure_class to its
# histogram bucket.  A rule matches when the lowercased status is in
# ``statuses`` or the lowercased failure_class is in ``failure_classes``; an
# empty set never matches on that field.  The infrastructure rule must stay
# ahead of the candidate-timeout rule so an evaluator/tool/platform failure
# always wins over a candidate-owned hang.
_NODE_STATUS_RULES: tuple[tuple[frozenset[str], frozenset[str], str], ...] = (
    (frozenset({"missing", "not_found"}), frozenset({"missing"}), "missing"),
    (frozenset({"skipped", "skip", "xfail", "excluded"}), frozenset(), "skipped"),
    (frozenset({"duplicate", "duplicated"}), frozenset({"duplicate"}), "duplicate"),
    (frozenset({"infrastructure_error", "error", "blocked"}), frozenset({"infrastructure_error", "evaluator_infrastructure"}), "error"),
    (frozenset({"candidate_timeout"}), frozenset({"timeout"}), "failed"),
)
_NODE_STATUS_DEFAULT_BUCKET = "failed"


def node_status_counts(nodes: list[dict[str, Any]]) -> dict[str, int]:
    """Return an auditable status histogram for the configured node set.

    ``passed`` counts nodes whose ``pass`` is exactly True.  Every other node is
    mapped by ``_NODE_STATUS_RULES`` (first match wins, default ``failed``):

    - a candidate-owned hang (``candidate_timeout``, or
      ``failure_class == "timeout"``) counts as ``failed``;
    - evaluator/tool/platform failures (``infrastructure_error``,
      ``evaluator_infrastructure``, ``error``, ``blocked``) count as ``error``
      so a platform outage never looks like a candidate failure.  The
      infrastructure rule deliberately precedes the candidate-timeout rule, so
      infrastructure always wins.

    The previous ``timeout``-in-error-set rule was removed deliberately: a
    ``failure_class`` of ``"timeout"`` is only ever produced together with
    ``candidate_status == "candidate_timeout"``, so that rule was unreachable
    and it contradicted the candidate-timeout rule on the meaning of timeout.

    The other buckets are explicit even when zero; this keeps CTRF and reward
    evidence schema-stable and exposes missing/skip/duplicate nodes instead of
    silently dropping them from the denominator.
    """
    counts = {
        "total": len(nodes),
        "passed": 0,
        "failed": 0,
        "missing": 0,
        "skipped": 0,
        "duplicate": 0,
        "error": 0,
    }
    for node in nodes:
        if node.get("pass") is True:
            counts["passed"] += 1
            continue
        status = str(node.get("candidate_status") or node.get("status") or "").lower()
        failure_class = str(node.get("failure_class") or "").lower()
        bucket = _NODE_STATUS_DEFAULT_BUCKET
        for statuses, failure_classes, rule_bucket in _NODE_STATUS_RULES:
            if status in statuses or failure_class in failure_classes:
                bucket = rule_bucket
                break
        counts[bucket] += 1
    return counts


def _infrastructure_verifier_report(
    error: str,
    *,
    f2p_ids: list[str],
    baseline_evidence: dict[str, dict[str, Any]],
    asset_report: dict[str, Any],
    test_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocked_f2p = [
        {
            "node_id": node_id,
            "group": "f2p",
            "baseline_status": (
                baseline_evidence[node_id]["base_status"]
                if baseline_evidence[node_id].get("provenance") == "current" else "unverified"
            ),
            "baseline_evidence": baseline_evidence[node_id],
            "candidate_status": "blocked",
            "blocked_by": "evaluator_infrastructure",
            "transition": "F2P",
            "failure_class": "evaluator_infrastructure",
            "summary": {},
            "summary_error": error,
            "duration_ms": 0,
            "evidence_path": None,
            "pass": False,
        }
        for node_id in f2p_ids
    ]
    blocked_scope = {
        "node_id": "migration_scope_complete",
        "group": "migration_scope",
        "candidate_status": "blocked",
        "blocked_by": "evaluator_infrastructure",
        "baseline_status": "not_applicable",
        "transition": "scope",
        "failure_class": "evaluator_infrastructure",
        "summary": {},
        "summary_error": error,
        "duration_ms": 0,
        "evidence_path": None,
        "pass": False,
    }
    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "verifier",
        "success": False,
        "status": "infrastructure_error",
        "test_reward": None,
        "score": None,
        "completion_rates": {"scope": None, "regression": None, "state": None},
        "migration_scope_complete": {"status": "not_measured", "nodes": []},
        "required_state_preservation_complete": {
            "status": "na",
            "reason": "No persistent state is involved.",
        },
        "nodes": [blocked_scope, *blocked_f2p],
        "error": error,
        "asset_integrity": asset_report,
        "test_patch": test_patch,
        "scope": {
            "results": [],
            "passed": 0,
            "total": 0,
            "pass_rate": None,
            "complete": False,
        },
        "f2p": {
            "results": blocked_f2p,
            "passed": 0,
            "total": len(f2p_ids),
            "pass_rate": None,
            "status_counts": node_status_counts(blocked_f2p),
        },
    }


def evaluate(repo: Path, config_path: Path, tests_root: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    task_id = config.get("task_id", "sharing-express-to-fastify")

    asset_report = verify_asset_manifest(tests_root / "assets")
    if asset_report.get("success") is not True:
        return {
            "schema_version": "harbor-migration-check/v1",
            "id": "verifier",
            "success": False,
            "status": "configuration_error",
            "test_reward": None,
            "score": None,
            "completion_rates": {"scope": None, "regression": None, "state": None},
            "migration_scope_complete": {"status": "not_measured", "nodes": []},
            "required_state_preservation_complete": {"status": "na", "reason": "No persistent state is involved."},
            "nodes": [],
            "asset_integrity": asset_report,
            "scope": {
                "results": [],
                "passed": 0,
                "total": 0,
                "pass_rate": None,
                "complete": False,
            },
            "f2p": {"results": [], "passed": 0, "total": 0, "pass_rate": None},
        }

    contract_path = Path(config.get("migration_contract", str(tests_root / "assets" / "migration-contract.json")))
    if not contract_path.is_file():
        # Fallback if config has container absolute path /tests/...
        contract_path = tests_root / "assets" / "migration-contract.json"
    if not contract_path.is_file():
        # Fallback to dev staging migration-units.json
        dev_contract = tests_root.parent.parent.parent / "build-reports" / "sharing-express-to-fastify" / "eval-tests" / "migration-units.json"
        if dev_contract.is_file():
            contract_path = dev_contract

    contract = json.loads(contract_path.read_text(encoding="utf-8")) if contract_path.is_file() else {}
    f2p_ids = config.get("f2p")
    if not isinstance(f2p_ids, list) or any(
        not isinstance(node_id, str) or not node_id for node_id in f2p_ids
    ):
        raise ValueError("config.json f2p must be a node-name array")
    if not f2p_ids:
        raise ValueError("config.json must freeze at least one F2P node")
    if any(not node_id.startswith("f2p::") for node_id in f2p_ids):
        raise ValueError("all F2P node ids must start with f2p::")
    if len(f2p_ids) != len(set(f2p_ids)):
        raise ValueError("config.json contains duplicate execution nodes")

    baseline_evidence = _load_baseline_evidence(config, tests_root, f2p_ids)
    required_nodes = config.get("verifier", {}).get("required_nodes", [])
    expected_order = ["migration_scope_complete", *f2p_ids]
    if required_nodes != expected_order:
        raise ValueError(
            "verifier.required_nodes must exactly match the migration scope and F2P node order"
        )

    patch_config = Path(str(config.get("test_patch", "")))
    test_patch = patch_config if patch_config.is_file() else tests_root / "verifier" / "test.patch"
    expected_patch_sha = str(config.get("test_patch_sha256", ""))
    patch_report: dict[str, Any] = {
        "path": "verifier/test.patch",
        "expected_sha256": expected_patch_sha,
    }
    if not test_patch.is_file():
        patch_report.update({"status": "infrastructure_error", "error": "tests/verifier/test.patch is missing"})
        return _infrastructure_verifier_report(
            patch_report["error"],
            f2p_ids=f2p_ids,
            baseline_evidence=baseline_evidence,
            asset_report=asset_report,
            test_patch=patch_report,
        )
    actual_patch_sha = _sha256(test_patch)
    patch_report["actual_sha256"] = actual_patch_sha
    if not re.fullmatch(r"[0-9a-f]{64}", expected_patch_sha) or actual_patch_sha != expected_patch_sha:
        patch_report.update({"status": "infrastructure_error", "error": "tests/verifier/test.patch SHA-256 mismatch"})
        return _infrastructure_verifier_report(
            patch_report["error"],
            f2p_ids=f2p_ids,
            baseline_evidence=baseline_evidence,
            asset_report=asset_report,
            test_patch=patch_report,
        )

    sandbox_tools = check_sandbox_tools()
    if sandbox_tools["success"] is not True:
        return _infrastructure_verifier_report(
            sandbox_tools["summary"],
            f2p_ids=f2p_ids,
            baseline_evidence=baseline_evidence,
            asset_report=asset_report,
            test_patch=patch_report,
        )

    evaluator_root = Path("/var/lib/harbor-evaluator")
    workspace: Path | None = None
    try:
        evaluator_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        evaluator_root.chmod(0o700)
        workspace = Path(tempfile.mkdtemp(prefix=f"{task_id}-", dir=evaluator_root))
        workspace.chmod(0o700)
    except OSError as exc:
        return _infrastructure_verifier_report(
            f"unable to allocate root-only evaluator workspace: {exc}",
            f2p_ids=f2p_ids,
            baseline_evidence=baseline_evidence,
            asset_report=asset_report,
            test_patch=patch_report,
        )

    prepared = _prepare_test_workspace(test_patch, workspace)
    patch_report.update(prepared)
    patch_report.pop("workspace", None)
    if prepared["success"] is not True:
        shutil.rmtree(workspace, ignore_errors=True)
        return _infrastructure_verifier_report(
            str(prepared.get("error", "unable to prepare evaluator tests")),
            f2p_ids=f2p_ids,
            baseline_evidence=baseline_evidence,
            asset_report=asset_report,
            test_patch=patch_report,
        )

    node_specs: list[dict[str, Any]] = []
    for node_id in f2p_ids:
        group, file_stem = node_id.split("::", 1)
        script_name = f"{file_stem}.js"
        node_specs.append({
            "node_id": node_id,
            "group": group,
            "script": workspace / "suite" / group / script_name,
            "script_display": f"suite/{group}/{script_name}",
            "env": {},
        })
    expected_files = {"suite/_harness.js", *(spec["script_display"] for spec in node_specs)}
    expected_group_files = {
        spec["script_display"] for spec in node_specs
    }
    actual_files = {
        path.relative_to(workspace).as_posix()
        for path in (workspace / "suite").rglob("*.js")
        if path.is_file()
    }
    actual_group_files = {
        path for path in actual_files if path.startswith("suite/f2p/")
    }
    missing_files = sorted(expected_files - actual_files)
    if missing_files or actual_group_files != expected_group_files:
        shutil.rmtree(workspace, ignore_errors=True)
        detail = (
            f"missing files: {missing_files}"
            if missing_files
            else f"unexpected F2P files: {sorted(actual_group_files - expected_group_files)}"
        )
        return _infrastructure_verifier_report(
            f"test.patch F2P suite does not match the configured nodes ({detail})",
            f2p_ids=f2p_ids,
            baseline_evidence=baseline_evidence,
            asset_report=asset_report,
            test_patch=patch_report,
        )

    scope = scope_nodes(repo, contract)
    scope_passed = all(node["pass"] for node in scope) and bool(scope)
    reports_dir = Path("/logs/verifier/reports")
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        return _infrastructure_verifier_report(
            f"unable to create evaluator node evidence directory: {exc}",
            f2p_ids=f2p_ids,
            baseline_evidence=baseline_evidence,
            asset_report=asset_report,
            test_patch=patch_report,
        )

    executed_nodes = []
    node_evidence: list[dict[str, Any]] = []
    try:
        for spec in node_specs:
            candidate_scratch: Path | None = None
            try:
                candidate_scratch, candidate_workspace = prepare_candidate_workspace(repo)
                result = run_node(
                    workspace,
                    spec["script"],
                    timeout=180,
                    env_extra=spec["env"],
                    candidate_repo=candidate_workspace,
                )
            finally:
                if candidate_scratch is not None:
                    cleanup_candidate_workspace(candidate_scratch)
            result["command"] = ["node", spec["script_display"]]
            result["workspace"] = {
                "kind": "fresh_independent_writable_copy_per_node",
                "git_metadata_included": False,
                "dedicated_home_tmp_cache": True,
            }
            node_evidence.append(
                {"node_id": spec["node_id"], "script": spec["script_display"], **result}
            )
            node = _canonical_controller_node(
                spec["node_id"],
                spec["group"],
                baseline_evidence[spec["node_id"]],
                result,
            )
            node["evidence_path"] = "reports/nodes.json"
            executed_nodes.append(node)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    try:
        (reports_dir / "nodes.json").write_text(
            json.dumps(
                {
                    "schema_version": "harbor-migration-node-report/v1",
                    "suite": str(config.get("suite", "")),
                    "tests": node_evidence,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        for node in executed_nodes:
            node.update({
                "candidate_status": "infrastructure_error",
                "pass": False,
                "failure_class": "infrastructure_error",
                "summary_error": f"unable to write canonical node evidence: {exc}",
            })

    f2p_nodes = [node for node in executed_nodes if node["group"] == "f2p"]

    def group_result(nodes: list[dict[str, Any]]) -> dict[str, Any]:
        passed = sum(1 for node in nodes if node["pass"])
        total = len(nodes)
        return {
            "results": nodes,
            "passed": passed,
            "total": total,
            "pass_rate": passed / total if total else None,
            "status_counts": node_status_counts(nodes),
        }

    f2p_result = group_result(f2p_nodes)
    all_nodes_passed = all(node["pass"] for node in executed_nodes)
    has_infra = any(node["candidate_status"] == "infrastructure_error" for node in executed_nodes)
    overall_passed = bool(scope_passed and all_nodes_passed and not has_infra)
    status = "infrastructure_error" if has_infra else ("passed" if overall_passed else "candidate_failure")
    scope_passed_count = sum(1 for node in scope if node["pass"])
    scope_rate = scope_passed_count / len(scope) if scope else 0.0
    scope_pass_rate = scope_rate if scope else None
    regression_rate = calculate_regression_rate(f2p_nodes)
    completion_rates = {"scope": scope_rate, "regression": regression_rate, "state": None}
    test_reward = None if has_infra else calculate_test_reward(completion_rates)
    score = (
        None
        if has_infra
        else calculate_verifier_score(scope_pass_rate, f2p_result["pass_rate"])
    )

    scope_node = {
        "node_id": "migration_scope_complete",
        "group": "migration_scope",
        "candidate_status": "passed" if scope_passed else "candidate_failure",
        "baseline_status": "not_applicable",
        "transition": "scope",
        "failure_class": None if scope_passed else "scope_incomplete",
        "summary": {
            "total": len(scope),
            "passed": scope_passed_count,
            "failed": sum(1 for node in scope if not node["pass"]),
            "results": scope,
        },
        "summary_error": None,
        "duration_ms": 0,
        "evidence_path": None,
        "pass": scope_passed,
    }

    return {
        "schema_version": "harbor-migration-check/v1",
        "id": "verifier",
        "success": overall_passed,
        "status": status,
        "test_reward": test_reward,
        "score": score,
        "completion_rates": completion_rates,
        "migration_scope_complete": {
            "status": "passed" if scope_passed else "failed",
            "nodes": scope,
        },
        "required_state_preservation_complete": {
            "status": "na",
            "reason": "No persistent state is involved.",
        },
        "asset_integrity": asset_report,
        "test_patch": patch_report,
        "nodes": [scope_node, *executed_nodes],
        "scope": {
            "results": scope,
            "passed": scope_passed_count,
            "total": len(scope),
            "pass_rate": scope_pass_rate,
            "complete": scope_passed,
        },
        "f2p": f2p_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    try:
        report = evaluate(args.repo.resolve(), args.config.resolve(), args.config.parent)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": "harbor-migration-check/v1",
            "id": "verifier",
            "success": False,
            "status": "configuration_error",
            "test_reward": None,
            "score": None,
            "completion_rates": {"scope": None, "regression": None, "state": None},
            "migration_scope_complete": {"status": "failed", "nodes": []},
            "required_state_preservation_complete": {"status": "na", "reason": "No persistent state is involved."},
            "nodes": [],
            "error": str(exc),
            "scope": {
                "results": [],
                "passed": 0,
                "total": 0,
                "pass_rate": None,
                "complete": False,
            },
            "f2p": {"results": [], "passed": 0, "total": 0, "pass_rate": None},
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
