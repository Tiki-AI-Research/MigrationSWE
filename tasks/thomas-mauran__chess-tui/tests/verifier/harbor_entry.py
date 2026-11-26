"""Direct Harbor verifier entry; this module never starts Docker."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .comparison import merge_comparison_status, status_only_projection
from .evidence import build_hardgate_evidence
from .hardgate_engine import evaluate_hardgate
from .observation_adapter import GenericObservationAdapter
from .observations import load_report_selection, project_report
from .verifier_engine import evaluate_verifier
from .reward import calculate_reward
from .integrity import validate_public_integrity, validate_public_projection_contract
from .sandbox import CANDIDATE_UID, check_sandbox_tools, sandbox_command
from .workspace_permissions import normalize_workspace_permissions, workspace_target
from .argv_template import render_runtime_argv

try:  # repository layout (tests / grader generation)
    from scripts.migration.grader.graded.objective.runner import score_candidate_objective
    from scripts.migration.grader.graded.rubric.gateway import HttpJudgeTransport
    from scripts.migration.grader.graded.rubric.judge_client import JudgeClient
    from scripts.migration.grader.graded.rubric.score import score_rubric
except ImportError:  # shipped verifier bundle layout (/tests/verifier/graded)
    try:
        from .graded.objective.runner import score_candidate_objective
        from .graded.rubric.gateway import HttpJudgeTransport
        from .graded.rubric.judge_client import JudgeClient
        from .graded.rubric.score import score_rubric
    except ImportError:  # verifier-only bundle without graded modules
        score_candidate_objective = None  # type: ignore[assignment]
        HttpJudgeTransport = None  # type: ignore[assignment]
        JudgeClient = None  # type: ignore[assignment]
        score_rubric = None  # type: ignore[assignment]


_REWARD_MODES = ("VERIFIER_ONLY", "GRADED")

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
    shutil.copytree(stage, assets, dirs_exist_ok=True, symlinks=True)
    # copytree copies the stage root's 0700 mode onto assets; restore traversal
    # for the unprivileged harness after the copy.
    os.chmod(assets, 0o755)
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


def _verifier_owned_command(receipt: Mapping[str, object]) -> bool:
    kind = str(receipt.get("kind") or "")
    if kind == "target-execution":
        return True
    command = receipt.get("command")
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or not command:
        return False
    executable = str(command[0]).replace("\\", "/")
    name = executable.rsplit("/", 1)[-1].casefold()
    if name.startswith("migration-harness"):
        return True
    return any(
        executable.startswith(prefix)
        for prefix in ("/tests/", "/opt/migration/", "/assets/")
    )


def _failure_payload(receipt: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(receipt, Mapping):
        return {}
    return {
        "kind": receipt.get("kind"),
        "command": receipt.get("command"),
        "returncode": receipt.get("returncode"),
        "timed_out": receipt.get("timed_out"),
        "stdout_tail": str(receipt.get("stdout") or "")[-4000:],
        "stderr_tail": str(receipt.get("stderr") or "")[-4000:],
        "verifier_owned": _verifier_owned_command(receipt),
    }


def _run_declared_commands(
    commands: Sequence[tuple[str, Sequence[str]]],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str],
    unprivileged: bool = False,
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
            if _verifier_owned_command(receipt):
                return receipts, False, True
            return receipts, True, False
    return receipts, False, False


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


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
    failure = _failure_payload(receipts[-1] if receipts else None)
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
        "failure": failure,
        "candidate_execution_error": (
            "verifier command infrastructure failure"
            if infrastructure_error
            else "candidate prerequisite command failed"
        ),
        "receipts": {"commands": receipts, "harness_returncode": None},
    }


def _public_infrastructure_result(
    message: str,
    *,
    failure: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "migration-verifier-result:v2",
        "status": "infra_error",
        "hardgate": "INFRA_ERROR",
        "cheating": False,
        "components": {},
        "test_progress": None,
        "eligible_for_graded": False,
        "observation_contract_errors": [str(message)[-2000:]],
        "observations": [],
        "failure": dict(failure or {}),
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


def _public_runtime_error(runtime: Mapping[str, object]) -> str | None:
    """Validate the materialized runtime rather than a stale projection status.

    A delivered public projection can carry an ``INVALID_EVALUATOR`` marker that
    is stale relative to the runtime contract actually shipped in ``config.json``
    (for example when an authoring-side reproduction failed for an unrelated
    reason).  Trusting that marker short-circuits before any scoring.  Instead
    fail closed only when the materialized runtime is genuinely unusable.
    """
    workdir = runtime.get("workdir")
    if not isinstance(workdir, str) or not workdir.startswith("/"):
        return "public runtime workdir is missing"
    for key in ("build_commands", "native_test_commands"):
        commands = runtime.get(key)
        if not isinstance(commands, list) or not commands:
            return f"public runtime {key} is missing"
        for command in commands:
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(part, str) or not part for part in command)
            ):
                return f"public runtime {key} is invalid"
    harness = runtime.get("harness_command")
    if not isinstance(harness, list) or not harness:
        return "public runtime harness_command is missing"
    if any(not isinstance(part, str) or not part for part in harness):
        return "public runtime harness_command is invalid"
    return None


def _apply_observation_contract_errors(
    result: dict[str, object],
    errors: Sequence[str],
) -> None:
    if errors:
        result["status"] = "infra_error"
        result["hardgate"] = "INFRA_ERROR"
        result["eligible_for_graded"] = False
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
        target = workspace_target(candidate, target_name, allow_leaf_link=True)
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
                directory = workspace_target(candidate, target_name)
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(0o755)


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
    normalize_workspace_permissions(
        candidate,
        uid=CANDIDATE_UID if os.geteuid() == 0 else None,
        excluded=(".harbor-assets",),
    )


def _candidate_report_path() -> Path:
    """Return a candidate-writable report path outside the repository.

    Writing the harness report inside the candidate repo pollutes change-scope
    probes and makes valid migrations fail P2P reproduction.
    """

    return Path("/tmp/harbor-result.json")


def _candidate_placeholders(candidate: Path, assets: Path) -> dict[str, str]:
    return {
        "repo": str(candidate),
        "assets": str(assets),
        "report": str(_candidate_report_path()),
    }


def _require_isolation(bundle: Path, *, allow_judge_egress: bool = False) -> None:
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
        # container is fine: only the Rubric Judge needs that network.
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
    if not allow_judge_egress and devices != {"lo"}:
        raise OSError("Harbor candidate requires network_mode none")
    probe = subprocess.run(
        ["unshare", "-m", "--", "mount", "--bind", "/tmp", "/tmp"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if probe.returncode != 0:
        raise OSError("Harbor mount namespace isolation is unavailable")


def _load_graded_asset(assets: Path, name: str) -> dict[str, Any]:
    path = assets / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"graded asset is missing or unsafe: {name}")
    return _load_json(path)


def _graded_objective(assets: Path, candidate: Path, logs: Path) -> float | None:
    baseline = _load_graded_asset(assets, "objective-baseline.json")
    result = score_candidate_objective(baseline, candidate_root=candidate)
    payload = {**result, "schema_version": "migration-objective-result:v1"}
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "objective-results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return float(result["score"]) if result.get("status") == "PASS" else None


def _graded_rubric(assets: Path, logs: Path) -> float | None:
    asset = _load_graded_asset(assets, "rubric-asset.json")
    gateway = _load_graded_asset(assets, "judge-gateway.json")
    instruction = (assets / "judge-instruction.md").read_text(encoding="utf-8")
    model_patch = Path("/logs/artifacts/model.patch")
    candidate_diff = (
        model_patch.read_text(encoding="utf-8", errors="replace") if model_patch.is_file() else ""
    )
    transport = HttpJudgeTransport(
        base_url=str(gateway.get("base_url")),
        model=str(gateway.get("model")),
        api_key_env=str(gateway.get("api_key_env")),
        response_format=str(gateway.get("response_format", "json_object")),
    )
    client = JudgeClient(
        transport,
        timeout=float(gateway.get("timeout_seconds", 120.0)),
        max_retries=int(gateway.get("max_retries", 2)),
    )
    judgements = client.judge(
        asset,
        instruction=instruction,
        candidate_diff=candidate_diff,
        evidence={"note": "score only against the provided instruction, diff and atom scopes"},
    )
    result = score_rubric(asset, judgements)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "rubric-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return float(result["score"]) if result.get("status") == "PASS" else None


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
        del image, assets_root, network, writable_input, workspace_materializations
        root = Path(input_root)
        evidence = Path(evidence_root)
        evidence.mkdir(parents=True, exist_ok=True)
        args = [str(argument).replace("/logs/", str(evidence) + "/") for argument in command]
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


_LICHESS_CONTRACT_TEST = r'''package lichess_test

import (
    "fmt"
    "net/http"
    "net/http/httptest"
    "testing"

    "github.com/thomas-mauran/chess-tui/internal/lichess"
)

func TestVerifierHTTPStatus(t *testing.T) {
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path == "/board/game/g1/resign" {
            w.WriteHeader(http.StatusBadRequest)
            fmt.Fprint(w, "nope")
            return
        }
        if r.URL.Path != "/board/game/g1/move/e2e4" {
            t.Errorf("unexpected move path: %s", r.URL.Path)
        }
        w.WriteHeader(http.StatusTeapot)
    }))
    defer server.Close()
    client := lichess.NewClient("token")
    client.BaseURL = server.URL
    if err := client.MakeMove("g1", "e2e4"); err == nil || (err.Error() != "Failed to make move: 418 I'm a teapot" && err.Error() != "Failed to make move: 418") {
        t.Errorf("make_move error = %v", err)
    }
    if err := client.ResignGame("g1"); err == nil || (err.Error() != "Failed to resign game: 400 Bad Request - nope" && err.Error() != "Failed to resign game: 400 - nope") {
        t.Errorf("resign error = %v", err)
    }
}

func TestVerifierGameInfoMalformed(t *testing.T) {
    server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path != "/stream/game/g1" {
            t.Errorf("unexpected game info path: %s", r.URL.Path)
        }
        fmt.Fprint(w, "{\"turns\":\"bad\",\"lastMove\":\"e2e4\"}\n")
    }))
    defer server.Close()
    client := lichess.NewClient("token")
    client.BaseURL = server.URL
    if _, _, err := client.GetGameInfo("g1"); err == nil {
        t.Error("unparsable turns value must return an error")
    }
}
'''


def _lichess_contract_probe(
    candidate: Path, logs: Path, environment: Mapping[str, str],
    timeout: float, public_runtime: bool,
) -> dict[str, bool]:
    """Run behavior checks that the shipped target runner does not enforce."""

    package = candidate / "internal" / "lichess"
    if (
        not package.is_dir()
        or package.is_symlink()
        or not package.resolve().is_relative_to(candidate.resolve())
    ):
        return {"TestVerifierHTTPStatus": False, "TestVerifierGameInfoMalformed": False}
    probe = package / f"verifier_{uuid.uuid4().hex}_test.go"
    probe.write_text(_LICHESS_CONTRACT_TEST, encoding="utf-8")
    probe.chmod(0o644)
    try:
        completed = _run(
            ["go", "test", "-json", "-count=1", "-timeout=30s", "-run=^TestVerifier(HTTPStatus|GameInfoMalformed)$", "./internal/lichess"],
            cwd=candidate,
            timeout=min(timeout, 120),
            environment=environment,
            unprivileged=public_runtime,
        )
    finally:
        probe.unlink()
    (logs / "lichess-contract-probe.txt").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    statuses: dict[str, bool] = {}
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("Test")
        if name in {"TestVerifierHTTPStatus", "TestVerifierGameInfoMalformed"} and event.get("Action") in {"pass", "fail"}:
            statuses[name] = event["Action"] == "pass"
    return {
        name: statuses.get(name, False)
        for name in ("TestVerifierHTTPStatus", "TestVerifierGameInfoMalformed")
    }


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
            _require_isolation(bundle, allow_judge_egress=reward_mode == "GRADED")
        except (OSError, ValueError) as exc:
            return _write_result(logs, _public_infrastructure_result(str(exc)))
        if public_contract and public_contract.get("runtime_error"):
            runtime_error = _public_runtime_error(runtime)
            if runtime_error:
                return _write_result(logs, _public_infrastructure_result(runtime_error))
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
            "eligible_for_graded": False,
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
        candidate_report = _candidate_report_path()
        try:
            candidate_report.unlink()
        except OSError:
            pass
        placeholder_values = _candidate_placeholders(candidate, candidate_assets)
        if private_assets:
            harness_assets = candidate_assets
    else:
        candidate_report = logs / "result.json"
        placeholder_values["report"] = str(candidate_report)
    rendered_commands = [
        (phase, list(render_runtime_argv(tuple(str(item) for item in raw), placeholder_values)))
        for phase, raw in commands
    ]
    receipts, candidate_failed, infrastructure_failed = _run_declared_commands(
        rendered_commands,
        cwd=candidate,
        timeout=timeout,
        environment=environment,
        unprivileged=public_runtime,
    )
    if candidate_failed or infrastructure_failed:
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
    command = list(render_runtime_argv(tuple(str(item) for item in harness), placeholder_values))
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
    if not candidate_report.is_file():
        if completed.returncode != 0:
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
            return _write_result(logs, result)
        raise ValueError("Harbor candidate harness did not produce /logs/verifier/result.json")
    if public_runtime:
        shutil.copyfile(candidate_report, report)
        try:
            candidate_report.unlink()
        except OSError:
            pass
    try:
        payload = _load_json(report)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raw_tail = ""
        try:
            raw = report.read_bytes()
            raw_tail = raw[-4000:].decode("utf-8", errors="replace")
        except OSError:
            raw_tail = ""
        return _write_result(
            logs,
            _public_infrastructure_result(
                f"candidate report parse failed: {report}: {type(exc).__name__}: {exc}",
                failure={
                    "kind": "report-parse",
                    "report_path": str(report),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:2000],
                    "raw_tail": raw_tail,
                },
            ),
        )
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
    contract_errors = tuple(projection_errors) + adapter.validate_report(
        payload, config, actual_returncode=completed.returncode
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
    source_baseline = _load_json(assets / "source-baseline-outputs.json")
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
                    baseline,
                    current,
                    config.get("normalization_policy", {}),
                ).status
            current["status"] = merge_comparison_status(
                str(current.get("status", "INCONCLUSIVE")),
                str(comparison_status),
            )
        compared.append(current)
    if public_runtime:
        try:
            probe_status = _lichess_contract_probe(
                candidate, logs, environment, timeout, public_runtime
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _write_result(
                logs, _public_infrastructure_result(
                    f"Lichess contract probe could not run: {type(exc).__name__}: {exc}"
                ),
            )
        for row in compared:
            probe_name = {
                "F2P-010": "TestVerifierHTTPStatus",
                "DIFF-024": "TestVerifierHTTPStatus",
                "F2P-006": "TestVerifierGameInfoMalformed",
                "DIFF-027": "TestVerifierGameInfoMalformed",
            }.get(str(row.get("id")))
            if probe_name and not probe_status[probe_name]:
                row["status"] = "FAIL"
                row["value"] = {"type": "json", "value": {"result": "FAIL"}}
                if "candidate_value" in row:
                    row["candidate_value"] = row["value"]
                row["reason"] = f"Lichess behavior probe failed: {probe_name}"
    by_kind = {
        kind: [row for row in compared if row.get("kind") == kind]
        for kind in ("P2P", "F2P", "DIFFERENTIAL", "STATE")
    }
    assets_config = config.get("assets", {})
    assets_config = assets_config if isinstance(assets_config, Mapping) else {}
    target_baseline_name = assets_config.get("target_test_baseline")
    source_baseline_name = assets_config.get("source_test_baseline")
    protected_baseline = (
        _load_json(assets / str(target_baseline_name))
        if target_baseline_name and (assets / str(target_baseline_name)).is_file()
        else None
    )
    provided_baseline = (
        _load_json(assets / str(source_baseline_name))
        if source_baseline_name and (assets / str(source_baseline_name)).is_file()
        else None
    )
    # ``tests_not_tampered`` protects the tests the candidate was *given*
    # (the source baseline).  The reference-derived target baseline is the
    # comparison set for semantic drift, but a target-only test the candidate
    # never wrote must not be reported as tampering.
    test_baseline = provided_baseline or protected_baseline or {"files": {}, "marker_counts": {}}
    if isinstance(provided_baseline, Mapping) and isinstance(provided_baseline.get("files"), Mapping):
        provided_test_paths: list[str] | None = list(provided_baseline["files"])
    else:
        provided_test_paths = None
    test_baseline_source = (
        "target-test-baseline" if protected_baseline is not None else "source-test-baseline"
    )
    evidence = build_hardgate_evidence(
        candidate,
        config=config,
        observations=compared,
        observation_errors=contract_errors,
        receipts=receipts,
        patch_applicable=not apply_failed,
        source_test_baseline=test_baseline,
        provided_test_paths=provided_test_paths,
        test_baseline_source=test_baseline_source,
    )
    hardgate = evaluate_hardgate(candidate, evidence)
    result_evidence = {
        key: evidence.get(key)
        for key in (
            "anti_cheating_pass", "protected_paths_unchanged", "tests_not_tampered",
            "no_new_skip_or_xfail", "no_deceptive_source_fallback",
            "public_contract_preserved", "source_hits",
        )
    }
    result_evidence["deceptive_fallback_hits"] = evidence.get("deceptive_fallback_hits")
    result_evidence["ordinary_residue_hits"] = evidence.get("ordinary_residue_hits")
    result_evidence["compound_gates"] = {
        name: {"status": value.get("status")}
        for name, value in (hardgate.get("compound_gates") or {}).items()
        if isinstance(value, Mapping)
    }
    result_evidence["missing_atoms"] = hardgate.get("missing_atoms", [])
    result_evidence["hardgate_summary"] = hardgate.get("summary", {})
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
        weights=config["weights"],
        applicability=config.get("group_applicability"),
    )
    _apply_observation_contract_errors(result, contract_errors)
    objective_score: float | None = None
    rubric_score: float | None = None
    if reward_mode == "GRADED" and result.get("status") == "success":
        try:
            objective_score = _graded_objective(assets, candidate, logs)
        except Exception as exc:  # never let a grader defect become a candidate zero
            objective_score = None
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "objective-results.json").write_text(
                json.dumps({"schema_version": "migration-objective-result:v1", "status": "INFRA_ERROR", "error": str(exc)[:2000]}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        try:
            rubric_score = _graded_rubric(assets, logs)
        except Exception as exc:
            rubric_score = None
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "rubric-results.json").write_text(
                json.dumps({"schema_version": "migration-rubric-result:v1", "status": "INFRA_ERROR", "error": str(exc)[:2000]}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    result["reward"] = calculate_reward(
        hardgate=hardgate,
        verifier=result,
        reward_mode=reward_mode,
        objective=objective_score,
        rubric=rubric_score,
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
