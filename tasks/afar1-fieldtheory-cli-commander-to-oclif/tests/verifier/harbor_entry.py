"""Direct Harbor verifier entry; this module never starts Docker."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .comparison import cli_contract_projection, merge_comparison_status, status_only_projection
from .dependency_probe import probe_dependencies
from .evidence import (
    build_hardgate_evidence,
    changed_paths_from_patch,
    changed_paths_from_patch_file,
)
from .hardgate_engine import evaluate_hardgate
from .observation_adapter import GenericObservationAdapter
from .observations import (
    load_report_selection,
    project_report,
    validate_source_baseline_provenance,
)
from .verifier_engine import evaluate_verifier
from .reward import calculate_reward
from .integrity import validate_public_integrity, validate_public_projection_contract
from .sandbox import CANDIDATE_UID, check_sandbox_tools, sandbox_command


def _load_source_baseline(
    bundle_assets: Path, config: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Load the frozen source baseline and validate its provenance block."""

    path = bundle_assets / "source-baseline-outputs.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, ["source baseline outputs are unreadable"]
    if not isinstance(payload, Mapping):
        return {}, ["source baseline outputs are not an object"]
    _, errors = validate_source_baseline_provenance(payload, config)
    return dict(payload), errors


_REWARD_MODES = ("VERIFIER_ONLY",)

# Candidate repository root is exposed under every conventional environment
# name a model-authored harness may read.  Calibration executes the candidate at
# the runtime workdir and therefore also exports the same aliases (see
# calibration.oracle), so an authored harness resolves the real candidate in
# Harbor instead of the pristine repository embedded at /workspace/repo.
REPO_ROOT_ENVIRONMENT_KEYS = (
    "MIGRATION_REPO_ROOT",
    "MIGRATION_REPO",
    "REPO_ROOT",
    "REPO",
)


def repo_root_environment(repo_root: str) -> dict[str, str]:
    return {key: repo_root for key in REPO_ROOT_ENVIRONMENT_KEYS}


PUBLIC_PROJECTION_SCHEMA = "migration-public-projection:v1"
PUBLIC_REPORTS = {
    "ctrf": "/logs/verifier/ctrf.json",
    "verifier": "/logs/verifier/verifier-result.json",
    "reward": "/logs/verifier/reward.json",
    "reward_details": "/logs/verifier/reward-details.json",
}


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
    unprivileged: bool = False,
    assets: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {}
    if unprivileged and os.name != "nt":
        if os.geteuid() != 0:
            raise OSError("Harbor evaluator must start candidate commands as root")
        mode = _candidate_isolation_mode()
        if mode == "mount":
            command = (
                _harness_isolated_command(command, cwd, assets)
                if assets is not None
                else _isolated_command(command, cwd)
            )
        elif mode == "netns":
            command = sandbox_command(command)
        else:
            command = [sys.executable, "-c", _SETUID_SCRIPT, str(cwd), *command]
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
        env={**environment},
        **kwargs,
    )


_ISOLATION_SCRIPT = r"""
import os, subprocess, sys
from pathlib import Path

candidate = Path(sys.argv[1])
hidden = candidate.parent / ("." + candidate.name + "-hidden")
hidden.mkdir(mode=0o700, exist_ok=True)
for target in ("/tests", "/logs"):
    subprocess.run(["mount", "--bind", str(hidden), target], check=True)
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
os.chdir(candidate)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""


def _isolated_command(command: list[str], candidate: Path) -> list[str]:
    return ["unshare", "-m", "--", sys.executable, "-c", _ISOLATION_SCRIPT, str(candidate), *command]


_HARNESS_ISOLATION_SCRIPT = r"""
import os, shutil, subprocess, sys, tempfile
from pathlib import Path

assets = Path(sys.argv[1]).resolve()
candidate = Path(sys.argv[2]).resolve()
command = sys.argv[3:]
stage = Path(tempfile.mkdtemp(prefix="harbor-assets-", dir="/tmp"))
try:
    shutil.copytree(assets, stage, dirs_exist_ok=True, symlinks=True)
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            raise SystemExit("candidate assets must not contain symlinks")
        executable = path.is_dir() or bool(path.stat().st_mode & 0o111)
        path.chmod(0o755 if executable else 0o644)
    # The authoritative asset tree stays root-only (0700).  The harness gets a
    # private tmpfs view so a candidate cannot read the same files by path.
    subprocess.run(["mount", "--make-rprivate", "/"], check=False)
    subprocess.run(["mount", "-t", "tmpfs", "tmpfs", str(assets)], check=True)
    os.chmod(assets, 0o755)
    shutil.copytree(stage, assets, dirs_exist_ok=True, symlinks=True)
    # copytree restores the staged metadata, so the private 0700 root comes back
    # and the unprivileged candidate cannot traverse the asset tree.  Re-apply
    # the visible modes inside this throwaway tmpfs view only; the authoritative
    # asset root keeps its root-only permissions.
    os.chmod(assets, 0o755)
    for path in sorted(assets.rglob("*")):
        if path.is_symlink():
            continue
        executable = path.is_dir() or bool(path.stat().st_mode & 0o111)
        path.chmod(0o755 if executable else 0o644)
finally:
    shutil.rmtree(stage, ignore_errors=True)
hidden = candidate.parent / ("." + candidate.name + "-hidden")
hidden.mkdir(mode=0o700, exist_ok=True)
for target in ("/tests", "/logs"):
    subprocess.run(["mount", "--bind", str(hidden), target], check=True)
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
os.chdir(candidate)
os.execvpe(command[0], command, os.environ)
"""


def _harness_isolated_command(
    command: list[str], candidate: Path, assets: Path,
) -> list[str]:
    return [
        "unshare", "-m", "--", sys.executable, "-c",
        _HARNESS_ISOLATION_SCRIPT, str(assets), str(candidate), *command,
    ]


_SETUID_SCRIPT = r"""
import os, sys
from pathlib import Path

candidate = Path(sys.argv[1])
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
os.chdir(candidate)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""


