"""Direct Harbor verifier entry; this module never starts Docker."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .baseline import source_baseline_errors
from .differential import compare
from .comparison import merge_comparison_status
from .evidence import build_hardgate_evidence
from .hardgate_engine import evaluate_hardgate
from .observations import (
    normalize_observation_report,
    observation_contract_errors,
    observation_result_errors,
)
from .sandbox import (
    candidate_environment,
    check_sandbox_tools,
    cleanup_candidate_workspace,
    cleanup_readonly_assets,
    is_namespace_error,
    network_isolated_command,
    prepare_candidate_workspace,
    prepare_readonly_assets,
    prepare_writable_path,
    sandbox_command,
)
from .security_regression import run_security_self_check
from .verifier_engine import evaluate_verifier


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
    mode: str = "network",
) -> subprocess.CompletedProcess[str]:
    execution_command = sandbox_command(command) if mode == "candidate" else network_isolated_command(command)
    subprocess_cwd = cwd
    if mode == "candidate" and environment.get("HARBOR_READONLY_ROOT"):
        # cwd is established before exec(unshare).  Starting from the
        # scratch parent prevents retaining a pre-mount writable directory
        # reference; namespace_prefix() changes into the remounted repo.
        subprocess_cwd = Path(environment["HARBOR_READONLY_ROOT"]).parent
    return subprocess.run(
        execution_command,
        cwd=subprocess_cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
        env={**environment},
    )


def _run_with_receipt(
    command: list[str],
    *,
    kind: str,
    index: int,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
    mode: str = "network",
) -> tuple[subprocess.CompletedProcess[str] | None, dict[str, object]]:
    execution_command = sandbox_command([str(item) for item in command]) if mode == "candidate" else network_isolated_command([str(item) for item in command])
    receipt: dict[str, object] = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": kind,
        "index": index,
        "command": list(command),
        "execution_command": execution_command,
        "execution_mode": mode,
        "network_isolated": os.name != "nt",
        "candidate_privilege_dropped": mode == "candidate" and os.name != "nt",
        "returncode": None,
        "timed_out": False,
    }
    try:
        completed = _run(
            command,
            cwd=cwd,
            timeout=timeout,
            environment=environment,
            mode=mode,
        )
    except subprocess.TimeoutExpired as exc:
        receipt["timed_out"] = True
        receipt["diagnostic"] = str(exc)[-2000:]
        return None, receipt
    except OSError as exc:
        receipt["infrastructure_error"] = True
        receipt["diagnostic"] = str(exc)[-2000:]
        return None, receipt
    receipt["returncode"] = completed.returncode
    receipt["stdout_tail"] = (completed.stdout or "")[-4000:]
    receipt["stderr_tail"] = (completed.stderr or "")[-4000:]
    return completed, receipt


def _run_declared_commands(
    commands: Sequence[tuple[str, Sequence[str]]],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
    mode: str = "network",
) -> tuple[list[dict[str, object]], bool, bool]:
    receipts: list[dict[str, object]] = []
    for index, (kind, raw) in enumerate(commands, 1):
        completed, receipt = _run_with_receipt(
            [str(item) for item in raw],
            kind=kind,
            index=index,
            cwd=cwd,
            timeout=timeout,
            environment=environment,
            mode=mode,
        )
        receipts.append(receipt)
        if completed is None:
            # A timeout is attributable to the candidate command.  Only an
            # inability to start the command (or a namespace setup failure)
            # is verifier infrastructure failure.
            if receipt.get("timed_out") is True:
                return receipts, True, False
            return receipts, False, True
        if completed.returncode != 0:
            infrastructure = mode == "candidate" and is_namespace_error(
                completed.stderr or "", completed.returncode
            )
            return receipts, not infrastructure, infrastructure
    return receipts, False, False


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _write_result(logs: Path, result: dict[str, object]) -> dict[str, object]:
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "verifier-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _execution_failure_result(
    *,
    receipts: list[dict[str, object]],
    infrastructure_error: bool,
    failure_reason: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": "infra_error" if infrastructure_error else "failed",
        "hardgate": "INFRA_ERROR" if infrastructure_error else "FAIL",
        "cheating": False,
        "components": {},
        "test_progress": None if infrastructure_error else {"T": 0.0},
        "eligible_for_graded": False,
        "observation_contract_errors": [],
        "observations": [],
        "candidate_execution_error": failure_reason or (
            "verifier command infrastructure failure"
            if infrastructure_error
            else "candidate prerequisite command failed"
        ),
        "receipts": {"commands": receipts, "harness_returncode": None},
    }


def _apply_observation_contract_errors(
    result: dict[str, object],
    errors: Sequence[str],
) -> None:
    # Evidence-shape errors are candidate validity failures.  The evidence
    # builder maps them to candidate_valid=FAIL, which yields the ordinary
    # hardgate reward of 0; they are not evidence of anti-cheating activity.
    result["observation_contract_errors"] = list(errors)


def run_harbor_verifier(
    *,
    candidate_root: Path,
    bundle_root: Path,
    logs_root: Path,
    apply_failed: bool = False,
    patch_message: str = "",
) -> dict[str, object]:
    candidate = candidate_root.resolve()
    bundle = bundle_root.resolve()
    logs = logs_root.resolve()
    if not candidate.is_dir() or candidate.is_symlink():
        raise ValueError("Harbor candidate /app is missing or unsafe")
    config = _load_json(bundle / "config.json")
    harbor = config.get("harbor")
    if isinstance(harbor, Mapping) and harbor.get("reward_mode") != "VERIFIER_ONLY":
        raise ValueError("Harbor verifier only supports VERIFIER_ONLY")
    assets = bundle / "assets"
    config.setdefault("policy", {})
    runtime = config.get("target_environment", {}).get("runtime", {})
    timeout = float(runtime.get("timeout_seconds", 1800))
    logs.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            logs.chmod(0o700)
        except OSError:
            pass
    if apply_failed:
        result = {
            "schema_version": "migration-verifier-result:v2",
            "status": "failed",
            "hardgate": "FAIL",
            "cheating": False,
            "components": {},
            "test_progress": {"T": 0.0},
            "eligible_for_graded": False,
            "observation_contract_errors": [],
            "observations": [],
            "candidate_patch_error": patch_message,
            "receipts": {"commands": [], "harness_returncode": None},
        }
        return _write_result(logs, result)
    try:
        source_baseline = _load_json(assets / "source-baseline-outputs.json")
        baseline_errors = source_baseline_errors(assets, config, source_baseline)
    except (OSError, ValueError) as exc:
        baseline_errors = (str(exc),)
    if baseline_errors:
        return _write_result(logs, _execution_failure_result(
            receipts=[{
                "schema_version": "migration-execution-receipt:v1",
                "kind": "source-baseline-validation",
                "returncode": None,
                "timed_out": False,
                "infrastructure_error": True,
                "diagnostic": "; ".join(baseline_errors),
            }],
            infrastructure_error=True,
            failure_reason="verifier source baseline does not match evaluator test cases: "
                           + "; ".join(baseline_errors),
        ))
    self_check = run_security_self_check()
    if self_check.get("success") is not True:
        return _write_result(
            logs,
            _execution_failure_result(
                receipts=[{
                    "schema_version": "migration-execution-receipt:v1",
                    "kind": "verifier-security-self-check",
                    "returncode": 1,
                    "timed_out": False,
                    "infrastructure_error": True,
                    "diagnostic": json.dumps(self_check, ensure_ascii=False)[-4000:],
                }],
                infrastructure_error=True,
                failure_reason="verifier security regression failed",
            ),
        )
    sandbox_tools = check_sandbox_tools()
    if sandbox_tools["success"] is not True:
        return _write_result(
            logs,
            _execution_failure_result(
                receipts=[{
                    "schema_version": "migration-execution-receipt:v1",
                    "kind": "sandbox-capability-probe",
                    "returncode": None,
                    "timed_out": False,
                    "infrastructure_error": True,
                    "diagnostic": sandbox_tools["summary"],
                    "sandbox": sandbox_tools,
                }],
                infrastructure_error=True,
            ),
        )
    try:
        candidate_scratch, execution_root = prepare_candidate_workspace(candidate)
        asset_holder, candidate_assets = prepare_readonly_assets(assets)
    except OSError as exc:
        if "candidate_scratch" in locals():
            cleanup_candidate_workspace(candidate_scratch)
        return _write_result(
            logs,
            _execution_failure_result(
                receipts=[{
                    "schema_version": "migration-execution-receipt:v1",
                    "kind": "candidate-workspace",
                    "returncode": None,
                    "timed_out": False,
                    "infrastructure_error": True,
                    "diagnostic": str(exc),
                }],
                infrastructure_error=True,
            ),
        )

    def finish(result: dict[str, object]) -> dict[str, object]:
        cleanup_readonly_assets(asset_holder)
        cleanup_candidate_workspace(candidate_scratch)
        return _write_result(logs, result)

    # Only explicitly declared runtime storage is writable.  The repository
    # itself is mounted read-only before the candidate UID is entered.
    writable_paths: list[Path] = []
    for materialization in runtime.get("workspace_materializations", []):
        if not isinstance(materialization, Mapping):
            continue
        source = Path(str(materialization.get("source", "")))
        target = execution_root / str(materialization.get("target", ""))
        if source.is_dir() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copytree(source, target, symlinks=False)
                prepare_writable_path(target)
            except (OSError, shutil.Error) as exc:
                return finish(_execution_failure_result(
                    receipts=[{
                        "schema_version": "migration-execution-receipt:v1",
                        "kind": "candidate-workspace",
                        "returncode": None,
                        "timed_out": False,
                        "infrastructure_error": True,
                        "diagnostic": str(exc),
                    }],
                    infrastructure_error=True,
                ))
        if target.exists():
            prepare_writable_path(target)
            writable_paths.append(target)
    for storage in runtime.get("writable_storage", []):
        if not isinstance(storage, Mapping):
            continue
        relative = str(storage.get("target", "")).strip()
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            return finish(_execution_failure_result(
                receipts=[], infrastructure_error=True,
            ))
        target = execution_root / relative
        try:
            prepare_writable_path(target)
        except OSError as exc:
            return finish(_execution_failure_result(
                receipts=[{
                    "schema_version": "migration-execution-receipt:v1",
                    "kind": "candidate-workspace",
                    "returncode": None,
                    "timed_out": False,
                    "infrastructure_error": True,
                    "diagnostic": str(exc),
                }],
                infrastructure_error=True,
            ))
        if target not in writable_paths:
            writable_paths.append(target)
    report_dir = candidate_scratch / "report"
    prepare_writable_path(report_dir)
    writable_paths.append(report_dir)
    environment = candidate_environment(
        candidate_scratch,
        runtime.get("environment", {}) if isinstance(runtime.get("environment", {}), Mapping) else {},
        readonly_root=execution_root,
        writable_paths=writable_paths,
    )
    placeholder_values = {
        "repo": str(execution_root),
        "assets": str(candidate_assets),
        "report": str(report_dir / "result.json"),
    }
    commands = [
        *(("target-build", raw) for raw in runtime.get("build_commands", [])),
        *(("target-native-test", raw) for raw in runtime.get("native_test_commands", [])),
    ]
    receipts: list[dict[str, object]] = []
    rendered_commands = [
        (phase, [str(item).format_map(placeholder_values) for item in raw])
        for phase, raw in commands
    ]
    receipts, candidate_failed, infrastructure_failed = _run_declared_commands(
        rendered_commands,
        cwd=execution_root,
        timeout=timeout,
        environment=environment,
        mode="candidate",
    )
    if candidate_failed or infrastructure_failed:
        timed_out = any(receipt.get("timed_out") is True for receipt in receipts)
        return finish(
            _execution_failure_result(
                receipts=receipts,
                infrastructure_error=infrastructure_failed,
                failure_reason="candidate command timed out" if timed_out else None,
            ),
        )
    # A declaration in setup.cfg is not proof that the target environment runs
    # the migrated Click.  Probe the actual interpreter before accepting the
    # build; a hidden or stale 6.7 runtime is a migration failure, not a pass.
    version_probe = runtime.get("version_probe")
    if isinstance(version_probe, list) and version_probe:
        probe_command = [
            str(item).format_map(placeholder_values) for item in version_probe
        ]
        completed_probe, probe_receipt = _run_with_receipt(
            probe_command,
            kind="version-probe",
            index=len(receipts) + 1,
            cwd=execution_root,
            timeout=min(timeout, 120.0),
            environment=environment,
            mode="candidate",
        )
        receipts.append(probe_receipt)
        if completed_probe is None:
            return finish(
                _execution_failure_result(
                    receipts=receipts,
                    infrastructure_error=probe_receipt.get("timed_out") is not True,
                    failure_reason=(
                        "target runtime version probe timed out"
                        if probe_receipt.get("timed_out") is True
                        else "target runtime version probe could not start"
                    ),
                )
            )
        if completed_probe.returncode != 0:
            return finish(
                _execution_failure_result(
                    receipts=receipts,
                    infrastructure_error=False,
                    failure_reason="target runtime does not provide Click 8.1.7",
                )
            )
    # The migration suite is evaluator-owned and is materialized only after
    # the candidate's native tests have completed.  This keeps the native
    # upstream suite isolated from the contract suite while still making the
    # frozen source-test baseline available at its repository-relative path
    # (``test/``) for the target harness.
    migration_tests = candidate_assets / "candidate-test"
    target_test_tree = execution_root / "test"
    if migration_tests.is_dir():
        if target_test_tree.is_symlink():
            return finish(_execution_failure_result(
                receipts=receipts,
                infrastructure_error=True,
                failure_reason="candidate test tree is an unsafe symlink",
            ))
        try:
            shutil.copytree(
                migration_tests,
                target_test_tree,
                symlinks=False,
                dirs_exist_ok=True,
            )
        except (OSError, shutil.Error) as exc:
            return finish(_execution_failure_result(
                receipts=receipts,
                infrastructure_error=True,
                failure_reason=f"unable to materialize migration test tree: {exc}",
            ))
    harness = runtime.get("harness_command")
    if not isinstance(harness, list) or not harness:
        raise ValueError("target runtime harness command is missing")
    command = [str(item).format_map(placeholder_values) for item in harness]
    completed, harness_receipt = _run_with_receipt(
        command,
        kind="target-execution",
        index=len(receipts) + 1,
        cwd=execution_root,
        timeout=timeout,
        environment=environment,
        mode="candidate",
    )
    if completed is None:
        receipts.append(harness_receipt)
        return finish(
            _execution_failure_result(
                receipts=receipts,
                infrastructure_error=harness_receipt.get("timed_out") is not True,
                failure_reason=(
                    "candidate harness timed out"
                    if harness_receipt.get("timed_out") is True
                    else None
                ),
            ),
        )
    report = Path(placeholder_values["report"])
    harness_receipt["report_present"] = report.is_file()
    receipts.append(harness_receipt)
    if not report.is_file():
        if completed.returncode != 0:
            namespace_failure = is_namespace_error(completed.stderr or "", completed.returncode)
            if namespace_failure:
                return finish(
                    _execution_failure_result(
                        receipts=receipts,
                        infrastructure_error=True,
                    ),
                )
            result = {
                "schema_version": "migration-verifier-result:v2",
                "status": "failed",
                "hardgate": "FAIL",
                "cheating": False,
                "components": {},
                "test_progress": {"T": 0.0},
                "eligible_for_graded": False,
                "observation_contract_errors": [],
                "observations": [],
                "candidate_harness_error": "harness exited without an observations report",
                "receipts": {"commands": receipts, "harness_returncode": completed.returncode},
            }
            return finish(result)
        raise ValueError("Harbor candidate harness did not produce a candidate report")
    payload = _load_json(report)
    # Keep the evaluator-owned copy available for downstream tooling while
    # ensuring the candidate process never had write access to /logs.
    logs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(report, logs / "result.json")
    payload = normalize_observation_report(
        payload,
        role="candidate",
        returncode=completed.returncode,
        report_path="/logs/verifier/result.json",
        config=config,
    )
    contract_errors = (
        *observation_contract_errors(
            payload, config, actual_returncode=completed.returncode
        ),
        *observation_result_errors(
            payload, actual_returncode=completed.returncode
        ),
    )
    receipts.append({
        "schema_version": "migration-execution-receipt:v1",
        "kind": "report",
        "returncode": 0,
        "timed_out": False,
        "valid": not contract_errors,
    })
    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        observations = []
    baseline_rows = source_baseline.get("observations", [])
    baseline_by_id = {str(row.get("id")): row for row in baseline_rows if isinstance(row, Mapping)} if isinstance(baseline_rows, list) else {}
    compared: list[dict[str, object]] = []
    for row in observations:
        current = dict(row)
        baseline = baseline_by_id.get(str(current.get("id")))
        if current.get("kind") in {"P2P", "DIFFERENTIAL"} and baseline is not None:
            current["source_value"] = baseline.get("value")
            current["candidate_value"] = current.get("value")
            comparison_status = compare(
                baseline.get("value"),
                current.get("value"),
                config.get("normalization_policy", {}),
            )["status"]
            current["status"] = merge_comparison_status(
                str(current.get("status", "INCONCLUSIVE")),
                str(comparison_status),
            )
        compared.append(current)
    by_kind = {
        kind: [row for row in compared if row.get("kind") == kind]
        for kind in ("P2P", "F2P", "DIFFERENTIAL", "STATE")
    }
    baseline_name = config.get("assets", {}).get("source_test_baseline")
    test_baseline = _load_json(assets / str(baseline_name)) if baseline_name else {"files": {}, "marker_counts": {}}
    evidence = build_hardgate_evidence(
        candidate,
        config=config,
        observations=compared,
        observation_errors=contract_errors,
        receipts=receipts,
        patch_applicable=not apply_failed,
        source_test_baseline=test_baseline,
        # Test files are injected into the isolated execution copy after
        # native tests.  Integrity must inspect that final tree, while the
        # other anti-cheating scans continue to inspect the patched candidate.
        test_integrity_root=execution_root,
    )
    hardgate = evaluate_hardgate(candidate, evidence)
    # Compact gate summary for the report contract (/logs/verifier/hard-gate.json).
    # The full evidence payload stays in this result's hardgate-independent
    # fields; only status/applicability are carried so the artifact stays small.
    hardgate_compound_gates = {
        str(name): {
            "status": str(gate.get("status", "INCONCLUSIVE")),
            "applicable": bool(gate.get("applicable")),
        }
        for name, gate in (hardgate.get("compound_gates") or {}).items()
        if isinstance(gate, Mapping)
    }
    units_path = assets / str(config["assets"]["migration_units"])
    units = _load_json(units_path).get("units", [])
    units = [dict(row) for row in units if isinstance(row, Mapping)]
    unit_checks = config.get("migration_unit_checks")
    result = evaluate_verifier(
        hardgate=hardgate,
        migration_units=units,
        unit_checks=unit_checks,
        p2p=by_kind["P2P"],
        f2p=by_kind["F2P"],
        differential=by_kind["DIFFERENTIAL"],
        state={"applicable": bool(config["policy"].get("state_applicable")), "checks": by_kind["STATE"]},
        weights=config["weights"],
        applicability=config.get("group_applicability"),
    )
    _apply_observation_contract_errors(result, contract_errors)
    result["observations"] = compared
    result["cheating"] = bool(hardgate.get("cheating"))
    result["hardgate_compound_gates"] = hardgate_compound_gates
    result["hardgate_cheat_level"] = str(hardgate.get("cheat_level", "L0"))
    # Keep the evidence for failing atoms available in the result.  Without it a
    # -1 reward that came from a static integrity scan is not auditable after the
    # temporary candidate tree is removed.
    result["hardgate_failing_atoms"] = {
        str(name): atom
        for name, atom in (hardgate.get("automated_evidence") or {}).items()
        if isinstance(atom, Mapping)
        and str(atom.get("status", "")).upper() not in {"PASS", "NA"}
    }
    if apply_failed:
        result["candidate_patch_error"] = patch_message
    result["receipts"] = {"commands": receipts, "harness_returncode": completed.returncode}
    logs.mkdir(parents=True, exist_ok=True)
    return finish(result)


__all__ = ["run_harbor_verifier"]
