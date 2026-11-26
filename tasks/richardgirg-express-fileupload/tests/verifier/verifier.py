#!/usr/bin/env python3
"""Completion verifier for the express-fileupload -> Koa 2 migration task.

The verifier answers exactly one question: did the candidate complete the
declared migration? It runs the hidden F2P payload node by node and aggregates
through the declared migration units. ``success`` is derived from real per-node
outcomes only.

It carries three components, each normalized to ``[0, 1]``:
``migration_scope_complete`` (unit coverage), ``f2p`` (target-technology pass
ratio) and the conditional ``required_state_preservation_complete``. ``T`` is
their weighted average over the applicable components (see ``verifier_scores``
/ ``weighted_t``), and the grader pays ``T_weight * T`` when the verifier fails.

P2P is *not* a verifier component: existing-behaviour preservation is a boolean
hard-gate atom (``p2p_regression_preserved.p2p_no_regression``) and never enters
``T`` or any other score. The per-node P2P outcome is still copied into the CTRF
from the gate report, because that report is the platform's single identity
mapping for ``[metadata.tests].pass_to_pass_count``.

Exit codes:
    0  verifier completed (inspect verifier-result.json -> success / status)
    2  infrastructure error (the run cannot be interpreted as candidate signal)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox  # noqa: E402  (local verifier helper)
import semantic_scan  # noqa: E402  (shared with hardgate.py; single source of truth)

SCHEMA_VERSION = "migration-verifier-result/v1"
CTRF_SCHEMA = "migration-ctrf/v1"
NODE_REPORT_SCHEMA = "migration-node-report/v1"
F2P_PAYLOAD = "test/_koa_migration_cases.js"

# ``migration_scope_complete`` is not just unit coverage: a candidate can keep
# every unit green while leaving an unexecuted source-framework reference in the
# migrated tree ("looks migrated, still carries the old stack"). Actual source
# module references are residue; strings identifying the required Express call
# mode, documentation and URLs are not evidence of a source-runtime dependency.
LEGACY_SOURCE_RESIDUE_ALLOW = re.compile(
    r"""\bmode\b\s*(?:===|!==|==|!=|:)\s*['"]express['"]"""
)
LEGACY_SOURCE_RESIDUE_SKIP = ("github.com", "http://", "https://")

# ``required_state_preservation_complete`` is conditional, and the condition is a
# *declaration*: only an explicit JSON boolean settles it. Anything else (field
# omitted, string, null, non-boolean) leaves applicability undetermined and must
# never be reported as ``na`` -- an undecidable declaration has to block the run
# instead of silently dropping the component out of ``T``.
STATE_DECLARATION_KEY = "persistent_state_involved"