def _candidate_isolation_mode() -> str:
    """Select how candidate commands are dropped from the verifier process.

    ``mount`` is the strict default: candidate commands run in a mount namespace
    that hides ``/tests`` and ``/logs``.  ``setuid`` is an explicit opt-out for
    Harbor deployments that cannot grant the verifier ``CAP_SYS_ADMIN``; it still
    drops to uid 1000 with a scrubbed environment and relies on the ``go-rwx``
    permissions of ``/tests``.  Any other value is rejected.
    """

    mode = os.environ.get("MIGRATION_CANDIDATE_ISOLATION", "mount").strip().lower()
    if mode not in {"mount", "netns", "setuid"}:
        raise OSError(
            "MIGRATION_CANDIDATE_ISOLATION must be mount, netns or setuid"
        )
    return mode



def _run_with_receipt(
    command: list[str],
    *,
    kind: str,
    index: int,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
    unprivileged: bool = False,
    assets: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str] | None, dict[str, object]]:
    receipt: dict[str, object] = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": kind,
        "index": index,
        "command": list(command),
        "returncode": None,
        "timed_out": False,
    }
    try:
        completed = _run(
            command,
            cwd=cwd,
            timeout=timeout,
            environment=environment,
            unprivileged=unprivileged,
            assets=assets,
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
    if completed.returncode != 0:
        receipt["stdout"] = (completed.stdout or "")[-2000:]
        receipt["stderr"] = (completed.stderr or "")[-2000:]
    return completed, receipt


def _independent_compile(runtime: Mapping[str, object]) -> list[str] | None:
    """Return a direct compiler invocation for the declared toolchain.

    The build gate must not be satisfiable by the candidate's own build script,
    which it can rewrite.  Only toolchains whose compiler is shipped in the
    repository are handled; anything else keeps the receipt-only behaviour.
    """

    technology = runtime.get("technology")
    if not isinstance(technology, Mapping):
        return None
    toolchain = technology.get("toolchain")
    name = ""
    if isinstance(toolchain, Mapping):
        name = str(toolchain.get("name", "")).strip().casefold()
    if name in {"typescript", "tsc"}:
        # ``--noEmit`` keeps the check side-effect free: the goal is "does this
        # tree compile", not "produce an artifact".
        return [
            "node",
            "node_modules/typescript/bin/tsc",
            "-p",
            "tsconfig.json",
            "--noEmit",
        ]
    return None


_COMPILER_TOKENS = (
    "tsc",
    "typescript/bin/tsc",
    "tsc.js",
    "webpack",
    "esbuild",
    "rollup",
    "vite build",
    "swc",
    "babel",
)


def _build_receipt_is_trustworthy(root: Path) -> bool:
    """Decide whether the build receipt may be taken at face value.

    Trusted when the declared build script actually invokes a compiler *and* the
    build output is newer than every source file.  Otherwise the tree may have
    been built by a no-op script in front of a stale ``dist/``, and the
    independent compile must run.
    """

    package = root / "package.json"
    try:
        payload = json.loads(package.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return False
    scripts = payload.get("scripts") if isinstance(payload, Mapping) else None
    if not isinstance(scripts, Mapping):
        return False
    build = scripts.get("build")
    if not isinstance(build, str) or not any(token in build for token in _COMPILER_TOKENS):
        return False
    outputs = [root / "dist", root / "build", root / "lib", root / "out"]
    existing = [path for path in outputs if path.is_dir()]
    if not existing:
        return False
    newest_source = 0.0
    for pattern in ("src/**/*.ts", "src/**/*.tsx", "src/**/*.js", "src/**/*.mjs"):
        for path in root.glob(pattern):
            try:
                newest_source = max(newest_source, path.stat().st_mtime)
            except OSError:
                continue
    oldest_output = min(
        (
            path.stat().st_mtime
            for directory in existing
            for path in directory.rglob("*")
            if path.is_file()
        ),
        default=0.0,
    )
    return bool(newest_source and oldest_output and oldest_output >= newest_source)


def _run_declared_commands(
    commands: Sequence[tuple[str, Sequence[str]]],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
    unprivileged: bool = False,
    verify_build: Sequence[str] | None = None,
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
            unprivileged=unprivileged,
        )
        receipts.append(receipt)
        if completed is None:
            return receipts, False, True
        if completed.returncode != 0:
            if kind == "target-native-test":
                # A suite failure is not a compiler failure. Keep collecting
                # observations; build_hardgate_evidence retains this receipt in
                # the final regression gate rather than silently discarding it.
                continue
            return receipts, True, False
        if kind == "target-build" and verify_build:
            # The build script is candidate-controlled.  Its success receipt is
            # only worth trusting when the script really invoked a compiler and
            # the emitted output is newer than the sources; otherwise the tree
            # may have been "built" by a no-op in front of a stale dist, and the
            # compiler is run directly.  Doing this lazily keeps the cost off the
            # normal path, which matters because the evaluation budget is
            # measured in tens of seconds.
            if _build_receipt_is_trustworthy(cwd):
                continue
            verified, compile_receipt = _run_with_receipt(
                [str(item) for item in verify_build],
                kind="target-compile",
                index=len(receipts) + 1,
                cwd=cwd,
                timeout=timeout,
                environment=environment,
                unprivileged=unprivileged,
            )
            receipts.append(compile_receipt)
            if verified is None:
                return receipts, False, True
            if verified.returncode != 0:
                return receipts, True, False
    return receipts, False, False


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _dependency_receipt(
    candidate: Path,
    config: Mapping[str, object],
    *,
    index: int,
) -> dict[str, object]:
    """Run the offline dependency probe and turn it into its own receipt.

    The hard gate only accepts ``dependency_resolved`` from a
    ``dependency-resolution`` receipt whose ``dependency_report.ok`` is true, so
    the probe result travels with the receipt instead of being inferred from a
    build or an import that happened to work.
    """

    policy = config.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    settings = policy.get("dependency_probe")
    settings = settings if isinstance(settings, Mapping) else {}
    try:
        report = probe_dependencies(candidate, settings)
        returncode = 0 if report.get("ok") is True else 1
        error = None
    except (OSError, ValueError) as exc:
        report = {"ok": False, "errors": [f"dependency-probe-error: {type(exc).__name__}"]}
        returncode = 1
        error = str(exc)[-2000:]
    receipt: dict[str, object] = {
        "schema_version": "migration-execution-receipt:v1",
        "kind": "dependency-resolution",
        "index": index,
        "command": ["dependency-probe", "offline"],
        "returncode": returncode,
        "timed_out": False,
        "dependency_report": report,
    }
    if error:
        receipt["diagnostic"] = error
    return receipt


def _production_sink_baseline(assets: Path, config: Mapping[str, object]) -> dict[str, Any] | None:
    name = (config.get("assets") or {}).get("production_sink_baseline") if isinstance(config.get("assets"), Mapping) else None
    if not isinstance(name, str) or not name.strip():
        return None
    path = assets / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"production sink baseline is missing or unsafe: {name}")
    return _load_json(path)


def _test_baselines(
    assets: Path,
    config: Mapping[str, object],
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """Load the required (source) and reference (target) test baselines.

    The source baseline is the required one: it lists the tests the candidate
    must keep.  The target baseline only supplies the reference translation, so
    a file that exists only in the target solution (the added black-box CLI
    test) is never demanded from the candidate.
    """

    declared = config.get("assets") if isinstance(config.get("assets"), Mapping) else {}
    source_name = declared.get("source_test_baseline")
    target_name = declared.get("target_test_baseline")
    required: dict[str, Any] = {"files": {}, "marker_counts": {}}
    if isinstance(source_name, str) and source_name.strip():
        required = _load_json(assets / source_name)
    reference: dict[str, Any] | None = None
    if isinstance(target_name, str) and target_name.strip():
        path = assets / target_name
        if path.is_file() and not path.is_symlink():
            reference = _load_json(path)
    return required, reference, "source-test-baseline"


def _candidate_changed_paths(logs: Path) -> list[str] | None:
    """Read the candidate's change set from the collected model patch.

    Returns ``None`` when the patch is unavailable, which keeps synthetic callers
    working; in the harbor flow grader always writes it.
    """

    candidates = [
        Path(logs).parent / "artifacts" / "model.patch",
        Path("/logs/artifacts/model.patch"),
    ]
    for path in candidates:
        resolved = path
        if not resolved.is_file():
            # The patch may live next to the verifier output rather than under a
            # sibling ``artifacts`` directory.
            continue
        # Prefer git's own decoding: quoted/escaped paths (non-ASCII, spaces,
        # control characters) cannot make a changed file invisible.  The text
        # parser is the fallback when git is unavailable.
        decoded = changed_paths_from_patch_file(resolved)
        if decoded is not None:
            return decoded
        try:
            text = resolved.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if text.strip():
            return changed_paths_from_patch(text)
    return None


def _write_result(logs: Path, result: dict[str, object]) -> dict[str, object]:
    if not isinstance(result.get("reward"), Mapping):
        result["reward"] = calculate_reward(
            hardgate={
                "status": result.get("hardgate", "INCONCLUSIVE"),
                "cheating": bool(result.get("cheating")),
            },
            verifier=result,
        )
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
) -> dict[str, object]:
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": "infra_error" if infrastructure_error else "failed",
        "hardgate": "INFRA_ERROR" if infrastructure_error else "FAIL",
        "cheating": False,
        "components": {},
        "test_progress": None if infrastructure_error else {"T": 0.0},
        "observation_contract_errors": [],
        "observations": [],
        "candidate_execution_error": (
            "verifier command infrastructure failure"
            if infrastructure_error
            else "candidate prerequisite command failed"
        ),
        "receipts": {"commands": receipts, "harness_returncode": None},
    }


