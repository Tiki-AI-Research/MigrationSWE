from __future__ import annotations
import argparse, hashlib, json, shlex, stat, subprocess, sys
sys.dont_write_bytecode = True
from pathlib import Path
from hardgate_engine import evaluate_hardgate
from differential import compare
from comparison import merge_comparison_status
from observations import normalize_observation_report, observation_contract_errors
from evidence import build_hardgate_evidence, ConfigurationError
from baseline import BaselineError, expand_compact_value
from state_preservation import (
    ApplicabilityError,
    load_migration_contract,
    resolve_state_applicability,
)
from verifier_engine import evaluate_verifier

def _digest_tree(root):
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rows.append((path.relative_to(root).as_posix(), "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()))
    return "sha256:" + hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def _digest_file(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

def _validate_runtime_files(bundle):
    manifest_path = bundle / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest.get("files")
    actual = {}
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        if path.is_file() and path != manifest_path and relative not in {"manifest.json", "delivery-provenance.json"}:
            actual[relative] = _digest_file(path)
    if not isinstance(expected, dict) or actual != expected:
        raise SystemExit("runtime files changed and no longer match the manifest")

def _run(command, timeout):
    try:
        return subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 125, "", str(exc))

def _workspace_script(runtime):
    steps = ["cp -R /input/repo/. /workspace/repo"]
    for item in runtime.get("workspace_materializations", []):
        source = shlex.quote(item["source"])
        target_path = "/workspace/repo/" + item["target"]
        target = shlex.quote(target_path)
        parent = shlex.quote(str(Path(target_path).parent))
        steps.extend((f"rm -rf {target}", f"mkdir -p {parent}"))
        operation = "ln -s" if item["mode"] == "symlink" else "cp -R"
        steps.append(f"{operation} {source} {target}")
    steps.append("cd /workspace/repo && exec sh /assets/target/run.sh")
    return " && ".join(steps)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    bundle = args.config.parent
    _validate_runtime_files(bundle)
    environment = bundle / config["target_environment"]["path"]
    if _digest_tree(environment) != config["target_environment"]["tree_sha256"]:
        raise SystemExit("target environment digest mismatch")
    image = "migration-grader-runtime:" + config["target_environment"]["tree_sha256"].split(":", 1)[-1][:16]
    timeout = float(config["target_environment"]["runtime"]["timeout_seconds"])
    build = _run([args.docker, "build", "--file", str(environment / "Dockerfile"), "--tag", image, str(environment)], timeout)
    inspected = _run([args.docker, "image", "inspect", image, "--format", "{{.Id}}"], min(timeout, 60)) if build.returncode == 0 else subprocess.CompletedProcess([], 125, "", "image build failed")
    image_ref = inspected.stdout.strip() if inspected.returncode == 0 else ""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = args.output.parent.resolve()
    evidence_mode = stat.S_IMODE(evidence.stat().st_mode)
    environment_args = []
    for key, value in sorted(config["target_environment"]["runtime"].get("environment", {}).items()):
        environment_args.extend(["--env", f"{key}={value}"])
    if image_ref:
        try:
            evidence.chmod(0o777)
            run = _run([
                args.docker, "run", "--rm", "--network", "none", "--read-only", "--cap-drop=ALL",
                "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,exec,nosuid,nodev",
                "--mount", f"type=bind,source={args.candidate_root.resolve()},target=/input/repo,readonly",
                "--mount", f"type=bind,source={(bundle / 'assets').resolve()},target=/assets,readonly",
                "--mount", f"type=bind,source={evidence},target=/logs",
                "--mount", "type=volume,target=/workspace/repo,volume-nocopy",
                *environment_args,
                image_ref, "sh", "-lc", _workspace_script(config["target_environment"]["runtime"])
            ], timeout)
        finally:
            evidence.chmod(evidence_mode)
    else:
        run = subprocess.CompletedProcess([], 125, "", "image build or inspection failed")
    report = evidence / "result.json"
    report_present = report.is_file()
    payload = json.loads(report.read_text(encoding="utf-8")) if report_present else {"observations": []}
    payload = normalize_observation_report(payload, role="candidate", returncode=run.returncode, report_path="/logs/result.json", config=config)
    observations = payload.get("observations", [])
    contract_errors = list(observation_contract_errors(payload, config, actual_returncode=run.returncode))
    if not report_present:
        contract_errors.insert(
            0,
            "candidate harness produced no observations report: "
            f"container exited with code {run.returncode}",
        )
    baseline_path = bundle / "assets" / "source-baseline-outputs.json"
    if not baseline_path.is_file():
        raise SystemExit("source baseline outputs are missing")
    baseline_rows = json.loads(baseline_path.read_text(encoding="utf-8")).get("observations", [])
    baseline_by_id = {}
    for row in baseline_rows:
        if not isinstance(row, dict):
            continue
        envelope = row.get("value")
        if isinstance(envelope, dict) and "value" in envelope:
            try:
                envelope = dict(envelope)
                envelope["value"] = expand_compact_value(
                    str(row.get("id")), envelope["value"]
                )
            except BaselineError as exc:
                raise SystemExit(str(exc))
            row = dict(row)
            row["value"] = envelope
        baseline_by_id[str(row.get("id"))] = row
    compared = []
    for row in observations:
        current = dict(row)
        baseline = baseline_by_id.get(str(current.get("id")))
        if current.get("kind") in {"P2P", "DIFFERENTIAL"} and baseline is not None:
            candidate_value = current.get("value")
            source_value = baseline.get("value")
            current["source_value"] = source_value
            current["candidate_value"] = candidate_value
            comparison_status = compare(source_value, candidate_value, config.get("normalization_policy", {}))["status"]
            current["status"] = merge_comparison_status(current.get("status", "INCONCLUSIVE"), comparison_status)
        compared.append(current)
    observations = compared
    by_kind = {kind: [row for row in observations if row.get("kind") == kind] for kind in ("P2P", "F2P", "DIFFERENTIAL", "STATE")}
    baseline = json.loads((bundle / "assets" / config["assets"].get("source_test_baseline", "source-test-baseline.json")).read_text(encoding="utf-8"))
    try:
        evidence = build_hardgate_evidence(
            args.candidate_root,
            config=config,
            observations=observations,
            observation_errors=contract_errors,
            receipts=[
                {"schema_version": "migration-execution-receipt:v1", "kind": "environment-build", "returncode": build.returncode, "timed_out": False},
                {"schema_version": "migration-execution-receipt:v1", "kind": "version-probe", "returncode": inspected.returncode, "timed_out": False},
                {"schema_version": "migration-execution-receipt:v1", "kind": "target-execution", "returncode": run.returncode, "timed_out": False, "failed_phase": None if report_present else "no-report", "report_present": report_present, "report_missing": not report_present},
                {"schema_version": "migration-execution-receipt:v1", "kind": "report", "returncode": 0 if report_present else None, "timed_out": False, "failed_phase": None if report_present else "no-report", "report_present": report_present, "valid": report_present and not contract_errors},
            ],
            patch_applicable=True,
            source_test_baseline=baseline,
        )
    except ConfigurationError as exc:
        result = {
            "schema_version": "migration-verifier-result:v2",
            "status": "inconclusive",
            "hardgate": "INCONCLUSIVE",
            "cheating": False,
            "cheat_level": "L0",
            "components": {},
            "test_progress": None,
            "eligible_for_graded": False,
            "observation_contract_errors": [str(exc)],
            "observations": observations,
            "receipts": {"image_build_returncode": build.returncode, "container_returncode": run.returncode},
        }
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        return 1
    hardgate = evaluate_hardgate(args.candidate_root, evidence)
    units = json.loads((bundle / "assets" / config["assets"]["migration_units"]).read_text(encoding="utf-8")).get("units", [])
    try:
        declaration = resolve_state_applicability(
            policy=config.get("policy", {}),
            group_applicability=config.get("group_applicability"),
            contract=load_migration_contract(config, bundle),
        )
        state_applicability = declaration["applicable"]
        state_sources = declaration["sources"]
    except ApplicabilityError as exc:
        state_applicability = None
        state_sources = {"error": str(exc)}
    result = evaluate_verifier(hardgate=hardgate, migration_units=units, unit_checks=config.get("migration_unit_checks"), p2p=by_kind["P2P"], f2p=by_kind["F2P"], differential=by_kind["DIFFERENTIAL"], state={"applicable": state_applicability, "checks": by_kind["STATE"], "applicability": state_sources}, weights=config["weights"], applicability=config.get("group_applicability"), candidate_root=args.candidate_root)
    result["state_applicability"] = state_sources
    result["cheating"] = bool(hardgate.get("cheating"))
    result["cheat_level"] = str(hardgate.get("cheat_level", "L0"))
    if result.get("hardgate") != "PASS":
        result["reward_override"] = hardgate.get("reward_override")
    if contract_errors:
        result["status"] = "inconclusive"
        result["eligible_for_graded"] = False
        result["test_progress"] = None
    result["observation_contract_errors"] = list(contract_errors)
    result["observations"] = observations
    result["receipts"] = {"image_build_returncode": build.returncode, "container_returncode": run.returncode}
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result["status"] == "success" else 1
if __name__ == "__main__": raise SystemExit(main())
