#!/usr/bin/env python3
"""Task-specific verifier for the JsSIP PEG.js -> Peggy migration.

Runs the composite hard gates, the hidden P2P/F2P/differential harness, and the
``migration_scope_complete`` coverage over the declared migration units.  It
writes:

* the CTRF report for the required nodes, and
* ``verifier-result.json`` with the verifier components and completion rates.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from sandbox import SandboxUnavailable, run as sandbox_run

HARNESS_RELATIVE = "src/test/migration-verifier/run-checks.cjs"
_VALID_STATUSES = {"passed", "failed", "skipped", "pending", "other"}


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_hardgate_module() -> Any:
    path = Path(__file__).resolve().parent / "hardgate.py"
    spec = importlib.util.spec_from_file_location("jssip_hardgate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _scope_checks(repo: Path) -> dict[str, tuple[str, str]]:
    hardgate = _load_hardgate_module()
    return {
        "manifest": hardgate._manifest_dependency_check(repo),
        "lockfile": hardgate._lockfile_check(repo),
        "generator": hardgate._generator_check(repo),
        "grammar": hardgate._grammar_check(repo),
        "residual": hardgate._residual_scan(repo),
    }


def _migration_scope_complete(config: dict, repo: Path) -> dict[str, Any]:
    units = config.get("migration_units") or []
    if not units:
        return {"status": "na", "ratio": None, "passed": 0, "total": 0, "nodes": []}
    checks = _scope_checks(repo)
    nodes: list[dict[str, Any]] = []
    passed = 0
    for unit in units:
        check = str(unit.get("check", ""))
        status, message = checks.get(check, ("failed", "unknown migration unit check"))
        ok = status == "passed"
        passed += int(ok)
        nodes.append(
            {
                "node_id": f"scope::{unit.get('id')}",
                "unit_id": unit.get("id"),
                "type": unit.get("type"),
                "check": check,
                "pass": ok,
                "message": message,
            }
        )
    total = len(units)
    return {
        "status": "passed" if passed == total else "failed",
        "ratio": passed / total,
        "passed": passed,
        "total": total,
        "nodes": nodes,
    }


def _hidden_results(
    config: dict,
    repo: Path,
    *,
    reference: Path | None = None,
    expected: Path | None = None,
    hidden_unavailable: bool = False,
    hidden_blocked: str | None = None,
) -> dict[str, tuple[str, str]]:
    required = [str(node) for node in config.get("f2p", [])]
    required += [str(node) for node in config.get("p2p", [])]
    required += [str(node) for node in config.get("differential", [])]
    results: dict[str, tuple[str, str]] = {}

    def fail_all(message: str) -> dict[str, tuple[str, str]]:
        return {node: ("failed", message[-2000:]) for node in required}

    if hidden_blocked:
        return fail_all(hidden_blocked)
    if hidden_unavailable:
        return fail_all(
            "hidden migration checks were not executed because a protected "
            "path collided with the evaluator payload"
        )
    harness = repo / HARNESS_RELATIVE
    if not harness.is_file():
        message = f"hidden migration checks are missing: {HARNESS_RELATIVE}"
        return {node: ("failed", message) for node in required}

    here = Path(__file__).resolve().parent
    reference = reference or (here / "reference-grammar.js")
    expected = expected or (here / "expected.json")
    try:
        reference_source = reference.read_text(encoding="utf-8")
        expected_values = json.loads(expected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"hidden evaluator answer material is unreadable: {exc}") from exc
    if not isinstance(expected_values, dict):
        raise RuntimeError("hidden expected-value payload is not a JSON object")

    nonce = secrets.token_hex(16)
    payload = json.dumps(
        {
            "reference": reference_source,
            "expected": expected_values,
            "nonce": nonce,
        },
        ensure_ascii=False,
    )

    expected_groups: dict[str, str] = {}
    for node in config.get("f2p", []):
        expected_groups[str(node)] = "F2P"
    for node in config.get("p2p", []):
        expected_groups[str(node)] = "P2P"
    for node in config.get("differential", []):
        expected_groups[str(node)] = "differential"
    required_set = set(expected_groups)

    # The result file is opened by the root verifier before the child drops to
    # UID 1001.  The child can write through the inherited descriptor but the
    # directory is not traversable by candidate code.  The nonce is delivered
    # on stdin (consumed before candidate modules load), so an attacker can
    # neither read nor forge the authenticated envelope.
    with tempfile.TemporaryDirectory(prefix="jssip-hidden-result-") as result_dir:
        result_path = Path(result_dir) / "result.json"
        result_fd = os.open(result_path, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
        command = [
            "node",
            str(harness),
            f"--repo={repo}",
            f"--result-fd={result_fd}",
        ]
        try:
            completed = sandbox_run(
                command,
                cwd=repo,
                timeout=1800,
                input_data=payload,
                pass_fds=(result_fd,),
            )
        except SandboxUnavailable as exc:
            raise RuntimeError(
                f"cannot execute hidden migration checks in isolation: {exc}"
            ) from exc
        finally:
            try:
                os.lseek(result_fd, 0, os.SEEK_SET)
                raw_envelope = os.read(result_fd, 8 * 1024 * 1024)
            except OSError:
                raw_envelope = b""
            finally:
                os.close(result_fd)

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "hidden checks crashed").strip()
        return fail_all(message or "hidden checks exited non-zero")
    text_envelope = raw_envelope.decode("utf-8", errors="replace").strip()
    parsed = None
    for candidate in [text_envelope, *reversed(text_envelope.splitlines())]:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("version") == "deepswe-hidden/v1"
            and value.get("nonce") == nonce
        ):
            parsed = value
            break
    if not isinstance(parsed, dict):
        return fail_all(
            "hidden checks did not emit an authenticated result envelope"
        )
    if parsed.get("complete") is not True:
        return fail_all("hidden checks did not report completion")
    raw_entries = parsed.get("results")
    if not isinstance(raw_entries, list):
        return fail_all("hidden checks result envelope has no results array")
    entries = [item for item in raw_entries if isinstance(item, dict)]
    if len(entries) != len(raw_entries) or len(entries) != len(required_set):
        return fail_all(
            f"hidden checks returned {len(entries)} entries for "
            f"{len(required_set)} required nodes"
        )
    seen: set[str] = set()
    for entry in entries:
        node = str(entry.get("testId") or "")
        if node in seen:
            return fail_all(f"hidden checks returned duplicate node {node!r}")
        seen.add(node)
        if node not in expected_groups:
            return fail_all(f"hidden checks returned unexpected node {node!r}")
        group = str(entry.get("group") or "")
        if group != expected_groups[node]:
            return fail_all(
                f"hidden checks returned node {node!r} in group {group!r}, "
                f"expected {expected_groups[node]!r}"
            )
        status = str(entry.get("status", "failed"))
        if status not in _VALID_STATUSES:
            status = "failed"
        results[node] = (status, str(entry.get("message") or "")[:2000])
    if seen != required_set:
        missing = sorted(required_set - seen)
        return fail_all("required nodes missing from hidden checks: " + ", ".join(missing[:5]))
    return results


def _build_ctrf(config: dict, results: dict[str, tuple[str, str]], scope: dict[str, Any]) -> dict:
    f2p = [str(node) for node in config.get("f2p", [])]
    p2p = [str(node) for node in config.get("p2p", [])]
    differential = [str(node) for node in config.get("differential", [])]
    groups: dict[str, str] = {}
    for node in f2p:
        groups[node] = "F2P"
    for node in p2p:
        groups[node] = "P2P"
    for node in differential:
        groups[node] = "differential"

    task_id = str(config.get("task_id", ""))
    base_commit = str(config.get("base_commit", ""))
    tests: list[dict] = []
    for node, group in groups.items():
        status, message = results.get(node, ("failed", "missing"))
        if status not in _VALID_STATUSES:
            status = "failed"
        tests.append(
            {
                "name": node,
                "testId": node,
                "status": status,
                "duration": 0,
                "message": message,
                "extra": {
                    "deepswe": {
                        "group": group,
                        "required": True,
                        "taskId": task_id,
                        "baseCommit": base_commit,
                    }
                },
            }
        )
    summary = {
        "tests": len(tests),
        "passed": sum(item["status"] == "passed" for item in tests),
        "failed": sum(item["status"] == "failed" for item in tests),
        "skipped": sum(item["status"] == "skipped" for item in tests),
        "pending": sum(item["status"] == "pending" for item in tests),
        "other": sum(item["status"] == "other" for item in tests),
    }
    return {
        "reportFormat": "CTRF",
        "specVersion": "0.0.0",
        "results": {
            "tool": {"name": "deepswe-migration-verifier", "version": "2"},
            "summary": summary,
            "tests": tests,
            "environment": {"taskId": task_id, "baseCommit": base_commit, "suite": config.get("suite", "")},
        },
        "migration_scope_complete": scope,
    }


def _section(config: dict, results: dict[str, tuple[str, str]], group: str) -> dict[str, Any]:
    nodes = [str(node) for node in config.get(group, [])]
    passed = sum(results.get(node, ("failed", ""))[0] == "passed" for node in nodes)
    total = len(nodes)
    return {
        "passed": passed,
        "total": total,
        "ratio": passed / total if total else 1.0,
        "complete": passed == total,
        "results": [
            {
                "node_id": node,
                "pass": results.get(node, ("failed", ""))[0] == "passed",
                "message": results.get(node, ("failed", ""))[1],
            }
            for node in nodes
        ],
    }


def _combine_sections(*sections: dict[str, Any]) -> dict[str, Any]:
    """Merge behavioral sections without double-counting node ids."""
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for section in sections:
        for result in section.get("results") or []:
            node = str(result.get("node_id"))
            if node in seen:
                continue
            seen.add(node)
            results.append(result)
    passed = sum(bool(result.get("pass")) for result in results)
    total = len(results)
    return {
        "passed": passed,
        "total": total,
        "ratio": passed / total if total else 1.0,
        "complete": passed == total,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--eval-repo", type=Path, default=None)
    parser.add_argument("--reference", type=Path, default=None)
    parser.add_argument("--expected", type=Path, default=None)
    parser.add_argument("--ctrf-output", type=Path, required=True)
    parser.add_argument("--hard-gate-output", type=Path, required=True)
    parser.add_argument("--apply-failed", type=int, choices=(0, 1), default=0)
    parser.add_argument("--hidden-unavailable", type=int, choices=(0, 1), default=0)
    args = parser.parse_args()

    config = _read_json(args.config)
    repo = args.repo.resolve()
    eval_repo = (args.eval_repo or args.repo).resolve()

    # Static fallback evidence can be established before executing any
    # candidate module.  Skip the hidden run in that case: executing a known
    # subprocess/VM/answer-reading loader only wastes time (and can turn a
    # deterministic L2 failure into a timeout).  The hard gate still performs
    # the authoritative scan and records the cheating verdict.
    hidden_blocked = None
    if not args.hidden_unavailable and not args.apply_failed:
        preflight_status, preflight_message = _load_hardgate_module()._hidden_fallback(repo)
        if preflight_status == "failed":
            hidden_blocked = "hidden migration checks were not executed: " + preflight_message

    # Run the hidden regression harness first so the P2P results can be fed to
    # the hard gate: P2P (pass-to-pass behavior preservation) is a hard gate,
    # not a graded component.  The hidden harness executes in a separate
    # evaluator workspace where test.patch has been applied; the canonical
    # candidate repository is never contaminated with hidden tests.
    results = _hidden_results(
        config,
        eval_repo,
        reference=args.reference,
        expected=args.expected,
        hidden_unavailable=bool(args.hidden_unavailable),
        hidden_blocked=hidden_blocked,
    )
    scope = _migration_scope_complete(config, repo)
    ctrf = _build_ctrf(config, results, scope)
    _write_json(args.ctrf_output, ctrf)

    hardgate = Path(__file__).resolve().parent / "hardgate.py"
    completed = _run(
        [
            sys.executable,
            str(hardgate),
            "--config", str(args.config),
            "--repo", str(repo),
            "--eval-repo", str(eval_repo),
            "--output", str(args.hard_gate_output),
            "--apply-failed", str(args.apply_failed),
            "--ctrf", str(args.ctrf_output),
        ]
    )
    if completed.returncode != 0:
        raise RuntimeError("hard gate crashed: " + (completed.stderr or completed.stdout or "")[-2000:])
    hard_gate = _read_json(args.hard_gate_output) if args.hard_gate_output.is_file() else {}
    hard_gate_passed = hard_gate.get("passed") is True and not args.apply_failed

    f2p = _section(config, results, "f2p")
    p2p = _section(config, results, "p2p")
    differential = _section(config, results, "differential")
    behavior = _combine_sections(p2p, differential)
    # P2P (including the differential behavior probe) is enforced by the hard
    # gate and must not enter T.  Only F2P contributes to the verifier score.
    regression = f2p

    persistent_state = bool(config.get("persistent_state_involved", False))
    if persistent_state:
        state = {"status": "passed", "ratio": 1.0}
    else:
        state = {"status": "na", "ratio": None, "reason": "No persistent state is involved."}

    completion_rates = {
        "scope": scope["ratio"],
        "f2p": f2p["ratio"],
        "state": state["ratio"],
    }
    applicable_rates = [rate for rate in completion_rates.values() if rate is not None]
    test_reward = sum(applicable_rates) / len(applicable_rates) if applicable_rates else 0.0

    success = (
        hard_gate_passed
        and scope["status"] in {"passed", "na"}
        and f2p["complete"]
        and state["status"] in {"passed", "na"}
    )

    summary = {
        "schema_version": "deepswe-verifier-result/v2",
        "task_id": config.get("task_id", ""),
        "status": "passed" if success else "failed",
        "success": bool(success),
        "hard_gate_passed": bool(hard_gate_passed),
        "migration_scope_complete": scope,
        "f2p": f2p,
        "p2p": behavior,
        "differential": differential,
        "regression": regression,
        "required_state_preservation_complete": state,
        "completion_rates": completion_rates,
        "p2p_pass_ratio": behavior["ratio"],
        "test_reward": test_reward,
    }
    verifier_output = (config.get("diagnostics") or {}).get("verifier")
    if verifier_output:
        _write_json(Path(verifier_output), summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