_STATIC_EVIDENCE_KEYS = (
    "anti_cheating_pass", "protected_paths_unchanged", "change_scope_respected",
    "tests_not_tampered", "no_new_skip_or_xfail", "no_deceptive_source_fallback",
    "public_contract_preserved", "source_hits",
)


def _static_cheating_result(
    *,
    candidate: Path,
    assets: Path,
    config: Mapping[str, object],
    receipts: list[dict[str, object]],
) -> dict[str, object] | None:
    """Classify cheating from the candidate tree even if execution failed.

    A candidate that introduces a runtime bridge must never be reported as an
    ordinary failure only because its own prerequisite commands also failed.
    The L1/L2 signals are derived from candidate files and frozen assets, so
    they stay available before the harness has produced observations.
    """

    try:
        test_baseline, reference_baseline, baseline_source = _test_baselines(assets, config)
        evidence = build_hardgate_evidence(
            candidate,
            config=config,
            observations=[],
            observation_errors=[],
            receipts=receipts,
            patch_applicable=True,
            source_test_baseline=test_baseline,
            test_baseline_source=baseline_source,
            reference_test_baseline=reference_baseline,
            production_sink_baseline=_production_sink_baseline(assets, config),
        )
        hardgate = evaluate_hardgate(candidate, evidence)
    except (OSError, ValueError):
        # Integrity scanning must never turn a failing run into an
        # infrastructure error; the ordinary failure result stays in force.
        return None
    # Integrity verdicts never depend on whether the candidate's own commands
    # succeeded, so a real tampering/bridge hit has to keep its level: dropping
    # it here rewrites L1 tampering into an ordinary build failure (0 instead of
    # -1).  L1 is only produced when the integrity scan actually hit (test
    # tampering, protected-path drift, anti-cheating violation); a plain build
    # failure with no hit cannot reach this branch because no atom fails.
    if hardgate.get("cheat_level") not in {"L1", "L2"}:
        return None
    detail = {key: evidence.get(key) for key in _STATIC_EVIDENCE_KEYS}
    detail["deceptive_fallback_hits"] = evidence.get("deceptive_fallback_hits")
    detail["runtime_bridge_hits"] = evidence.get("runtime_bridge_hits")
    detail["ordinary_residue_hits"] = evidence.get("ordinary_residue_hits")
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": "failed",
        "hardgate": "FAIL",
        "cheating": True,
        "cheat_level": hardgate.get("cheat_level"),
        "components": {},
        "test_progress": {"T": 0.0},
        "observation_contract_errors": [],
        "observations": [],
        "candidate_execution_error": "candidate prerequisite command failed",
        "hardgate_detail": detail,
        "receipts": {"commands": receipts, "harness_returncode": None},
    }


