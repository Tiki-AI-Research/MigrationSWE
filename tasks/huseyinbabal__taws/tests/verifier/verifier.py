from __future__ import annotations
import argparse, hashlib, json, shlex, stat, subprocess, sys
sys.dont_write_bytecode = True
from pathlib import Path
from hardgate_engine import evaluate_hardgate
from comparison import merge_comparison_status
from observation_adapter import GenericObservationAdapter
from observations import normalize_observation_report, observation_contract_errors, load_report_selection, project_report
from evidence import build_hardgate_evidence
from verifier_engine import evaluate_verifier
from reward import calculate_verifier_reward

def _digest_tree(root):
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rows.append((path.relative_to(root).as_posix(), "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()))
    return "sha256:" + hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def _digest_file(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

def _merge_test_integrity_baselines(assets, config):
    """Merge the source and target test baselines for ``tests_not_tampered``.

    Source-baseline files existed in the task's starting repository and are
    instruction-protected (presence + content).  Target-only files exist only
    in the reference solution and are marked ``present_only``: absence is
    allowed and an independently authored same-named file is not tamper
    evidence -- a candidate cannot tamper with a test it never received.
    """
    asset_map = config.get("assets", {})
    asset_map = asset_map if isinstance(asset_map, dict) else {}
    source_name = asset_map.get("source_test_baseline")
    target_name = asset_map.get("target_test_baseline")
    source = json.loads((assets / str(source_name)).read_text(encoding="utf-8")) if source_name else {}
    target = json.loads((assets / str(target_name)).read_text(encoding="utf-8")) if target_name else {}

    def _files(doc):
        files = doc.get("files") if isinstance(doc, dict) else None
        return files if isinstance(files, dict) else {}

    merged = {}
    for relative, record in _files(source).items():
        if isinstance(record, dict):
            entry = dict(record)
            entry.pop("present_only", None)
            merged[str(relative)] = entry
    for relative, record in _files(target).items():
        key = str(relative)
        if key in merged or not isinstance(record, dict):
            continue
        entry = dict(record)
        entry["present_only"] = True
        merged[key] = entry

    def _counts(doc):
        counts = doc.get("marker_counts") if isinstance(doc, dict) else None
        return counts if isinstance(counts, dict) else {}

    source_counts = _counts(source)
    target_counts = _counts(target)
    marker_counts = {
        key: int(source_counts.get(key, 0) or 0) + int(target_counts.get(key, 0) or 0)
        for key in ("skip", "xfail", "disabled")
    }
    return {"files": merged, "marker_counts": marker_counts}

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
    try:
        build = run_docker_build(
            args.docker,
            [
                "--network", "host",
                *label_arguments(resource_labels("grader-runtime")),
                "--file", str(environment / "Dockerfile"),
                "--tag", image, str(environment),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        build = subprocess.CompletedProcess([], 125, "", str(exc))
    inspected = _run([args.docker, "image", "inspect", image, "--format", "{{.Id}}"], min(timeout, 60)) if build.returncode == 0 else subprocess.CompletedProcess([], 125, "", "image build failed")
    image_ref = inspected.stdout.strip() if inspected.returncode == 0 else ""
    if image_ref:
        record_current_resource(
            f"image:{image_ref}",
            kind="grader-runtime",
            metadata={"tags": [image]},
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = args.output.parent.resolve()
    evidence_mode = stat.S_IMODE(evidence.stat().st_mode)
    environment_args = []
    for key, value in sorted(config["target_environment"]["runtime"].get("environment", {}).items()):
        environment_args.extend(["--env", f"{key}={value}"])
    runtime_volume = f"migration-grader-runtime-{os.getpid()}-{int(time.time() * 1000)}"
    volume_create = _run([
        args.docker, "volume", "create",
        *label_arguments(resource_labels("grader-runtime")),
        runtime_volume,
    ], min(timeout, 60)) if image_ref else subprocess.CompletedProcess([], 125, "", "image build or inspection failed")
    if image_ref and volume_create.returncode == 0:
        record_current_resource(
            f"volume:{runtime_volume}",
            kind="volume",
            metadata={"volume": runtime_volume, "resource_kind": "grader-runtime"},
        )
    if image_ref and volume_create.returncode == 0:
        try:
            evidence.chmod(0o777)
            run = _run([
                args.docker, "run", "--rm", "--network", "none", "--read-only", "--cap-drop=ALL",
                "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,exec,nosuid,nodev",
                "--mount", f"type=bind,source={args.candidate_root.resolve()},target=/input/repo,readonly",
                "--mount", f"type=bind,source={(bundle / 'assets').resolve()},target=/assets,readonly",
                "--mount", f"type=bind,source={evidence},target=/logs",
                "--mount", f"type=volume,src={runtime_volume},target=/workspace/repo,volume-nocopy",
                *environment_args,
                image_ref, "sh", "-c", _workspace_script(config["target_environment"]["runtime"])
            ], timeout)
        finally:
            evidence.chmod(evidence_mode)
            removed = _run(
                [args.docker, "volume", "rm", runtime_volume], min(timeout, 60)
            )
            ledger = current_resource_ledger()
            if ledger is not None:
                if removed.returncode == 0:
                    ledger.record_deleted(
                        f"volume:{runtime_volume}",
                        detail={"volume": runtime_volume},
                    )
                else:
                    ledger.record_cleanup_failure(
                        f"volume:{runtime_volume}",
                        error=(
                            removed.stderr
                            or removed.stdout
                            or "volume rm failed"
                        ),
                    )
    elif image_ref:
        run = subprocess.CompletedProcess([], 125, "", "runtime workspace volume create failed")
    else:
        run = subprocess.CompletedProcess([], 125, "", "image build or inspection failed")
    report = evidence / "result.json"
    payload = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {"observations": []}
    adapter = GenericObservationAdapter()
    payload = adapter.normalize_report(payload, role="candidate", returncode=run.returncode, report_path="/logs/result.json", config=config)
    payload, projection_errors = project_report(payload, load_report_selection(bundle / "assets"))
    observations = payload.get("observations", [])
    contract_errors = adapter.validate_report(payload, config, actual_returncode=run.returncode)
    if projection_errors:
        contract_errors = (*contract_errors, *projection_errors)
    baseline_path = bundle / "assets" / "source-baseline-outputs.json"
    if not baseline_path.is_file():
        raise SystemExit("source baseline outputs are missing")
    baseline_rows = json.loads(baseline_path.read_text(encoding="utf-8")).get("observations", [])
    baseline_by_id = {str(row.get("id")): row for row in baseline_rows if isinstance(row, dict)}
    compared = []
    for row in observations:
        current = dict(row)
        baseline = baseline_by_id.get(str(current.get("id")))
        if current.get("kind") in {"P2P", "DIFFERENTIAL", "CONTRACT"} and baseline is not None:
            candidate_value = current.get("value")
            source_value = baseline.get("value")
            current["source_value"] = source_value
            current["candidate_value"] = candidate_value
            # DIFFERENTIAL source rows are the source technology's own
            # projection, not the target differential probe's value, so the
            # recorded source sentinel must not downgrade the candidate's probe
            # result (see harbor_entry for the same rule).
            if current.get("kind") != "DIFFERENTIAL":
                comparison_status = adapter.compare(
                    baseline,
                    current,
                    config.get("normalization_policy", {}),
                ).status
                current["status"] = merge_comparison_status(current.get("status", "INCONCLUSIVE"), comparison_status)
        compared.append(current)
    observations = compared
    by_kind = {kind: [row for row in observations if row.get("kind") == kind] for kind in ("P2P", "F2P", "DIFFERENTIAL", "STATE")}
    baseline = _merge_test_integrity_baselines(bundle / "assets", config)
    evidence = build_hardgate_evidence(
        args.candidate_root,
        config=config,
        observations=observations,
        observation_errors=contract_errors,
        receipts=[
            {"schema_version": "migration-execution-receipt:v1", "kind": "environment-build", "returncode": build.returncode, "timed_out": False},
            {"schema_version": "migration-execution-receipt:v1", "kind": "version-probe", "returncode": inspected.returncode, "timed_out": False},
            {"schema_version": "migration-execution-receipt:v1", "kind": "target-execution", "returncode": run.returncode, "timed_out": False, "report_present": report.is_file()},
            {"schema_version": "migration-execution-receipt:v1", "kind": "report", "returncode": 0 if report.is_file() else None, "timed_out": False, "valid": report.is_file() and not contract_errors},
        ],
        patch_applicable=True,
        source_test_baseline=baseline,
        test_baseline_source="source-test-baseline",
    )
    hardgate = evaluate_hardgate(args.candidate_root, evidence)
    units = json.loads((bundle / "assets" / config["assets"]["migration_units"]).read_text(encoding="utf-8")).get("units", [])
    result = evaluate_verifier(hardgate=hardgate, migration_units=units, unit_checks=config.get("migration_unit_checks"), p2p=by_kind["P2P"], f2p=by_kind["F2P"], differential=by_kind["DIFFERENTIAL"], state={"applicable": bool(config["policy"].get("state_applicable")), "checks": by_kind["STATE"], "roles": {str(row["id"]): row.get("state_role") for row in config.get("tests", {}).get("state", [])}}, weights=config["weights"], applicability=config.get("group_applicability"))
    if contract_errors:
        result["status"] = "inconclusive"
        result["eligible_for_graded"] = False
        result["test_progress"] = None
    result["reward"] = calculate_verifier_reward(
        hardgate=hardgate,
        verifier=result,
    )
    result["observation_contract_errors"] = list(contract_errors)
    result["observations"] = observations
    result["receipts"] = {"image_build_returncode": build.returncode, "container_returncode": run.returncode}
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result["status"] == "success" else 1
if __name__ == "__main__": raise SystemExit(main())