def sh(cmd: Sequence[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None,
       timeout: int = 120) -> Dict[str, Any]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    if sandbox.is_candidate_command(cmd):
        merged = sandbox.candidate_env(merged, extra=env)
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=merged,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return {
            "cmd": list(cmd),
            "returncode": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", "replace"),
            "stderr": proc.stderr.decode("utf-8", "replace"),
            "timed_out": False,
        }
    except FileNotFoundError as err:
        return {"cmd": list(cmd), "returncode": None, "stdout": "", "stderr": str(err), "timed_out": False, "missing": True}
    except subprocess.TimeoutExpired as err:
        return {
            "cmd": list(cmd),
            "returncode": None,
            "stdout": (err.stdout or b"").decode("utf-8", "replace"),
            "stderr": (err.stderr or b"").decode("utf-8", "replace"),
            "timed_out": True,
        }


def tail(text: str, limit: int = 1200) -> str:
    return (text or "")[-limit:]


def normalize_title(title: str) -> str:
    """Canonical node-id normalization; must stay in sync with
    authoring/generate_verifier_manifests.py."""
    return re.sub(r"[^0-9A-Za-z]+", "_", title).strip("_").lower()


def assign_ids(records: Sequence[Dict[str, Any]]) -> List[Tuple[str, Dict[str, Any]]]:
    from collections import Counter
    base_ids = [normalize_title(record.get("fullTitle") or record.get("full_title") or "") for record in records]
    counts = Counter(base_ids)
    seen: Counter = Counter()
    assigned: List[Tuple[str, Dict[str, Any]]] = []
    for base_id, record in zip(base_ids, records):
        seen[base_id] += 1
        node_id = base_id if counts[base_id] == 1 else f"{base_id}#{seen[base_id]}"
        assigned.append((node_id, record))
    return assigned


class Verifier:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo = Path(args.repo).resolve()
        self.tests_dir = Path(args.tests_dir).resolve() if args.tests_dir else Path(__file__).resolve().parent.parent
        self.node_bin = args.node or "node"
        self.config: Dict[str, Any] = {}
        self.f2p: List[str] = []
        self.p2p: List[str] = []
        self.state_nodes: List[str] = []
        self.units: List[Dict[str, Any]] = []
        self.statuses: Dict[str, Dict[str, Any]] = {}
        self.hard_gate_doc: Dict[str, Any] = {}
        self.scope_residue: List[Dict[str, Any]] = []
        # Deliberately hidden (indirect/obfuscated) source-framework references.
        # Kept apart from ``scope_residue`` so a completeness gap and a
        # deceptive fallback are never reported as the same failure (QC 114).
        self.deceptive_residue: List[Dict[str, Any]] = []
        # Frozen instruction.md provenance recorded by load_configuration.
        self.instruction_alignment: Dict[str, Any] = {}
        self.infra_error: Optional[str] = None
        self.infra_details: Dict[str, Any] = {}
        # Set when the verifier does not reach its behavioural nodes because an
        # upstream gate (hard gate failed) or the sandbox made them
        # unevaluable. Recorded explicitly so "not evaluated" is never mistaken
        # for "evaluated and failed" downstream.
        self.short_circuited = False
        self.short_circuit_reason: Optional[str] = None
        # P2P is a hard-gate atom and the gate never re-runs a failed node, so
        # this is always empty; it is kept in the report schema (and set
        # explicitly rather than via ``getattr``) so the field can never be
        # mistaken for "some failures were quarantined away".
        self.quarantined: List[str] = []
        self.timings: Dict[str, Any] = {}
        self.start_ts = 0
        self.notes: List[str] = []

    # -- infrastructure -----------------------------------------------------
    def set_infra(self, message: str, **details: Any) -> None:
        if self.infra_error is None:
            self.infra_error = message
        if details:
            self.infra_details.setdefault("events", []).append({"message": message, **details})
        self.notes.append(f"INFRA_ERROR: {message}")

    def node_env(self) -> Dict[str, str]:
        parts: List[str] = []
        deps = self.tests_dir / "deps" / "node_modules"
        if deps.is_dir():
            parts.append(str(deps))
        repo_modules = self.repo / "node_modules"
        if repo_modules.is_dir():
            parts.append(str(repo_modules))
        env = {"NODE_ENV": "test"}
        if parts:
            env["NODE_PATH"] = os.pathsep.join(parts)
        return env

    def timeout(self, key: str, default: int) -> int:
        conf = (self.config.get("hard_gate") or {}).get("timeouts_sec") or {}
        try:
            return int(conf.get(key, default))
        except (TypeError, ValueError):
            return default

    # -- configuration ------------------------------------------------------
    def load_configuration(self) -> bool:
        try:
            self.config = json.loads(Path(self.args.config).read_text(encoding="utf-8"))
        except Exception as err:  # noqa: BLE001
            self.set_infra(f"cannot read verifier config: {err}")
            return False

        f2p = self.config.get("f2p")
        p2p = self.config.get("p2p")
        if not isinstance(f2p, list) or not isinstance(p2p, list):
            self.set_infra("config.f2p and config.p2p must be arrays")
            return False
        if not all(isinstance(item, str) for item in [*f2p, *p2p]):
            self.set_infra("all configured node ids must be strings")
            return False
        if not f2p:
            self.set_infra("config.f2p must not be empty")
            return False
        if not p2p:
            self.set_infra("config.p2p must not be empty")
            return False
        duplicates = {node for node in [*f2p, *p2p] if [*f2p, *p2p].count(node) > 1}
        if duplicates:
            self.set_infra("duplicate node ids in config", duplicates=sorted(duplicates))
            return False
        overlap = set(f2p) & set(p2p)
        if overlap:
            self.set_infra("f2p and p2p must not overlap", overlap=sorted(overlap))
            return False
        self.f2p = list(f2p)
        self.p2p = list(p2p)

        # The optional state-preservation component only exists for tasks that
        # declare persistent state; everything else scores ``na`` and is dropped
        # from the T average instead of being averaged in as a free 1.0.
        state_nodes = self.config.get("required_state_nodes")
        if state_nodes is None:
            state_nodes = []
        if not isinstance(state_nodes, list) or not all(isinstance(node, str) for node in state_nodes):
            self.set_infra("config.required_state_nodes must be an array of node ids")
            return False
        self.state_nodes = list(state_nodes)

        # migration units registry
        units_path = Path(self.args.migration_units)
        try:
            unit_doc = json.loads(units_path.read_text(encoding="utf-8"))
        except Exception as err:  # noqa: BLE001
            self.set_infra(f"cannot read migration units: {err}")
            return False
        units = unit_doc.get("units") if isinstance(unit_doc, dict) else unit_doc
        if not isinstance(units, list) or not units:
            self.set_infra("migration_units must declare a non-empty units array")
            return False
        unit_ids: List[str] = []
        f2p_set = set(self.f2p)
        for unit in units:
            if not isinstance(unit, dict) or not isinstance(unit.get("id"), str):
                self.set_infra("every migration unit needs a string id")
                return False
            required = unit.get("required_nodes")
            if not isinstance(required, list) or not required:
                self.set_infra(f"migration unit {unit.get('id')} has no required_nodes")
                return False
            if not all(isinstance(node, str) for node in required):
                self.set_infra(f"migration unit {unit.get('id')} has non-string required_nodes")
                return False
            if len(set(required)) != len(required):
                self.set_infra(f"migration unit {unit.get('id')} repeats a required node")
                return False
            unknown = sorted(set(required) - f2p_set)
            if unknown:
                self.set_infra(f"migration unit {unit.get('id')} references unknown nodes", unknown=unknown)
                return False
            unit_ids.append(unit["id"])
        if len(set(unit_ids)) != len(unit_ids):
            self.set_infra("migration unit ids must be unique", ids=unit_ids)
            return False
        covered = {node for unit in units for node in unit["required_nodes"]}
        uncovered = sorted(f2p_set - covered)
        if uncovered:
            self.set_infra("every F2P node must be covered by a migration unit", uncovered=uncovered)
            return False
        self.units = units

        # instruction.md alignment (QC 105): the migration-unit set must be the
        # one instruction.md declares, not a self-consistent but smaller or
        # larger set assembled from the JSON alone. The frozen instruction text
        # ships next to the asset so the cross-check runs inside the (read-only)
        # evaluation image.
        instruction = unit_doc.get("instruction") if isinstance(unit_doc, dict) else None
        if not isinstance(instruction, dict):
            self.set_infra("migration_units.json is missing its instruction.md alignment block")
            return False
        instruction_path = self.tests_dir / "assets" / str(instruction.get("file") or "instruction.md")
        if not instruction_path.is_file():
            self.set_infra(f"frozen instruction.md is missing at {instruction_path}")
            return False
        instruction_text = instruction_path.read_text(encoding="utf-8")
        expected_sha = str(instruction.get("sha256") or "")
        actual_sha = hashlib.sha256(instruction_text.encode("utf-8")).hexdigest()
        if expected_sha and expected_sha != actual_sha:
            self.set_infra("the frozen instruction.md does not match migration_units.instruction.sha256")
            return False
        declared_anchors = instruction.get("anchors")
        if not isinstance(declared_anchors, list) or not declared_anchors:
            self.set_infra("migration_units.instruction.anchors must be a non-empty array")
            return False
        if not all(isinstance(anchor, str) and anchor for anchor in declared_anchors):
            self.set_infra("every instruction.md anchor must be a non-empty string")
            return False
        if list(declared_anchors) != [unit["instruction_anchor"] for unit in units]:
            self.set_infra(
                "the migration-unit anchors do not match instruction.md's declared anchors",
                declared=declared_anchors,
                units=[unit.get("instruction_anchor") for unit in units],
            )
            return False
        for anchor in declared_anchors:
            if anchor not in instruction_text:
                self.set_infra(f"instruction.md does not contain the declared anchor {anchor!r}")
                return False
        self.instruction_alignment = {
            "file": instruction_path.name,
            "sha256": actual_sha,
            "anchors": list(declared_anchors),
        }

        # frozen registries
        f2p_manifest_path = Path(self.args.f2p_manifest) if self.args.f2p_manifest else self.tests_dir / "assets" / "f2p_manifest.json"
        p2p_manifest_path = Path(self.args.p2p_manifest) if self.args.p2p_manifest else self.tests_dir / "assets" / "p2p_manifest.json"
        try:
            f2p_manifest = json.loads(f2p_manifest_path.read_text(encoding="utf-8"))
            p2p_manifest = json.loads(p2p_manifest_path.read_text(encoding="utf-8"))
        except Exception as err:  # noqa: BLE001
            self.set_infra(f"cannot read frozen manifests: {err}")
            return False

        manifest_f2p = [node.get("id") for node in f2p_manifest.get("nodes", [])]
        manifest_p2p = [node.get("id") for node in p2p_manifest.get("nodes", [])]
        if sorted(manifest_f2p) != sorted(self.f2p):
            self.set_infra("config.f2p does not match the frozen F2P registry")
            return False
        if manifest_p2p != self.p2p:
            self.set_infra("config.p2p does not match the frozen P2P manifest (order and identity)")
            return False
        self.f2p_manifest = f2p_manifest
        self.p2p_manifest = p2p_manifest
        self.p2p_records = p2p_manifest.get("nodes", [])
        return True

    # -- execution ----------------------------------------------------------
    def record_status(self, node: str, status: str, **extra: Any) -> None:
        payload = {"status": status}
        payload.update(extra)
        self.statuses[node] = payload

    def run_f2p(self) -> None:
        payload_path = self.f2p_payload_path()
        self.payload_hash_before = None
        if not payload_path.is_file():
            self.set_infra(f"hidden F2P payload missing at {payload_path}")
            for node in self.f2p:
                self.record_status(node, "not_run", reason="hidden payload missing")
            return
        # The payload must live outside every tree the candidate identity can
        # write; otherwise a node running in-process can rewrite the nodes that
        # have not executed yet and have the forged exit codes scored
        # (QC ADV-F2P-MUTABLE-PAYLOAD).
        mutable_issue = self.payload_mutability_issue(payload_path)
        if mutable_issue:
            self.set_infra(
                "hidden F2P payload is writable by the candidate identity, so its per-node "
                "results cannot be trusted: " + mutable_issue,
                payload=str(payload_path),
            )
            for node in self.f2p:
                self.record_status(node, "not_run", reason="hidden payload is not evaluator-owned")
            return
        self.payload_hash_before = self.payload_digest(payload_path)

        listed = sh(sandbox.wrap([self.node_bin, str(payload_path), "--list"]), cwd=self.repo, env=self.node_env(), timeout=60)
        if listed.get("missing") or listed.get("timed_out") or listed["returncode"] != 0:
            self.set_infra("cannot enumerate the hidden F2P payload", result={k: listed.get(k) for k in ("returncode", "timed_out", "missing")},
                           stderr=tail(listed.get("stderr", "")))
            for node in self.f2p:
                self.record_status(node, "not_run", reason="payload enumeration failed")
            return
        try:
            payload_nodes = json.loads(listed["stdout"].strip().splitlines()[-1])
        except Exception as err:  # noqa: BLE001
            self.set_infra(f"hidden payload --list output is not valid JSON: {err}", stdout=tail(listed["stdout"]))
            for node in self.f2p:
                self.record_status(node, "not_run", reason="payload enumeration unparsable")
            return
        if sorted(payload_nodes) != sorted(self.f2p):
            self.set_infra("hidden F2P payload registry does not match config.f2p",
                           payload=sorted(payload_nodes), config=sorted(self.f2p))
            for node in self.f2p:
                self.record_status(node, "not_run", reason="payload registry mismatch")
            return

        node_timeout = self.timeout("f2p_node", 60)
        for node in self.f2p:
            result = sh(sandbox.wrap([self.node_bin, str(payload_path), node]), cwd=self.repo, env=self.node_env(), timeout=node_timeout)
            if result.get("missing"):
                self.set_infra("node executable is not available for F2P execution")
                self.record_status(node, "not_run", reason="node missing")
                continue
            if result.get("timed_out"):
                self.record_status(node, "failed", reason=f"F2P watchdog timeout after {node_timeout}s",
                                   timed_out=True, stderr=tail(result.get("stderr", "")))
                continue
            # A namespace/capability failure is an evaluator problem, never a
            # candidate failure: reporting it as "case assertion failed" would
            # score an unisolated run instead of flagging the broken sandbox.
            if sandbox.namespace_error(result):
                self.set_infra("candidate sandbox failed while running an F2P node",
                               node=node, stderr=tail(result.get("stderr", "")))
                self.record_status(node, "not_run", reason="candidate sandbox unavailable")
                continue
            rc = result["returncode"]
            if rc == 0:
                self.record_status(node, "passed", duration_ms=None)
            elif rc == 1:
                self.record_status(node, "failed", reason="case assertion failed", stderr=tail(result.get("stderr", "")))
            elif rc == 3:
                self.record_status(node, "failed", reason="case watchdog timeout", timed_out=True,
                                   stderr=tail(result.get("stderr", "")))
            elif rc == 4:
                self.set_infra(f"hidden payload reported an unknown node: {node}")
                self.record_status(node, "not_run", reason="payload/config registry mismatch")
            else:
                self.record_status(node, "failed", reason=f"unexpected exit code {rc}", stderr=tail(result.get("stderr", "")))

        # Re-hash after the run: even with the ownership guard above, a payload
        # that changed mid-run means the per-node verdicts are not evidence.
        if self.payload_digest(payload_path) != self.payload_hash_before:
            self.set_infra("hidden F2P payload changed while the nodes were executing",
                           payload=str(payload_path),
                           payload_hash_before=self.payload_hash_before)

    def f2p_payload_path(self) -> Path:
        """Where the hidden F2P payload is executed from.

        ``--f2p-payload`` points at the evaluator-owned staging copy created by
        ``test.sh``; the in-repo path is only the legacy fallback.
        """
        override = getattr(self.args, "f2p_payload", None)
        if override:
            return Path(override).resolve()
        return (self.repo / F2P_PAYLOAD).resolve()

    @staticmethod
    def payload_digest(path: Path) -> Optional[str]:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def payload_mutability_issue(self, path: Path) -> Optional[str]:
        """Reasons the candidate identity could rewrite/delete the payload.

        The candidate runs as the unprivileged sandbox identity, so a payload it
        does not own and cannot write -- inside a directory chain that is also
        not writable by it -- is immutable for the duration of the run.
        """
        ids = sandbox.candidate_ids()
        if ids is None:
            # No unprivileged identity is configured: every subprocess runs as
            # the evaluator itself, so nothing extra can be asserted here.
            return None
        if not sandbox.is_root():
            # No privilege drop happens either, so the evaluated process already
            # runs as whatever owns the payload; the check would be vacuous.
            return None
        uid = ids[0]
        targets = [path, *path.parents]
        for target in targets:
            try:
                stat = target.stat()
            except OSError as err:
                return f"cannot stat {target}: {err}"
            if stat.st_uid == uid:
                return f"{target} is owned by the candidate identity (uid {uid})"
            if stat.st_mode & 0o022:
                return f"{target} is group/other-writable (mode {stat.st_mode & 0o777:o})"
            if target == Path(target.root):
                break
        return None

    def p2p_evidence(self) -> Dict[str, Dict[str, Any]]:
        """P2P node statuses produced by the hard gate.

        P2P is a hard-gate concern (``p2p_regression_preserved``): it is a
        boolean behavioural floor, it is not scored, and it never enters
        ``test_reward``. The verifier only carries the per-node outcome into the
        canonical CTRF/node report, because that report is the platform's single
        identity mapping for ``[metadata.tests].pass_to_pass_count``.
        """
        evidence = (self.hard_gate_doc or {}).get("p2p_nodes")
        if not isinstance(evidence, dict):
            return {}
        return {str(node): entry for node, entry in evidence.items() if isinstance(entry, dict)}

    def carry_p2p_evidence(self) -> None:
        """Keep measured gate outcomes even when another atom stops F2P."""
        for node, entry in self.p2p_evidence().items():
            if node in self.p2p:
                status = entry.get("status") or "not_run"
                if status not in {"passed", "failed", "skipped", "missing", "not_run"}:
                    self.set_infra(f"invalid hard-gate P2P status for {node}: {status!r}")
                    continue
                self.record_status(node, status, **{
                    key: value for key, value in entry.items() if key not in {"status", "node_id"}
                })

    def p2p_check_result(self) -> Dict[str, Any]:
        counts = self.counts(self.p2p)
        atom = (((self.hard_gate_doc.get("checks") or {}).get("p2p_regression_preserved") or {})
                .get("atoms") or {}).get("p2p_no_regression") or {}
        status = atom.get("status")
        if status not in {"pass", "fail", "na", "not_run"} or (status == "pass" and counts["passed"] != len(self.p2p)):
            status = ("na" if not self.p2p else "pass" if counts["passed"] == len(self.p2p)
                      else "fail" if counts["failed"] or counts["skipped"] else "not_run")
        return {"status": status, "evaluated": status not in {"not_run", "na"},
                "decided_by": "hard_gate", "scored": False,
                "p2p_pass_ratio": self.ratio(self.p2p), "p2p": counts}

    def counts(self, nodes: Sequence[str]) -> Dict[str, int]:
        result = {"total": len(nodes), "passed": 0, "failed": 0, "skipped": 0, "missing": 0, "not_run": 0}
        for node in nodes:
            status = (self.statuses.get(node) or {}).get("status", "missing")
            if status in result:
                result[status] += 1
            else:
                result["failed"] += 1
        return result

    def ratio(self, nodes: Sequence[str]) -> float:
        if not nodes:
            return 0.0
        passed = sum(1 for node in nodes if (self.statuses.get(node) or {}).get("status") == "passed")
        return passed / len(nodes)

    def build_unit_results(self) -> List[Dict[str, Any]]:
        results = []
        for unit in self.units:
            node_results = []
            unit_ok = True
            for node in unit["required_nodes"]:
                status = (self.statuses.get(node) or {}).get("status", "missing")
                if status != "passed":
                    unit_ok = False
                node_results.append({"node": node, "status": status})
            results.append({
                "id": unit["id"],
                "description": unit.get("description"),
                "required_nodes": unit["required_nodes"],
                "nodes": node_results,
                "status": "pass" if unit_ok else "fail",
            })
        return results

    def build_ctrf(self, nodes: List[Tuple[str, str]]) -> Dict[str, Any]:
        """Emit a CTRF report that satisfies the platform's report contract.

        Required nodes may only be ``passed`` or ``failed``: a missing, skipped
        or not-run node is reported as ``failed`` because
        ``missing_is_failure`` / ``skip_is_failure`` are in force. Every entry
        carries ``extra.deepswe.group``/``required`` so the platform can map it
        back to ``config.json`` exactly.
        """
        import datetime

        tests = []
        passed = failed = 0
        suites: List[str] = []
        for node, node_type in nodes:
            entry = self.statuses.get(node) or {"status": "missing"}
            status = entry.get("status", "missing")
            if status == "passed":
                ctrf_status = "passed"
                passed += 1
            else:
                ctrf_status = "failed"
                failed += 1
            duration = entry.get("duration_ms")
            if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
                duration = 0
            suite = "hidden/koa_migration_cases.js" if node_type == "f2p" else (entry.get("file") or "test")
            suites.append(suite)
            message = entry.get("reason") or entry.get("error") or ""
            tests.append({
                "testId": node,
                "name": node,
                "status": ctrf_status,
                "duration": duration,
                "suite": [suite],
                "message": str(message)[:2000],
                "extra": {"deepswe": {"group": node_type, "required": True}},
            })
        stop = int(datetime.datetime.now().timestamp() * 1000)
        start = self.start_ts or stop
        if start > stop:
            start = stop
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "reportFormat": "CTRF",
            "specVersion": "0.0.0",
            "timestamp": timestamp,
            "generatedBy": "migration-verifier",
            "results": {
                "tool": {"name": "migration-verifier", "version": SCHEMA_VERSION},
                "summary": {
                    "tests": len(tests),
                    "passed": passed,
                    "failed": failed,
                    "skipped": 0,
                    "pending": 0,
                    "other": 0,
                    "suites": len(set(suites)) or 1,
                    "start": start,
                    "stop": stop,
                    "duration": max(0, stop - start),
                },
                "tests": tests,
                "extra": {
                    "deepswe": {
                        "taskId": self.config.get("task_id"),
                        "baseCommit": self.config.get("base_commit"),
                    }
                },
            },
        }

    def build_node_report(self, unit_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "schema_version": NODE_REPORT_SCHEMA,
            "task_id": self.config.get("task_id"),
            "base_commit": self.config.get("base_commit"),
            "f2p": [{"id": node, **(self.statuses.get(node) or {"status": "missing"})} for node in self.f2p],
            "p2p": [{"id": node, **(self.statuses.get(node) or {"status": "missing"})} for node in self.p2p],
            "counts": {
                "f2p": self.counts(self.f2p),
                "p2p": self.counts(self.p2p),
            },
            "migration_units": unit_results,
            "instruction_alignment": self.instruction_alignment,
            "infrastructure_error": self.infra_error,
            "infrastructure_details": self.infra_details,
            "notes": self.notes,
        }

    def scope_source_files(self, root: Path) -> List[Path]:
        """Every JavaScript-executable file under the migration scope (``lib/``).

        The suffix filter must not be a bypass: a deprecated API or a source
        fallback hidden in a ``.cjs``/``.mjs`` file is the same completeness
        failure as in a ``.js`` file, so the scan covers Node's executable
        source surface instead of only ``*.js``. Extensionless regular files are
        included too (Node can execute them directly); non-JS sources such as
        generated type declarations or docs are not, to avoid flagging prose.
        """
        executable_suffixes = {".js", ".cjs", ".mjs", ".jsx"}
        files: List[Path] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            suffix = path.suffix.lower()
            if suffix and suffix not in executable_suffixes:
                continue
            files.append(path)
        return files

    def source_scan(self) -> Dict[str, List[Dict[str, Any]]]:
        """Classify source-framework references inside the migration scope.

        Uses the shared ``semantic_scan`` registry so the integrity and
        completeness layers never disagree about the same text (QC 116):

        * ``ordinary``   -- a plain literal reference to the old framework. It
          is a *completeness* signal (``migration_scope_complete``): an honest
          but unfinished migration is not punished as cheating (QC 114).
        * ``deprecated`` -- retained as an empty compatibility field. The task
          does not require removal of every deprecated Node API.
        * ``hidden``     -- an indirect/obfuscated reference (split string,
          template interpolation, aliased/global ``require``, ``module.require``
          indirection, runtime string decode). Deliberately concealed fallback
          belongs to the cheating branch (QC 113); the hard gate is
          authoritative, but the verifier reports it too.
        """
        conf = self.config.get("hard_gate") or {}
        modules = conf.get("forbidden_source_modules") or ["express", "body-parser", "multer"]
        root = self.repo / "lib"
        result: Dict[str, List[Dict[str, Any]]] = {"ordinary": [], "hidden": [], "deprecated": []}
        if not root.is_dir():
            return result
        for path in self.scope_source_files(root):
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = source.splitlines()
            for finding in semantic_scan.module_load_findings(source, modules):
                lineno = finding["line"]
                entry = {
                    "path": path.relative_to(self.repo).as_posix(),
                    "line": lineno,
                    "code": lines[lineno - 1].strip()[:200],
                }
                classification = finding["kind"]
                result[classification].append({**entry, "class": classification})
        return result

    def legacy_source_residue(self) -> List[Dict[str, Any]]:
        """Ordinary + deprecated residue that fails the scope component."""
        scan = self.source_scan()
        return [*scan["ordinary"], *scan["deprecated"]]


    def state_applicability(self) -> Dict[str, Any]:
        """Classify the conditional state component for this task.

        * ``persistent_state_involved == true``  -> ``applicable``
        * ``persistent_state_involved == false`` -> ``not_applicable`` (``na``)
        * field omitted / not a JSON boolean / unparsable -> ``indeterminate``

        ``indeterminate`` is deliberately distinct from ``na``: "we did not see a
        declaration" is not evidence that the check does not apply, so it is
        scored as a blocking failure rather than dropped from ``T``.
        """
        if STATE_DECLARATION_KEY not in self.config:
            return {
                "applicability": "indeterminate",
                "declared": None,
                "reason": (f"config omits {STATE_DECLARATION_KEY}; "
                           "applicability cannot be determined"),
            }
        declared = self.config.get(STATE_DECLARATION_KEY)
        if isinstance(declared, bool):
            if declared:
                return {
                    "applicability": "applicable",
                    "declared": True,
                    "reason": f"explicit declaration {STATE_DECLARATION_KEY}=true",
                }
            return {
                "applicability": "not_applicable",
                "declared": False,
                "reason": f"explicit declaration {STATE_DECLARATION_KEY}=false",
            }
        return {
            "applicability": "indeterminate",
            "declared": declared,
            "reason": (f"{STATE_DECLARATION_KEY} must be a JSON boolean; got "
                       f"{type(declared).__name__} {declared!r}"),
        }

    def verifier_scores(self, unit_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The three verifier components, each normalized to [0, 1].

        ``T`` is their weighted average over the *applicable* components: a
        component that does not apply (no persistent state here) is dropped and
        the remaining weights are renormalized, so an inapplicable check can
        never dilute or inflate the score.
        """
        scan = self.source_scan()
        self.scope_residue = [*scan["ordinary"], *scan["deprecated"]]
        self.deceptive_residue = scan["hidden"]
        scope_passed = sum(1 for unit in unit_results if unit["status"] == "pass")
        unit_coverage = scope_passed / len(unit_results) if unit_results else 0.0
        # A unit can pass while the migrated tree still carries the old stack
        # (unexecuted reference, hidden marker); residue fails the scope
        # component regardless of unit coverage. A concealed/obfuscated source
        # fallback is the same completeness failure *and* blocks ``success`` on
        # its own, so it must drop the scope score too: otherwise ``weighted_t``
        # could still reach 1.0 and the grader would pay full marks for a
        # verifier failure (QC item 8: verifier failure must never score 1).
        scope_score = 0.0 if (self.scope_residue or self.deceptive_residue) else unit_coverage
        f2p_counts = self.counts(self.f2p)
        f2p_score = self.ratio(self.f2p)

        state = self.state_applicability()
        state_passed = sum(
            1 for node in self.state_nodes
            if (self.statuses.get(node) or {}).get("status") == "passed"
        )
        # ``na`` is reserved for an explicit declaration. ``indeterminate`` keeps
        # an honest 0 in the numerator/denominator so an omitted or undecidable
        # declaration can never be silently excluded from ``T``.
        if state["applicability"] == "applicable":
            state_score: Optional[float] = (
                state_passed / len(self.state_nodes) if self.state_nodes else 0.0
            )
            state_status = "pass" if state_score == 1.0 else "fail"
            state_applicable = True
            state_evidence = (
                f"{state_passed}/{len(self.state_nodes)} state assertions preserved"
                if self.state_nodes
                else "declared applicable but required_state_nodes is empty"
            )
        elif state["applicability"] == "not_applicable":
            state_score = None
            state_status = "na"
            state_applicable = False
            state_evidence = f"not applicable (na); {state['reason']}"
        else:
            state_score = 0.0
            state_status = "indeterminate"
            state_applicable = True
            state_evidence = f"indeterminate; {state['reason']}"

        declared = (self.config.get("verifier") or {}).get("weights") or {}
        if not isinstance(declared, dict):
            declared = {}

        def weight_of(key: str) -> float:
            try:
                return max(0.0, float(declared.get(key, 1.0)))
            except (TypeError, ValueError):
                return 1.0

        entries = [
            {
                "id": "migration_scope_complete",
                "score": scope_score,
                "weight": weight_of("migration_scope_complete"),
                "applicable": True,
                "status": "pass" if scope_score == 1.0 else "fail",
                "applicability": "applicable",
                "evidence": (
                    f"{scope_passed}/{len(unit_results)} migration units migrated"
                    + (f"; {len(self.scope_residue)} legacy source residue detected"
                       if self.scope_residue else "")
                    + (f"; {len(self.deceptive_residue)} concealed source residue detected"
                       if self.deceptive_residue else "")
                ),
            },
            {
                "id": "f2p",
                "score": f2p_score,
                "weight": weight_of("f2p"),
                "applicable": True,
                "status": "pass" if f2p_score == 1.0 else "fail",
                "applicability": "applicable",
                "evidence": f"f2p {f2p_counts['passed']}/{f2p_counts['total']}",
            },
            {
                "id": "required_state_preservation_complete",
                "score": state_score,
                "weight": weight_of("required_state_preservation_complete"),
                "applicable": state_applicable,
                "status": state_status,
                "applicability": state["applicability"],
                "evidence": state_evidence,
            },
        ]
        return entries

    @staticmethod
    def weighted_t(entries: List[Dict[str, Any]]) -> float:
        # Only a component that was explicitly declared not-applicable (``na``)
        # leaves the average; ``indeterminate`` stays in with a 0 score.
        applicable = [
            entry for entry in entries
            if entry.get("status") != "na" and entry["applicable"] and entry["score"] is not None
        ]
        total_weight = sum(entry["weight"] for entry in applicable)
        if not applicable or total_weight <= 0:
            return 0.0
        return sum(entry["score"] * entry["weight"] for entry in applicable) / total_weight

    def build_verifier_result(self, unit_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        f2p_counts = self.counts(self.f2p)
        scope_passed = sum(1 for unit in unit_results if unit["status"] == "pass")
        scope_ratio = scope_passed / len(unit_results) if unit_results else 0.0
        f2p_ratio = self.ratio(self.f2p)
        p2p_counts = self.counts(self.p2p)
        p2p_ratio = self.ratio(self.p2p)

        if self.short_circuited:
            # hard gate is the verifier's floor (QC items 3/17): once it fails,
            # scope / F2P / state were never executed, so this report must not
            # fabricate a measured 0.0 for them. Each declared component is kept
            # with an explicit ``not_run`` verdict and no score at all, so a
            # downstream reader can never mistake "not evaluated" for "failed".
            reason = self.short_circuit_reason or "hard gate failed; verifier components not evaluated"
            failed_nodes = [
                node for node in self.f2p
                if (self.statuses.get(node) or {}).get("status") != "passed"
            ]
            return {
                "schema_version": SCHEMA_VERSION,
                "task_id": self.config.get("task_id"),
                "base_commit": self.config.get("base_commit"),
                "instruction_alignment": self.instruction_alignment,
                "success": False,
                "status": "not_run",
                "short_circuited": True,
                "short_circuit_reason": reason,
                "sandbox": sandbox.describe(),
                "infrastructure_error": self.infra_error,
                "infrastructure_details": self.infra_details,
                # No component is scored: ``{}`` (not three 0.0 scores) is the
                # only honest value when nothing ran.
                "scores": {},
                "t_value": 0.0,
                "checks": {
                    **{component: {"status": "not_run", "evaluated": False, "reason": reason}
                       for component in ("migration_scope_complete", "f2p_regression",
                                         "required_state_preservation_complete")},
                    "p2p_regression_preserved": self.p2p_check_result(),
                },
                "checks_not_evaluated": True,
                "failed_nodes": failed_nodes,
                "failed_node_count": len(failed_nodes),
                "quarantined_flaky_nodes": getattr(self, "quarantined", []),
                "notes": self.notes,
            }

        scores = self.verifier_scores(unit_results)
        t_value = self.weighted_t(scores)
        state = self.state_applicability()
        state_counts = self.counts(self.state_nodes)
        state_entry = next(
            (entry for entry in scores if entry["id"] == "required_state_preservation_complete"), None
        )

        # P2P is decided by the hard gate and is never scored here; ``clean``
        # therefore depends only on the three verifier components. A component
        # whose applicability could not be determined is ``indeterminate`` -- not
        # ``na`` -- so it blocks success instead of shrinking the denominator.
        clean = (
            self.infra_error is None
            and f2p_counts["passed"] == f2p_counts["total"]
            and scope_ratio == 1.0
            # A concealed fallback is not a completeness gap: it blocks success
            # on its own (and is routed to the cheating branch by the hard gate).
            and not self.deceptive_residue
            and not any(entry.get("status") == "indeterminate" for entry in scores)
            and all(entry["score"] == 1.0 for entry in scores if entry["applicable"])
        )
        failed_nodes = [
            node for node in self.f2p
            if (self.statuses.get(node) or {}).get("status") != "passed"
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.config.get("task_id"),
            "base_commit": self.config.get("base_commit"),
            "instruction_alignment": self.instruction_alignment,
            "success": bool(clean),
            "status": (
                "infrastructure_error" if self.infra_error
                else ("not_run" if self.short_circuited else ("pass" if clean else "fail"))
            ),
            # ``not_run`` is a first-class verdict: the behavioural components
            # below were never exercised because an upstream gate failed (or
            # the sandbox was unavailable), so they must not be read as a
            # measured verifier failure.
            "short_circuited": bool(self.short_circuited),
            "short_circuit_reason": self.short_circuit_reason,
            "sandbox": sandbox.describe(),
            "infrastructure_error": self.infra_error,
            "infrastructure_details": self.infra_details,
            "scores": {entry["id"]: {
                "score": entry["score"],
                "weight": entry["weight"],
                "applicable": entry["applicable"],
                # ``status`` makes ``na`` explicit instead of leaving it implied
                # by a null score; ``applicability`` carries the declaration the
                # status was derived from.
                "status": entry["status"],
                "applicability": entry["applicability"],
                "evidence": entry["evidence"],
            } for entry in scores},
            "t_value": t_value,
            "checks": {
                "migration_scope_complete": {
                    "status": (
                        "not_run" if self.short_circuited
                        else ("pass" if (scope_ratio == 1.0 and not self.scope_residue
                                         and not self.deceptive_residue) else "fail")
                    ),
                    "passed_units": scope_passed,
                    "total_units": len(unit_results),
                    # ``coverage`` is the effective scope score (0 when residue
                    # is present); ``unit_coverage`` keeps the raw unit ratio.
                    "coverage": scores[0]["score"],
                    "unit_coverage": scope_ratio,
                    "residue": self.scope_residue,
                    "deceptive_residue": self.deceptive_residue,
                    "units": unit_results,
                },
                "f2p_regression": {
                    "status": (
                        "not_run" if self.short_circuited
                        else ("pass" if f2p_ratio == 1.0 else "fail")
                    ),
                    "f2p_pass_ratio": f2p_ratio,
                    "f2p": f2p_counts,
                },
                "required_state_preservation_complete": {
                    # ``status`` is one of pass / fail / na / indeterminate.
                    # ``na`` only ever comes from an explicit boolean ``false``
                    # declaration; an omitted or non-boolean declaration reports
                    # ``indeterminate`` and is scored as a failure.
                    "status": (
                        "not_run" if self.short_circuited
                        else (state_entry["status"] if state_entry else "indeterminate")
                    ),
                    "applicability": state_entry["applicability"] if state_entry else "indeterminate",
                    "applicable": bool(state_entry["applicable"]) if state_entry else False,
                    "score": state_entry["score"] if state_entry else None,
                    "reason": state["reason"],
                    "declared": state["declared"],
                    "state_nodes": {"passed": state_counts["passed"], "total": state_counts["total"]},
                },
                # Informational only: the boolean belongs to the hard gate
                # (``p2p_regression_preserved``) and never enters T.
                "p2p_regression_preserved": self.p2p_check_result(),
            },
            "failed_nodes": failed_nodes,
            "failed_node_count": len(failed_nodes),
            "quarantined_flaky_nodes": getattr(self, "quarantined", []),
            "notes": self.notes,
        }

    # -- entrypoint ---------------------------------------------------------
    def run(self) -> int:
        import time
        self.start_ts = int(time.time() * 1000)
        if not self.load_configuration():
            self.write_outputs(infra=True)
            return 2

        # Fail closed: if the namespace/identity barrier cannot be built, the
        # run must be reported as an infrastructure error instead of silently
        # executing candidate code with network access and full privileges.
        sandbox_tools = sandbox.check_sandbox_tools()

        hard_gate_doc = {}
        if getattr(self.args, "hard_gate_report", None) and os.path.exists(self.args.hard_gate_report):
            try:
                hard_gate_doc = json.loads(Path(self.args.hard_gate_report).read_text(encoding="utf-8"))
            except Exception:
                pass
        self.hard_gate_doc = hard_gate_doc
        self.carry_p2p_evidence()
        hard_gate_failed = bool(hard_gate_doc and (hard_gate_doc.get("success") is not True or hard_gate_doc.get("status") != "pass"))

        apply_failed = bool(self.args.apply_failed)
        if sandbox_tools.get("success") is not True:
            self.set_infra(sandbox_tools.get("summary") or "candidate sandbox is unavailable",
                           sandbox=sandbox_tools)
            reason = "candidate sandbox is unavailable; behavioral nodes not executed"
            self.short_circuited = True
            self.short_circuit_reason = reason
            for node in [*self.f2p, *self.p2p]:
                if node not in self.statuses:
                    self.record_status(node, "not_run", reason=reason)
            self.notes.append(reason)
        elif hard_gate_failed:
            reason = "Hard Gate failed; behavioral nodes not executed"
            self.short_circuited = True
            self.short_circuit_reason = reason
            for node in [*self.f2p, *self.p2p]:
                if node not in self.statuses:
                    self.record_status(node, "not_run", reason=reason)
            self.notes.append(reason)
        elif apply_failed:
            for node in [*self.f2p, *self.p2p]:
                if node not in self.statuses:
                    self.record_status(node, "failed", reason="candidate patch did not apply or was empty")
            self.notes.append("candidate patch was empty or could not be applied; all nodes recorded as failed")
        else:
            self.run_f2p()

        unit_results = self.build_unit_results()
        self.write_outputs(unit_results=unit_results)
        return 2 if self.infra_error else 0

    def write_outputs(self, unit_results: Optional[List[Dict[str, Any]]] = None, infra: bool = False) -> None:
        unit_results = unit_results if unit_results is not None else self.build_unit_results()
        for node in [*self.f2p, *self.p2p]:
            if node not in self.statuses:
                self.statuses[node] = {"status": "not_run", "reason": "verifier aborted before execution"}
        nodes = [(node, "f2p") for node in self.f2p] + [(node, "p2p") for node in self.p2p]
        ctrf = self.build_ctrf(nodes)
        node_report = self.build_node_report(unit_results)
        verifier_result = self.build_verifier_result(unit_results)

        def write(path: str, payload: Dict[str, Any]) -> None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        write(self.args.ctrf_output, ctrf)
        write(self.args.node_report, node_report)
        write(self.args.verifier_result, verifier_result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration completion verifier")
    parser.add_argument("--config", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--ctrf-output", required=True)
    parser.add_argument("--node-report", required=True)
    parser.add_argument("--migration-units", required=True)
    parser.add_argument("--verifier-result", required=True)
    parser.add_argument("--f2p-manifest", default=None)
    parser.add_argument("--p2p-manifest", default=None)
    parser.add_argument("--tests-dir", default=None)
    parser.add_argument("--node", default=None)
    # The hidden F2P payload is executed from an evaluator-owned staging tree
    # (see test.sh): the candidate repository is agent-writable, so running the
    # payload from inside it let candidate code replace later nodes and have the
    # forged exit codes scored (QC ADV-F2P-MUTABLE-PAYLOAD). When omitted the
    # legacy in-repo path is used for backwards compatibility.
    parser.add_argument("--f2p-payload", default=None)
    parser.add_argument("--apply-failed", type=int, default=0)
    parser.add_argument("--hard-gate-report", default=None)
    args = parser.parse_args()
    return Verifier(args).run()


if __name__ == "__main__":
    sys.exit(main())