def _public_infrastructure_result(message: str) -> dict[str, object]:
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": "infra_error",
        "hardgate": "INFRA_ERROR",
        "cheating": False,
        "components": {},
        "test_progress": None,
        "observation_contract_errors": [str(message)[-2000:]],
        "observations": [],
        "receipts": {"commands": [], "harness_returncode": None},
    }


def _reward_mode(config: Mapping[str, object]) -> str:
    harbor = config.get("harbor")
    if isinstance(harbor, Mapping) and isinstance(harbor.get("reward_mode"), str):
        return str(harbor["reward_mode"])
    value = config.get("reward_mode")
    return str(value) if isinstance(value, str) else "VERIFIER_ONLY"


def _validate_public_contract(config: Mapping[str, object]) -> tuple[Mapping[str, object] | None, str | None]:
    value = config.get("public_contract")
    if not isinstance(value, Mapping):
        return None, "public projection contract is missing"
    if value.get("schema_version") != PUBLIC_PROJECTION_SCHEMA:
        return None, "public projection contract schema is invalid"
    if value.get("mode") not in _REWARD_MODES:
        return None, "public projection contract mode is invalid"
    if value.get("mode") != _reward_mode(config):
        return None, "public projection contract mode disagrees with Harbor config"
    if value.get("task_id") != config.get("task_id"):
        return None, "public projection task identity is invalid"
    if value.get("base_commit") != config.get("base_commit"):
        return None, "public projection base identity is invalid"
    if config.get("reports") != PUBLIC_REPORTS:
        return None, "public projection report paths are invalid"
    if value.get("candidate_root") != "/app" or value.get("assets_root") != "/tests/assets":
        return None, "public projection runtime roots are invalid"
    if value.get("verifier_root") != "/tests/verifier" or value.get("logs_root") != "/logs":
        return None, "public projection runtime roots are invalid"
    if not isinstance(value.get("validated"), bool) or not isinstance(value.get("usable_for_scoring"), bool):
        return None, "public projection validation status is invalid"
    if value.get("validated") != value.get("usable_for_scoring"):
        return None, "public projection validation status is inconsistent"
    if value.get("usable_for_scoring") and not value.get("validated"):
        return None, "public projection cannot be usable before validation"
    runtime_error = value.get("runtime_error")
    if value.get("usable_for_scoring") and runtime_error:
        return None, "usable public projection contains runtime_error"
    if not value.get("validated") and not isinstance(runtime_error, str):
        return None, "unvalidated public projection has no runtime_error"
    return value, None


def _apply_observation_contract_errors(
    result: dict[str, object],
    errors: Sequence[str],
) -> None:
    if errors:
        result["status"] = "infra_error"
        result["hardgate"] = "INFRA_ERROR"
        result["test_progress"] = None
    result["observation_contract_errors"] = list(errors)


def _asset_references(runtime: Mapping[str, object]) -> tuple[str, ...]:
    paths: set[str] = set()
    commands = [
        *runtime.get("build_commands", []),
        *runtime.get("native_test_commands", []),
        runtime.get("harness_command", []),
    ]
    for command in commands:
        if not isinstance(command, list):
            continue
        for value in command:
            match = re.search(r"\{assets\}/([^\s'\"{}]+)", str(value))
            if match:
                paths.add(match.group(1).replace("\\", "/"))
    return tuple(sorted(paths))


def _prepare_candidate_assets(
    bundle: Path,
    candidate: Path,
    runtime: Mapping[str, object],
    *,
    private: bool = False,
) -> Path:
    root = candidate.parent / f".{candidate.name}-assets"
    if root.exists():
        if root.is_symlink():
            raise ValueError("candidate asset directory is unsafe")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    references = set(_asset_references(runtime))
    for name in ("shared", "target", "fixtures"):
        if (bundle / "assets" / name).is_dir():
            references.add(name)
    for relative in sorted(references):
        source = bundle / "assets" / relative
        if source.is_symlink() or not source.exists():
            raise ValueError(f"public runtime asset is missing or unsafe: {relative}")
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)
    for path in sorted(root.rglob("*")):
        if private:
            if path.is_symlink():
                raise ValueError("candidate assets must not contain symlinks")
            path.chmod(0o700 if path.is_dir() or path.stat().st_mode & 0o111 else 0o600)
        elif path.is_dir():
            path.chmod(0o555)
        else:
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
    root.chmod(0o700 if private else 0o555)
    if private:
        for path in (root, *sorted(root.rglob("*"))):
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError("candidate assets are not private")
        _assert_candidate_uid_cannot_read(root)
    return root


def _assert_candidate_uid_cannot_read(root: Path) -> None:
    """Prove the runtime copy is unreadable to the dropped candidate uid."""

    if os.name == "nt" or os.geteuid() != 0 or shutil.which("setpriv") is None:
        return
    sentinel = next((path for path in root.rglob("*") if path.is_file()), None)
    if sentinel is None:
        return
    probe = subprocess.run(
        [
            "setpriv", "--reuid=1000", "--regid=1000", "--clear-groups",
            "test", "-r", str(sentinel),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode == 0:
        raise ValueError("candidate assets are readable by candidate uid 1000")


def _materialize_workspace(candidate: Path, runtime: Mapping[str, object]) -> None:
    """Copy declared dependencies into the candidate workspace before commands run."""

    materializations = runtime.get("workspace_materializations", [])
    if not isinstance(materializations, list):
        raise ValueError("public runtime workspace materializations are invalid")
    for item in materializations:
        if not isinstance(item, Mapping):
            raise ValueError("public runtime workspace materialization is invalid")
        mode = str(item.get("mode", "copy"))
        source = item.get("source")
        target_name = item.get("target")
        if mode not in {"copy", "symlink"} or not isinstance(source, str) or not source.strip():
            raise ValueError("public runtime workspace materialization is invalid")
        if (
            not isinstance(target_name, str)
            or not target_name.strip()
            or target_name.startswith("/")
            or ".." in target_name.split("/")
        ):
            raise ValueError("public runtime workspace materialization target is unsafe")
        source_path = Path(source)
        if not source_path.exists():
            # Shared-environment runs already carry the workspace dependencies.
            continue
        target = candidate / target_name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "symlink":
            os.symlink(source_path, target)
        elif source_path.is_dir():
            shutil.copytree(source_path, target, symlinks=True)
        else:
            shutil.copy2(source_path, target)
    storage = runtime.get("writable_storage", [])
    if isinstance(storage, list):
        for item in storage:
            if not isinstance(item, Mapping) or item.get("initialization") != "empty":
                continue
            target_name = item.get("target")
            if (
                isinstance(target_name, str)
                and target_name.strip()
                and not target_name.startswith("/")
                and ".." not in target_name.split("/")
            ):
                directory = candidate / target_name
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(0o777)


_PATH_REWRITE_SUFFIXES = frozenset(
    {".toml", ".json", ".cfg", ".ini", ".conf", ".sh", ".mjs", ".cjs", ".js", ".ts", ".tsx", ".py"}
)


def _rewrite_candidate_paths(candidate: Path, runtime: Mapping[str, object]) -> None:
    """Map authored-workdir paths into the private Harbor candidate copy."""

    authored = str(runtime.get("workdir", "/workspace/repo")).rstrip("/")
    if not authored:
        return
    candidate_text = str(candidate)
    for path in sorted(candidate.rglob("*")):
        if ".git" in path.parts:
            continue
        if path.is_symlink():
            try:
                target = os.readlink(path)
            except OSError:
                continue
            if target == authored or target.startswith(authored + "/"):
                path.unlink()
                os.symlink(candidate_text + target[len(authored):], path)
            continue
        if not path.is_file() or path.suffix.casefold() not in _PATH_REWRITE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096] or authored.encode() not in raw:
            continue
        text = raw.decode("utf-8", "replace")
        rewritten = text.replace(authored, candidate_text)
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")


def _make_candidate_writable(candidate: Path) -> None:
    candidate.chmod(0o777)
    if os.geteuid() == 0:
        os.chown(candidate, CANDIDATE_UID, CANDIDATE_UID)
    for path in sorted(candidate.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.name == ".harbor-assets" or ".harbor-assets" in path.parts:
            continue
        if path.is_symlink():
            continue
        if os.geteuid() == 0:
            os.chown(path, CANDIDATE_UID, CANDIDATE_UID, follow_symlinks=False)
        if path.is_dir():
            path.chmod(0o777)
        elif path.is_file():
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(0o777 if executable else 0o666)


def _candidate_placeholders(candidate: Path, assets: Path) -> dict[str, str]:
    return {
        "repo": str(candidate),
        "assets": str(assets),
        "report": str(candidate / ".harbor-result.json"),
    }


def _require_isolation(bundle: Path) -> None:
    if os.name == "nt":
        return
    if os.geteuid() != 0:
        raise OSError("Harbor evaluator requires root to drop candidate privileges")
    if not (bundle / "config.json").is_file():
        raise OSError("Harbor public config is unavailable")
    mode = _candidate_isolation_mode()
    if mode == "netns":
        # Candidate and deterministic commands run in a private network/PID/mount
        # namespace (see verifier/sandbox.py), so an interface on the verifier
        # container is fine.
        tools = check_sandbox_tools()
        if tools.get("success") is not True:
            raise OSError(str(tools.get("summary", "candidate sandbox is unavailable")))
        return
    if mode == "setuid":
        # Shared-environment verifier: candidate commands run as uid 1000 with a
        # scrubbed environment; the agent environment may carry an interface, so
        # the mount namespace and network-device preconditions are not enforced.
        try:
            os.chmod("/logs", 0o700)
        except OSError:
            pass
        return
    devices = {entry.name for entry in Path("/sys/class/net").iterdir()}
    if devices != {"lo"}:
        raise OSError("Harbor candidate requires network_mode none")
    probe = subprocess.run(
        ["unshare", "-m", "--", "mount", "--bind", "/tmp", "/tmp"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if probe.returncode != 0:
        raise OSError("Harbor mount namespace isolation is unavailable")


_COLLECTOR_MASK_SCRIPT = r"""
import os, subprocess, sys
from pathlib import Path

assets = Path(sys.argv[1])
cwd = Path(sys.argv[2])
command = sys.argv[3:]
if assets.exists():
    # The expected observations live under this tree.  The collector runs the
    # project's own test runner, i.e. candidate-controlled code, so that code
    # must not be able to read the answers: mask the tree in this private mount
    # namespace before dropping privileges.  The harness command itself keeps
    # full access in its own namespace.
    subprocess.run(["mount", "-t", "tmpfs", "tmpfs", str(assets)], check=False)
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
os.chdir(cwd)
os.execvpe(command[0], command, os.environ)
"""


def _collector_command(
    command: Sequence[str], cwd: Path, assets: Path | None
) -> list[str]:
    """Run candidate test code with the answer assets masked out."""

    args = [str(item) for item in command]
    if os.name == "nt" or os.geteuid() != 0 or assets is None:
        return args
    return [
        "unshare",
        "-m",
        "--",
        sys.executable,
        "-c",
        _COLLECTOR_MASK_SCRIPT,
        str(assets),
        str(cwd),
        *args,
    ]


class _LocalCollectorExecutor:
    """Run the deterministic native-test collector without Docker.

    The verifier executes inside the verifier container, so the collector's
    container executor is replaced by a local subprocess runner: the adapter
    command (mocha/vitest/pytest JSON reporter) is rewritten to write its report
    under the evidence directory.
    """

    def __init__(self, environment: Mapping[str, str]) -> None:
        self._environment = dict(environment)
        self.last: dict[str, object] = {}

    def run(  # noqa: PLR0913 - mirrors ContainerExecutor.run
        self,
        image: object,
        *,
        input_root: Path,
        assets_root: Path,
        evidence_root: Path,
        command: Sequence[str],
        environment: Mapping[str, str] | None = None,
        network: str = "none",
        writable_input: bool = True,
        timeout: float | None = None,
        workspace_materializations: Sequence[object] = (),
    ) -> object:
        del image, network, writable_input, workspace_materializations
        root = Path(input_root)
        evidence = Path(evidence_root)
        assets = Path(assets_root)
        evidence.mkdir(parents=True, exist_ok=True)
        args = [str(argument).replace("/logs/", str(evidence) + "/") for argument in command]
        # The collector executes the project's own test runner, so candidate test
        # code must not inherit the verifier's root identity: the normal command
        # path drops to CANDIDATE_UID, and this adapter has to do the same or a
        # test can read root-only assets through it.  The report directory lives
        # under root-owned ``logs``, so it is opened up for the dropped uid.
        if os.name != "nt" and os.geteuid() == 0:
            os.chmod(evidence, 0o777)
            args = _collector_command(args, root, assets if assets.exists() else None)
        env = {**os.environ, **self._environment, **dict(environment or {})}
        try:
            completed = subprocess.run(
                args,
                cwd=str(root),
                env=env,
                timeout=float(timeout) if timeout else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            self.last = {"command": args, "timeout": True, "error": str(exc)}
            raise
        except OSError as exc:
            self.last = {"command": args, "error": f"{type(exc).__name__}: {exc}"}
            raise
        self.last = {
            "command": args,
            "returncode": completed.returncode,
            "stderr_tail": (completed.stderr or "")[-1500:],
            "stdout_tail": (completed.stdout or "")[-500:],
        }
        return None


def _merge_native_p2p_observations(
    *,
    observations: list[object],
    config: Mapping[str, object],
    runtime: Mapping[str, object],
    candidate: Path,
    assets: Path,
    logs: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> list[object]:
    """Override native-test P2P rows with the deterministic collector.

    The frozen source baseline uses the collector's canonical P2P shape, so the
    candidate must be evaluated the same way; this keeps Harbor evaluation
    independent of the model harness's native P2P projection.
    """

    try:
        from .native_test_collector import (
            collect_native_test_results,
            merge_role_observation_rows,
            native_test_targets,
        )
    except ImportError:  # pragma: no cover - flat verifier layout
        try:
            from native_test_collector import (  # type: ignore[no-redef]
                collect_native_test_results,
                merge_role_observation_rows,
                native_test_targets,
            )
        except ImportError:
            _write_native_merge_diagnostic(logs, {"route": "import-failed"})
            return observations
    tests = config.get("tests")
    baseline = _load_json(assets / "source-baseline-outputs.json")
    _, provenance_errors = validate_source_baseline_provenance(baseline, config)
    if provenance_errors:
        _write_native_merge_diagnostic(
            logs, {"route": "source-provenance-invalid", "errors": list(provenance_errors)}
        )
    baseline_rows = baseline.get("observations")
    p2p_rows: list[dict[str, object]] = []
    if isinstance(baseline_rows, list):
        for row in baseline_rows:
            if not isinstance(row, Mapping) or str(row.get("kind")) != "P2P":
                continue
            value = row.get("value")
            inner = value.get("value") if isinstance(value, Mapping) else None
            file = inner.get("file") if isinstance(inner, Mapping) else None
            title = inner.get("title") if isinstance(inner, Mapping) else None
            if isinstance(file, str) and file and isinstance(title, str) and title:
                p2p_rows.append(
                    {
                        "id": str(row.get("id")),
                        "kind": "P2P",
                        "coverage": {"surface": f"test:{file}#{title}"},
                    }
                )
    if not p2p_rows and isinstance(tests, Mapping) and isinstance(tests.get("p2p"), list):
        p2p_rows = [dict(row) for row in tests["p2p"] if isinstance(row, Mapping)]
    if not p2p_rows:
        _write_native_merge_diagnostic(logs, {"route": "no-p2p-rows"})
        return observations

    class _Inventory:
        def __init__(self, rows: Sequence[object]) -> None:
            self._rows = tuple(row for row in rows if isinstance(row, Mapping))
            self.p2p = self._rows

        def all_observations(self) -> tuple[Mapping[str, object], ...]:
            return self._rows

    inventory = _Inventory(p2p_rows)
    executor = _LocalCollectorExecutor(environment)
    diagnostic: dict[str, object] = {"route": "merge"}
    try:
        targets = native_test_targets(inventory)
        diagnostic["targets"] = len(targets)
        if not targets:
            diagnostic["route"] = "no-targets"
            _write_native_merge_diagnostic(logs, diagnostic)
            return observations
        collection = collect_native_test_results(
            role="target",
            runtime=runtime,
            repo_root=candidate,
            executor=executor,
            image=None,
            assets_root=assets,
            evidence_root=logs / "native",
            timeout=timeout,
            targets=targets,
        )
        diagnostic.update(
            {
                "adapter": collection.adapter,
                "available": collection.available,
                "parsed_count": collection.parsed_count,
                "collector": executor.last,
                "collection_diagnostics": list(collection.diagnostics)[:8],
            }
        )
        merged, _summary = merge_role_observation_rows(
            inventory=inventory,
            role="target",
            model_observations=[row for row in observations if isinstance(row, Mapping)],
            collection=collection,
        )
    except Exception as exc:  # pragma: no cover - keep the harness report on failure
        diagnostic["route"] = "exception"
        diagnostic["error"] = f"{type(exc).__name__}: {exc}"
        diagnostic["collector"] = executor.last
        _write_native_merge_diagnostic(logs, diagnostic)
        return observations
    diagnostic["merged"] = len(merged)
    _write_native_merge_diagnostic(logs, diagnostic)
    return [dict(row) for row in merged]


def _write_native_merge_diagnostic(logs: Path, diagnostic: Mapping[str, object]) -> None:
    try:
        (logs / "native-merge.json").write_text(
            json.dumps(dict(diagnostic), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:  # pragma: no cover - diagnostics are best effort
        pass


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
    # The verifier container may receive an empty bind-mounted /logs directory.
    # Create the report directory before the candidate harness writes or the
    # entry copies its report into it; otherwise a valid run fails as an
    # infrastructure error.
    logs.mkdir(parents=True, exist_ok=True)
    if not candidate.is_dir() or candidate.is_symlink():
        raise ValueError("Harbor candidate /app is missing or unsafe")
    config = _load_json(bundle / "config.json")
    harbor = config.get("harbor")
    reward_mode = _reward_mode(config)
    if reward_mode not in _REWARD_MODES:
        raise ValueError("Harbor verifier reward_mode is unsupported")
    if isinstance(harbor, Mapping) and harbor.get("reward_mode") not in _REWARD_MODES:
        raise ValueError("Harbor verifier reward_mode is unsupported")
    assets = bundle / "assets"
    config.setdefault("policy", {})
    public_runtime = isinstance(config.get("runtime"), Mapping)
    runtime = config.get("runtime")
    if not isinstance(runtime, Mapping):
        # Explicit legacy read support for internal unit fixtures only.  New
        # public bundles always use the top-level projected runtime contract.
        target = config.get("target_environment", {})
        runtime = target.get("runtime", {}) if isinstance(target, Mapping) else {}
    if not isinstance(runtime, Mapping):
        raise ValueError("public Harbor runtime contract is missing")
    if public_runtime:
        public_contract, contract_error = _validate_public_contract(config)
        if contract_error:
            return _write_result(logs, _public_infrastructure_result(contract_error))
        try:
            validate_public_integrity(bundle)
            validate_public_projection_contract(bundle, public_contract)
            _require_isolation(bundle)
        except (OSError, ValueError) as exc:
            return _write_result(logs, _public_infrastructure_result(str(exc)))
        runtime_error = public_contract.get("runtime_error") if public_contract else None
        if runtime_error:
            return _write_result(logs, _public_infrastructure_result(str(runtime_error)))
    timeout = float(runtime.get("timeout_seconds", 1800))
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "TMPDIR": "/tmp"}
    # The candidate is scored from a private copy (``candidate``), not from the
    # image workdir the runtime was authored against.  Rewrite any declared
    # environment value that points inside that workdir (for example a build or
    # module cache under the repository root) so it resolves to the real
    # candidate, exactly where the verifier materialized the dependencies.
    authored_workdir = str(runtime.get("workdir", "")).rstrip("/")
    candidate_root = str(candidate)
    for key, value in runtime.get("environment", {}).items():
        text = str(value)
        if authored_workdir and (
            text == authored_workdir or text.startswith(authored_workdir + "/")
        ):
            text = candidate_root + text[len(authored_workdir):]
        environment[str(key)] = text
    # The verifier image embeds the pristine repository at the runtime workdir so
    # the candidate can be reconstructed.  Generated harness assets prefer that
    # embedded checkout, so point every conventional repo-root variable at the
    # private candidate copy explicitly.
    environment.update(repo_root_environment(str(candidate)))
    placeholder_values = _candidate_placeholders(candidate, assets)
    commands = [
        *(("target-build", raw) for raw in runtime.get("build_commands", [])),
        # The declared build script is candidate-controlled, so the compiler is
        # invoked directly when the build receipt cannot be trusted (see
        # ``verify_build`` below).  That check is lazy on purpose: a full
        # ``tsc --noEmit`` costs tens of seconds and the evaluation budget is
        # measured in tens of seconds, so a normal migration must not pay it.
        *(("target-native-test", raw) for raw in runtime.get("native_test_commands", [])),
    ]
    receipts: list[dict[str, object]] = []
    if apply_failed:
        result = {
            "schema_version": "migration-verifier-result:v2",
            "status": "failed",
            "hardgate": "FAIL",
            "cheating": False,
            "components": {},
            "test_progress": {"T": 0.0},
                "observation_contract_errors": [],
            "observations": [],
            "candidate_patch_error": patch_message,
            "receipts": {"commands": [], "harness_returncode": None},
        }
        return _write_result(logs, result)
    harness_assets: Path | None = None
    if public_runtime:
        try:
            private_assets = _candidate_isolation_mode() == "mount"
            candidate_assets = _prepare_candidate_assets(
                bundle, candidate, runtime, private=private_assets,
            )
            _materialize_workspace(candidate, runtime)
            _rewrite_candidate_paths(candidate, runtime)
            # Materialized dependencies are copied as root, so run this after the
            # copy to let the dropped-privilege candidate write build/test caches.
            _make_candidate_writable(candidate)
        except (OSError, ValueError) as exc:
            return _write_result(logs, _public_infrastructure_result(str(exc)))
        candidate_report = candidate / ".harbor-result.json"
        placeholder_values = _candidate_placeholders(candidate, candidate_assets)
        if private_assets:
            harness_assets = candidate_assets
    else:
        candidate_report = logs / "result.json"
        placeholder_values["report"] = str(candidate_report)
    rendered_commands = [
        (phase, [str(item).format_map(placeholder_values) for item in raw])
        for phase, raw in commands
    ]
    receipts, candidate_failed, infrastructure_failed = _run_declared_commands(
        rendered_commands,
        cwd=candidate,
        timeout=timeout,
        environment=environment,
        unprivileged=public_runtime,
        verify_build=_independent_compile(runtime),
    )
    if candidate_failed or infrastructure_failed:
        cheating_result = (
            _static_cheating_result(
                candidate=candidate,
                assets=assets,
                config=config,
                receipts=receipts,
            )
            if not infrastructure_failed
            else None
        )
        if cheating_result is not None:
            return _write_result(logs, cheating_result)
        return _write_result(
            logs,
            _execution_failure_result(
                receipts=receipts,
                infrastructure_error=infrastructure_failed,
            ),
        )
    harness = runtime.get("harness_command")
    if not isinstance(harness, list) or not harness:
        return _write_result(logs, _public_infrastructure_result("target runtime harness command is missing"))
    command = [str(item).format_map(placeholder_values) for item in harness]
    completed, harness_receipt = _run_with_receipt(
        command,
        kind="target-execution",
        index=len(receipts) + 1,
        cwd=candidate,
        timeout=timeout,
        environment=environment,
        unprivileged=public_runtime,
        assets=harness_assets,
    )
    if completed is None:
        receipts.append(harness_receipt)
        return _write_result(
            logs,
            _execution_failure_result(
                receipts=receipts,
                infrastructure_error=True,
            ),
        )
    report = logs / "result.json"
    harness_receipt["report_present"] = candidate_report.is_file()
    receipts.append(harness_receipt)
    # Dependency resolution is proven by its own offline probe receipt, never by
    # the build or the harness having succeeded.  It is appended after the
    # harness so the receipt order mirrors execution order, but the hard gate
    # only reads it by kind.
    receipts.append(_dependency_receipt(candidate, config, index=len(receipts) + 1))
    if not candidate_report.is_file():
        if completed.returncode != 0:
            result = {
                "schema_version": "migration-verifier-result:v2",
                "status": "failed",
                "hardgate": "FAIL",
                "cheating": False,
                "components": {},
                "test_progress": {"T": 0.0},
                        "observation_contract_errors": [],
                "observations": [],
                "candidate_harness_error": "harness exited without an observations report",
                "receipts": {"commands": receipts, "harness_returncode": completed.returncode},
            }
            return _write_result(logs, result)
        raise ValueError("Harbor candidate harness did not produce /logs/verifier/result.json")
    if public_runtime:
        shutil.copyfile(candidate_report, report)
    payload = _load_json(report)
    adapter = GenericObservationAdapter()
    payload = adapter.normalize_report(
        payload,
        role="candidate",
        returncode=completed.returncode,
        report_path="/logs/verifier/result.json",
        config=config,
    )
    payload, projection_errors = project_report(
        payload, load_report_selection(bundle / "assets")
    )
    # Split the two error families: projection/validation problems come from the
    # candidate's own report and make the *candidate* invalid (ordinary failure,
    # reward 0); source-provenance problems are the evaluation's own assets and
    # stay an infrastructure error.
    candidate_report_errors = tuple(projection_errors) + adapter.validate_report(
        payload, config, actual_returncode=completed.returncode
    )
    contract_errors = tuple(candidate_report_errors)
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
    observations = _merge_native_p2p_observations(
        observations=observations,
        config=config,
        runtime=runtime,
        candidate=candidate,
        assets=assets,
        logs=logs,
        timeout=timeout,
        environment=environment,
    )
    source_baseline, provenance_errors = _load_source_baseline(assets, config)
    infrastructure_report_errors: tuple[str, ...] = ()
    if provenance_errors:
        infrastructure_report_errors = tuple(provenance_errors)
        contract_errors = tuple(contract_errors) + tuple(provenance_errors)
        _write_native_merge_diagnostic(
            logs, {"route": "source-provenance-invalid", "errors": list(provenance_errors)}
        )
    baseline_rows = source_baseline.get("observations", [])
    baseline_by_id = {str(row.get("id")): row for row in baseline_rows if isinstance(row, Mapping)} if isinstance(baseline_rows, list) else {}
    compared: list[dict[str, object]] = []
    for row in observations:
        current = dict(row)
        baseline = baseline_by_id.get(str(current.get("id")))
        if current.get("kind") in {"P2P", "DIFFERENTIAL"} and baseline is not None:
            current["source_value"] = baseline.get("value")
            current["candidate_value"] = current.get("value")
            if current.get("kind") == "P2P" and (
                status_only_projection(baseline.get("value"))
                != status_only_projection(current.get("value"))
            ):
                # A status-only baseline and a concrete candidate projection
                # are not comparable by value.  Compare their status instead;
                # the certified baseline itself uses a status-only P2P shape.
                comparison_status = (
                    "PASS"
                    if str(baseline.get("status")) == str(current.get("status"))
                    else "FAIL"
                )
            else:
                comparison_status = adapter.compare(
                    cli_contract_projection(dict(baseline)),
                    cli_contract_projection(current),
                    config.get("normalization_policy", {}),
                ).status
            current["status"] = merge_comparison_status(
                str(current.get("status", "INCONCLUSIVE")),
                str(comparison_status),
            )
        compared.append(current)
    by_kind = {
        kind: [row for row in compared if row.get("kind") == kind]
        for kind in ("P2P", "F2P", "DIFFERENTIAL", "STATE")
    }
    test_baseline, reference_baseline, baseline_source = _test_baselines(assets, config)
    evidence = build_hardgate_evidence(
        candidate,
        config=config,
        observations=compared,
        observation_errors=contract_errors,
        receipts=receipts,
        patch_applicable=not apply_failed,
        report_valid=not candidate_report_errors,
        candidate_changed_paths=_candidate_changed_paths(logs),
        source_test_baseline=test_baseline,
        test_baseline_source=baseline_source,
        reference_test_baseline=reference_baseline,
        production_sink_baseline=_production_sink_baseline(assets, config),
    )
    hardgate = evaluate_hardgate(candidate, evidence)
    result_evidence = {
        key: evidence.get(key)
        for key in (
            "anti_cheating_pass", "protected_paths_unchanged", "change_scope_respected",
            "tests_not_tampered", "no_new_skip_or_xfail", "no_deceptive_source_fallback",
            "public_contract_preserved", "source_hits",
        )
    }
    result_evidence["deceptive_fallback_hits"] = evidence.get("deceptive_fallback_hits")
    result_evidence["ordinary_residue_hits"] = evidence.get("ordinary_residue_hits")
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
        state={"applicable": bool(config["policy"].get("state_applicable")), "checks": by_kind["STATE"], "roles": {str(row["id"]): row.get("state_role") for row in config.get("tests", {}).get("state", [])}},
        applicability=config.get("group_applicability"),
    )
    # Only the evaluation's own asset problems are infrastructure failures; an
    # invalid *candidate* report is a candidate failure and has already been
    # folded into ``candidate_valid`` above.
    _apply_observation_contract_errors(result, infrastructure_report_errors)
    result["reward"] = calculate_reward(
        hardgate=hardgate,
        verifier=result,
    )
    result["observations"] = compared
    result["hardgate_detail"] = result_evidence
    result["cheating"] = bool(hardgate.get("cheating"))
    if apply_failed:
        result["candidate_patch_error"] = patch_message
    result["receipts"] = {"commands": receipts, "harness_returncode": completed.returncode}
    logs.mkdir(parents=True, exist_ok=True)
    return _write_result(logs, result)


__all__ = ["REPO_ROOT_ENVIRONMENT_KEYS", "repo_root_environment", "run_harbor_verifier"]
