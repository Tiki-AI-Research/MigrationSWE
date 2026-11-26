"""Direct Harbor verifier entry; this module never starts Docker."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import json
import os
import re
import shutil
import subprocess
import tempfile
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
        # The failing Go test can precede thousands of later passing tests.
        # Keep the complete failure output in the receipt; presentation fields
        # such as _failure_payload still use bounded tails.
        receipt["stdout"] = completed.stdout or ""
        receipt["stderr"] = completed.stderr or ""
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


def _go_skip_audit(output: str, returncode: int) -> dict[str, object]:
    """Read verifier-invoked ``go test -json`` outcomes, never source markers.

    A conditional Skip/Short branch is legal when the full run executes its
    checks. Retain actual skipped tests for the source-test integrity gate;
    optional newly added tests are not source obligations. Package-level
    "skip" events for packages with no test files are not skipped test cases.
    """
    skipped: list[dict[str, str]] = []
    finished_packages: set[str] = set()
    passed_tests = 0
    malformed = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(row, Mapping) or not isinstance(row.get("Action"), str):
            malformed += 1
            continue
        action, package, test = row["Action"], row.get("Package"), row.get("Test")
        if test and action == "skip":
            skipped.append({"package": str(package), "test": str(test)})
        if test and action == "pass":
            passed_tests += 1
        if not test and package and action in {"pass", "fail", "skip"}:
            finished_packages.add(str(package))
    return {
        "status": "PASS" if returncode == 0 and finished_packages and not malformed else "INCONCLUSIVE",
        "skipped_tests": skipped,
        "passed_tests": passed_tests,
        "finished_packages": sorted(finished_packages),
        "malformed_lines": malformed,
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
        command = [str(item) for item in raw]
        audit_go = kind == "target-native-test" and len(command) >= 2 and Path(command[0]).name == "go" and command[1] == "test"
        if audit_go:
            # Preserve every declared test target; add structured outcomes and
            # disable caching so conditional skips are observed in this run.
            command = [*command[:2], "-json", "-count=1", *command[2:]]
        completed, receipt = _run_with_receipt(
            command,
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
        if audit_go:
            receipt["go_skip_audit"] = _go_skip_audit(completed.stdout or "", completed.returncode)
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


_STATUS_WRAPPER_KEYS = frozenset({"result", "outcome", "status"})


def _frozen_p2p_status_wrapper_ids(baseline_rows: object) -> list[str] | None:
    """Return frozen P2P ids when every P2P row is a bare status wrapper.

    ``None`` means the frozen baseline carries at least one concrete test
    surface (``test:<file>#<title>``) or is malformed, so the deterministic
    native-test collector keeps ownership.  A non-empty list means the P2P group
    is a status wrapper with no executable surface, so it must be scored from
    the verifier-owned prerequisite receipts instead of the candidate harness.
    """

    if not isinstance(baseline_rows, list):
        return None
    ids: list[str] = []
    for row in baseline_rows:
        if not isinstance(row, Mapping) or str(row.get("kind")) != "P2P":
            continue
        value = row.get("value")
        inner = value.get("value") if isinstance(value, Mapping) else None
        identifier = row.get("id")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(inner, Mapping)
        ):
            return None
        file = inner.get("file")
        title = inner.get("title")
        if isinstance(file, str) and file and isinstance(title, str) and title:
            return None
        if not any(str(key).casefold() in _STATUS_WRAPPER_KEYS for key in inner):
            return None
        ids.append(identifier)
    return ids or None


def _native_test_aggregate_status(
    receipts: Sequence[Mapping[str, object]],
) -> str:
    """Aggregate the verifier-owned native-test prerequisite receipts.

    The verifier declares and runs the target runtime's native-test commands
    (``go test ./...`` / ``gotestsum ... ./...``) *before* the candidate harness,
    so their return codes are verifier-owned evidence.  The aggregate is PASS
    only when at least one ``target-native-test`` receipt completed with
    returncode 0; a failed, timed-out or missing receipt is fail-closed.
    """

    native = [
        row
        for row in receipts
        if isinstance(row, Mapping) and str(row.get("kind")) == "target-native-test"
    ]
    if not native:
        return "INCONCLUSIVE"
    for row in native:
        if row.get("timed_out") is True or row.get("returncode") is None:
            return "INCONCLUSIVE"
        if row.get("returncode") != 0:
            return "FAIL"
    return "PASS"


def _apply_aggregate_native_p2p_status(
    *,
    observations: list[object],
    p2p_ids: Sequence[str],
    receipts: Sequence[Mapping[str, object]],
    logs: Path,
    diagnostic: dict[str, object],
) -> list[object]:
    """Replace every frozen P2P row with verifier-owned aggregate evidence.

    The frozen baseline's status-wrapper projection is preserved exactly (no
    ``file``/``title`` surface is invented), but the result now derives from the
    verifier's own native-test execution rather than the candidate harness.
    """

    status = _native_test_aggregate_status(receipts)
    diagnostic["route"] = "aggregate-native-test-receipts"
    diagnostic["aggregate_status"] = status
    diagnostic["observation_ids"] = list(p2p_ids)
    diagnostic["native_test_receipts"] = [
        {
            "index": row.get("index"),
            "command": row.get("command"),
            "returncode": row.get("returncode"),
            "timed_out": row.get("timed_out"),
        }
        for row in receipts
        if isinstance(row, Mapping) and str(row.get("kind")) == "target-native-test"
    ]
    _write_native_merge_diagnostic(logs, diagnostic)
    replacements = {
        identifier: {
            "id": identifier,
            "kind": "P2P",
            "status": status,
            "value": {
                "type": "json",
                "value": {"projection": "status-wrapper", "result": status},
            },
        }
        for identifier in p2p_ids
    }
    merged: list[object] = []
    seen: set[str] = set()
    for row in observations:
        identifier = str(row.get("id")) if isinstance(row, Mapping) else ""
        if identifier in replacements:
            merged.append(dict(replacements[identifier]))
            seen.add(identifier)
        else:
            merged.append(row)
    for identifier in p2p_ids:
        if identifier not in seen:
            merged.append(dict(replacements[identifier]))
            seen.add(identifier)
    return merged


def _merge_native_p2p_observations(
    *,
    observations: list[object],
    receipts: Sequence[Mapping[str, object]],
    config: Mapping[str, object],
    runtime: Mapping[str, object],
    candidate: Path,
    assets: Path,
    logs: Path,
    timeout: float,
    environment: Mapping[str, str],
) -> list[object]:
    """Override native-test P2P rows with verifier-owned evidence.

    When the frozen baseline declares concrete test surfaces, the deterministic
    collector owns the P2P rows.  When it is a bare status wrapper (no
    ``file``/``title``), the aggregate prerequisite receipts own every frozen P2P
    id, so the candidate harness report can no longer supply the sole P2P
    evidence.
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

    # Frozen status-wrapper P2P: there is no test surface to collect, so score
    # every frozen P2P id from the verifier-owned prerequisite receipts and
    # never trust the candidate harness's P2P status as the sole evidence.
    wrapper_ids = _frozen_p2p_status_wrapper_ids(baseline_rows)

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
            if wrapper_ids is not None:
                return _apply_aggregate_native_p2p_status(
                    observations=list(observations),
                    p2p_ids=wrapper_ids,
                    receipts=receipts,
                    logs=logs,
                    diagnostic=diagnostic,
                )
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


_CONTRACT_PROBE_GOMOD = """module github.com/stremio/stremio-core/probes

go 1.23.0

require github.com/stremio/stremio-core v0.0.0

replace github.com/stremio/stremio-core => {candidate}
"""

_CONTRACT_PROBE_GO = r'''package contractprobe

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	core "github.com/stremio/stremio-core"
	addontransport "github.com/stremio/stremio-core/addon_transport"
	"github.com/stremio/stremio-core/analytics"
	deeplinks "github.com/stremio/stremio-core/deep_links"
	testenv "github.com/stremio/stremio-core/internal/testenv"
	"github.com/stremio/stremio-core/runtime"
	"github.com/stremio/stremio-core/runtime/msg"
	"github.com/stremio/stremio-core/types"
)

// JSON object member order is not part of the message contract. Array
// order, member presence, values and types must still match exactly.
func jsonEqual(got, want string) bool {
	var actual, expected any
	return json.Unmarshal([]byte(got), &actual) == nil &&
		json.Unmarshal([]byte(want), &expected) == nil && reflect.DeepEqual(actual, expected)
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(b)
}

func TestRuntimeEventWire(t *testing.T) {
	if got := mustJSON(t, runtime.NewStateEvent([]string{"ctx", "player"})); !jsonEqual(got, `{"name":"NewState","args":["ctx","player"]}`) {
		t.Fatalf("NewState wire = %s", got)
	}
	if got := mustJSON(t, runtime.CoreEvent(msg.NewEvent("UserLoggedOut", nil))); !jsonEqual(got, `{"name":"CoreEvent","args":{"event":"UserLoggedOut"}}`) {
		t.Fatalf("CoreEvent wire = %s", got)
	}
}

func TestActionEventWire(t *testing.T) {
	if got := mustJSON(t, msg.NewAction("Unload", nil)); !jsonEqual(got, `{"action":"Unload"}`) {
		t.Fatalf("action wire = %s", got)
	}
	if got := mustJSON(t, msg.LoadAction("Player", map[string]any{"stream": 1})); !jsonEqual(got, `{"action":"Load","args":{"model":"Player","args":{"stream":1}}}`) {
		t.Fatalf("load action wire = %s", got)
	}
	if got := mustJSON(t, msg.NewEvent("PlayerPlaying", nil)); !jsonEqual(got, `{"event":"PlayerPlaying"}`) {
		t.Fatalf("event wire = %s", got)
	}
}

func TestEnvErrorWireAndCodes(t *testing.T) {
	exact := []struct {
		got  *runtime.EnvError
		want string
	}{
		{runtime.EnvErrorFetch("x"), `{"code":1,"message":"Failed to fetch: x"}`},
		{runtime.EnvErrorOther("x"), `{"code":1001,"message":"Other error: x"}`},
		{runtime.EnvErrorStorageSchemaVersionDowngrade(26, 25), `{"code":5,"message":"Downgrade storage schema version from 26 to 25 is not allowed"}`},
	}
	for _, c := range exact {
		if got := mustJSON(t, c.got); !jsonEqual(got, c.want) {
			t.Errorf("wire = %s, want %s", got, c.want)
		}
	}
	codes := []struct {
		got  *runtime.EnvError
		code int
	}{
		{runtime.EnvErrorFetch("x"), 1},
		{runtime.EnvErrorAddonTransport("x"), 2},
		{runtime.EnvErrorSerde("x"), 3},
		{runtime.EnvErrorStorageUnavailable(), 4},
		{runtime.EnvErrorStorageSchemaVersionDowngrade(26, 25), 5},
		{runtime.EnvErrorStorageSchemaVersionUpgrade(runtime.EnvErrorOther("x")), 6},
		{runtime.EnvErrorStorageReadError("x"), 7},
		{runtime.EnvErrorStorageWriteError("x"), 8},
		{runtime.EnvErrorOther("x"), 1001},
	}
	for _, c := range codes {
		if c.got.Code != c.code {
			t.Errorf("code = %d, want %d", c.got.Code, c.code)
		}
		wire := mustJSON(t, c.got)
		var fields map[string]json.RawMessage
		if json.Unmarshal([]byte(wire), &fields) != nil || len(fields) != 2 || fields["code"] == nil || fields["message"] == nil {
			t.Errorf("wire = %s", wire)
		}
		if strings.Contains(wire, `"Code"`) || strings.Contains(wire, `"Message"`) {
			t.Errorf("internal field names leaked: %s", wire)
		}
	}
}

func TestProtectedStorageKeys(t *testing.T) {
	cases := map[string]string{
		core.SCHEMA_VERSION_STORAGE_KEY:        "schema_version",
		core.PROFILE_STORAGE_KEY:               "profile",
		core.LIBRARY_STORAGE_KEY:               "library",
		core.LIBRARY_RECENT_STORAGE_KEY:        "library_recent",
		core.STREAMS_STORAGE_KEY:               "streams",
		core.SEARCH_HISTORY_STORAGE_KEY:        "search_history",
		core.STREAMING_SERVER_URLS_STORAGE_KEY: "streaming_server_urls",
		core.NOTIFICATIONS_STORAGE_KEY:         "notifications",
		core.CALENDAR_STORAGE_KEY:              "calendar",
		core.DISMISSED_EVENTS_STORAGE_KEY:      "dismissed_events",
	}
	for got, want := range cases {
		if got != want {
			t.Errorf("storage key = %q, want %q", got, want)
		}
	}
	if core.SCHEMA_VERSION != 25 {
		t.Errorf("schema version = %d, want 25", core.SCHEMA_VERSION)
	}
}

func TestLegacyJSONRPC(t *testing.T) {
	got, err := addontransport.BuildLegacyReq(
		"https://legacywatchhub.strem.io/stremio/v1",
		types.ResourcePath{Resource: "stream", Type: "series", ID: "tt0386676:5:1"},
	)
	if err != nil {
		t.Fatalf("BuildLegacyReq: %v", err)
	}
	want := "https://legacywatchhub.strem.io/stremio/v1/q.json?b=eyJpZCI6MSwianNvbnJwYyI6IjIuMCIsIm1ldGhvZCI6InN0cmVhbS5maW5kIiwicGFyYW1zIjpbbnVsbCx7InF1ZXJ5Ijp7ImVwaXNvZGUiOjEsImltZGJfaWQiOiJ0dDAzODY2NzYiLCJzZWFzb24iOjUsInR5cGUiOiJzZXJpZXMifX1dfQ=="
	if got != want {
		t.Fatalf("legacy url mismatch:\n got: %s\nwant: %s", got, want)
	}
	mapped := (&addontransport.LegacyErr{Kind: addontransport.LegacyJSONRPC, Code: 5, Message: "boom"}).ToEnvError()
	if mapped.Code != 2 || mapped.Message != "Addon protocol violation: rpc error 5: boom" {
		t.Fatalf("legacy error mapping = %+v", mapped)
	}
}

func TestDeepLinkShapes(t *testing.T) {
	if got := deeplinks.NewSearchHistoryItemDeepLinks("foo bar").Search; got != "stremio:///search?query=foo%20bar" {
		t.Fatalf("search deep link = %q", got)
	}
	request := types.ResourceRequest{
		Base: "https://v3-cinemeta.strem.io/manifest.json",
		Path: types.ResourcePath{
			Resource: "catalog",
			Type:     "movie",
			ID:       "top",
			Extra:    []types.ExtraValue{{Name: "genre", Value: "Action"}},
		},
	}
	want := "stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json/movie/top?genre=Action"
	if got := deeplinks.NewDiscoverDeepLinks(request).Discover; got != want {
		t.Fatalf("discover deep link = %q, want %q", got, want)
	}
	meta := deeplinks.NewMetaItemDeepLinks("series", "tt1")
	if meta.MetaDetailsVideos == nil || *meta.MetaDetailsVideos != "stremio:///detail/series/tt1" {
		t.Fatalf("meta item deep link = %+v", meta)
	}
}

func TestQueryParamsEncode(t *testing.T) {
	got := types.QueryParamsEncode([]types.QueryPair{
		{Key: "sort", Value: "popularity"},
		{Key: "genre", Value: "A B"},
	})
	if got != "sort=popularity&genre=A%20B" {
		t.Fatalf("query params = %q", got)
	}
	if strings.Contains(got, "+") {
		t.Fatalf("space must be percent-encoded, not +: %q", got)
	}
}

func TestAnalyticsQueueSurface(t *testing.T) {
	var sent []analytics.Batch
	a := analytics.New(testenv.New(), func(b analytics.Batch) error {
		sent = append(sent, b)
		return nil
	})
	a.Emit("evt", map[string]any{"k": "v"}, "", nil)
	if got := a.QueueLen(); got != 0 {
		t.Fatalf("an empty auth key must not queue a batch, queue len = %d", got)
	}
	a.Emit("evt", map[string]any{"k": "v"}, "auth", nil)
	if got := a.QueueLen(); got != 1 {
		t.Fatalf("emit must queue one batch, queue len = %d", got)
	}
	a.Flush()
	if len(sent) != 1 || len(sent[0].Events) != 1 || sent[0].Events[0].Name != "evt" {
		t.Fatalf("flush must send the queued batch, sent = %+v", sent)
	}
}

func TestStorageSchemaMigration(t *testing.T) {
	// v1 -> latest: the persisted profile must be upgraded (legacy setting names
	// mapped to their current names, later defaults introduced) rather than
	// discarded, and the buckets removed by later schema versions must be cleared.
	e := testenv.New()
	e.Storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage(`1`)
	e.Storage[core.PROFILE_STORAGE_KEY] = json.RawMessage(`{"settings":{"interface_language":"eng","streaming_server_url":"http://127.0.0.1:11470"}}`)
	e.Storage[core.STREAMS_STORAGE_KEY] = json.RawMessage(`{"uid":null,"items":[]}`)
	e.Storage[core.SEARCH_HISTORY_STORAGE_KEY] = json.RawMessage(`{"uid":null,"items":{}}`)
	e.Storage[core.DISMISSED_EVENTS_STORAGE_KEY] = json.RawMessage(`{"uid":null,"items":{}}`)
	e.Storage[core.STREAMING_SERVER_URLS_STORAGE_KEY] = json.RawMessage(`{"uid":null,"items":{}}`)
	if err := runtime.MigrateStorageSchema(e); err != nil {
		t.Fatalf("migrate storage schema: %v", err)
	}
	var version uint32
	if err := json.Unmarshal(e.Storage[core.SCHEMA_VERSION_STORAGE_KEY], &version); err != nil || version != core.SCHEMA_VERSION {
		t.Fatalf("schema version after migration = %d (err %v), want %d", version, err, core.SCHEMA_VERSION)
	}
	var profile struct {
		AddonsLocked *bool                      `json:"addonsLocked"`
		Settings     map[string]json.RawMessage `json:"settings"`
	}
	if err := json.Unmarshal(e.Storage[core.PROFILE_STORAGE_KEY], &profile); err != nil {
		t.Fatalf("profile after migration: %v (raw %q)", err, string(e.Storage[core.PROFILE_STORAGE_KEY]))
	}
	if got := string(profile.Settings["interfaceLanguage"]); got != `"eng"` {
		t.Errorf("interfaceLanguage after migration = %s, want \"eng\"", got)
	}
	if _, ok := profile.Settings["interface_language"]; ok {
		t.Errorf("legacy interface_language must be renamed by the migration")
	}
	exact := map[string]string{
		"gpuVideoProcessing":        "false",
		"seekTimeDuration":          "20000",
		"audioLanguage":             `"eng"`,
		"frameRateMatchingStrategy": `"FrameRateOnly"`,
		"seekShortTimeDuration":     "3000",
		"subtitlesOpacity":          "100",
		"interfaceScale":            "100",
		"discordRpcEnabled":         "false",
	}
	for key, want := range exact {
		got, ok := profile.Settings[key]
		if !ok {
			t.Errorf("setting %q missing after migration", key)
			continue
		}
		if string(got) != want {
			t.Errorf("setting %q = %s, want %s", key, got, want)
		}
	}
	if profile.AddonsLocked == nil || *profile.AddonsLocked {
		t.Errorf("profile addonsLocked after migration = %v, want false", profile.AddonsLocked)
	}
	for _, key := range []string{
		core.STREAMS_STORAGE_KEY,
		core.SEARCH_HISTORY_STORAGE_KEY,
		core.DISMISSED_EVENTS_STORAGE_KEY,
		core.STREAMING_SERVER_URLS_STORAGE_KEY,
	} {
		if _, ok := e.Storage[key]; ok {
			t.Errorf("storage key %q must be cleared by the migration", key)
		}
	}

	// v5 -> v6: a persisted playInExternalPlayer=true becomes playerType="external".
	e2 := testenv.New()
	e2.Storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage(`5`)
	e2.Storage[core.PROFILE_STORAGE_KEY] = json.RawMessage(`{"settings":{"playInExternalPlayer":true}}`)
	if err := runtime.MigrateStorageSchema(e2); err != nil {
		t.Fatalf("migrate storage schema from v5: %v", err)
	}
	var upgraded struct {
		Settings map[string]json.RawMessage `json:"settings"`
	}
	if err := json.Unmarshal(e2.Storage[core.PROFILE_STORAGE_KEY], &upgraded); err != nil {
		t.Fatalf("profile after v5 migration: %v", err)
	}
	if got := string(upgraded.Settings["playerType"]); got != `"external"` {
		t.Errorf("playerType after v5 migration = %s, want \"external\"", got)
	}
	if _, ok := upgraded.Settings["playInExternalPlayer"]; ok {
		t.Errorf("playInExternalPlayer must be removed by the v6 upgrade")
	}
}
'''


_BEHAVIOR_PROBE_GO = r'''package contractprobe

import (
	"encoding/json"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	core "github.com/stremio/stremio-core"
	"github.com/stremio/stremio-core/internal/testenv"
	"github.com/stremio/stremio-core/models"
	"github.com/stremio/stremio-core/models/common"
	"github.com/stremio/stremio-core/runtime"
	"github.com/stremio/stremio-core/runtime/msg"
	"github.com/stremio/stremio-core/types"
)

// These probes use only the public migration contract. Candidate-authored
// tests and the build-only observation wrappers cannot substitute for them.
// Scheduling belongs to Env. Queue effects in a verifier-owned scheduler and
// drain them after Dispatch returns. A runtime may hold a model lock while it
// schedules a future, so invoking the callback inside ExecConcurrent would
// impose an unsupported reentrancy requirement. Draining keeps fetch callbacks
// inside the test lifetime while the engine still handles every returned msg.
type probeEnv struct {
	*testenv.Env
	mu sync.Mutex
	tasks []func()
}

func newProbeEnv() *probeEnv { return &probeEnv{Env: testenv.New()} }
func (env *probeEnv) enqueue(f func()) {
	env.mu.Lock()
	env.tasks = append(env.tasks, f)
	env.mu.Unlock()
}
func (env *probeEnv) ExecConcurrent(f func()) { env.enqueue(f) }
func (env *probeEnv) ExecSequential(f func()) { env.enqueue(f) }
func (env *probeEnv) drain() {
	for {
		env.mu.Lock()
		if len(env.tasks) == 0 { env.mu.Unlock(); return }
		task := env.tasks[0]
		env.tasks = env.tasks[1:]
		env.mu.Unlock()
		task()
	}
}

var _ runtime.Env = (*probeEnv)(nil)

type chainModel struct { whole, field int }

func (m *chainModel) Update(message *msg.Msg) ([]runtime.Effect, []string) {
	m.whole++
	if message.Kind == msg.KindInternal && message.Internal.Name == "first" {
		return []runtime.Effect{runtime.MsgEffect(msg.InternalMsg(msg.NewInternal("second", nil)))}, []string{"ctx"}
	}
	if message.Kind == msg.KindInternal && message.Internal.Name == "second" {
		return []runtime.Effect{runtime.MsgEffect(msg.EventMsg(msg.NewEvent("done", nil)))}, nil
	}
	return nil, nil
}

func (m *chainModel) UpdateField(message *msg.Msg, field string) ([]runtime.Effect, []string) {
	m.field++
	return nil, []string{field}
}

// Allow queued event forwarding to run before declaring the receiver empty.
// The deadline also bounds a broken producer that never becomes idle.
func collectEvents(rx <-chan runtime.RuntimeEvent) []runtime.RuntimeEvent {
	var events []runtime.RuntimeEvent
	deadline := time.NewTimer(2 * time.Second)
	defer deadline.Stop()
	for {
		idle := time.NewTimer(250 * time.Millisecond)
		select {
		case event, ok := <-rx:
			idle.Stop()
			if !ok { return events }
			events = append(events, event)
		case <-idle.C:
			return events
		case <-deadline.C:
			idle.Stop()
			return events
		}
	}
}

func TestRuntimeProcessesReturnedEffectsAndFieldRouting(t *testing.T) {
	m := &chainModel{}
	r, rx := runtime.New(m, []runtime.Effect{runtime.MsgEffect(msg.InternalMsg(msg.NewInternal("first", nil)))}, 32, newProbeEnv())
	events := collectEvents(rx)
	if m.whole != 2 || len(events) != 2 || events[0].Name != "NewState" || string(events[0].Args) != `["ctx"]` || events[1].Name != "CoreEvent" {
		t.Fatalf("internal effect chain lost: updates=%d events=%+v", m.whole, events)
	}
	if r.Model() != m { t.Fatal("Model must return the current model") }
	field := "player"
	r.Dispatch(runtime.RuntimeAction{Field: &field, Action: msg.UnloadAction()})
	if m.field != 1 || m.whole != 2 { t.Fatalf("field dispatch updated whole model: %+v", m) }
	r.Dispatch(runtime.RuntimeAction{Action: msg.UnloadAction()})
	if m.whole != 3 || m.field != 1 { t.Fatalf("whole dispatch routed incorrectly: %+v", m) }
	if got := collectEvents(rx); len(got) != 1 || got[0].Name != "NewState" || string(got[0].Args) != `["player"]` {
		t.Fatalf("no-op dispatch must not emit a state: %+v", got)
	}
}

func TestEventEffectsDoNotUpdateModel(t *testing.T) {
	m := &chainModel{}
	_, rx := runtime.New(m, []runtime.Effect{runtime.MsgEffect(msg.EventMsg(msg.NewEvent("only-event", nil)))}, 8, newProbeEnv())
	events := collectEvents(rx)
	if m.whole != 0 || m.field != 0 || len(events) != 1 || events[0].Name != "CoreEvent" {
		t.Fatalf("event effect must emit only CoreEvent: model=%+v events=%+v", m, events)
	}
}

func TestActionEffectPanics(t *testing.T) {
	defer func() { if recover() == nil { t.Error("an action returned by an effect must panic") } }()
	runtime.New(&chainModel{}, []runtime.Effect{runtime.MsgEffect(msg.ActionMsg(msg.UnloadAction()))}, 8, newProbeEnv())
}

type resourceTransport struct { fail bool; calls int }

func (tr *resourceTransport) Manifest() (types.Manifest, *runtime.EnvError) { return types.Manifest{}, nil }
func (tr *resourceTransport) Resource(path types.ResourcePath) (types.ResourceResponse, *runtime.EnvError) {
	tr.calls++
	if tr.fail { return types.ResourceResponse{}, runtime.EnvErrorFetch("offline") }
	return types.ResourceResponse{Raw: []byte(`7`)}, nil
}

func TestResourceLoadingSuccessFailureAndStaleResults(t *testing.T) {
	for _, fail := range []bool{false, true} {
		env := newProbeEnv()
		transport := &resourceTransport{fail: fail}
		env.Transport = transport
		request := types.ResourceRequest{Base: "https://probe.invalid/manifest.json", Path: types.ResourcePath{Resource: "meta", Type: "movie", ID: "resource-a"}}
		var resource common.ResourceLoadable[int]
		convert := func(response types.ResourceResponse) (int, error) { var value int; err := json.Unmarshal(response.Raw, &value); return value, err }
		effects := common.ResourceUpdate(env, &resource, common.ResourceRequested(&request), convert)
		if !effects.HasChanged || resource.Content == nil || !resource.Content.IsLoading() || len(effects.Effects) != 1 || effects.Effects[0].Future == nil {
			t.Fatalf("request must enter Loading and schedule the fetch: %+v", effects)
		}
		output := effects.Effects[0].Future.Run()
		if transport.calls != 1 || output == nil || output.Kind != msg.KindInternal { t.Fatal("resource future did not execute the transport") }
		response := types.ResourceResponse{Raw: []byte(`7`)}
		result := common.ResourceResult{Response: &response}
		if fail { result = common.ResourceResult{Err: runtime.EnvErrorFetch("offline")} }
		stale := request
		stale.Path.ID = "resource-b"
		if ignored := common.ResourceUpdate(env, &resource, common.ResourceRequestResult(&stale, result), convert); ignored.HasChanged || !resource.Content.IsLoading() {
			t.Fatal("a stale resource response changed the current request")
		}
		updated := common.ResourceUpdate(env, &resource, common.ResourceRequestResult(&request, result), convert)
		if !updated.HasChanged || (fail && !resource.Content.IsErr()) || (!fail && !resource.Content.IsReady()) { t.Fatal("resource result lost its state transition") }
	}
}

// A real API response, with the same user fields and timestamp encoding as
// the Rust engine. No network or external scoring service is used.
const probeUser = `{"_id":"qc-user","email":"qc@example.invalid","fbId":null,"appleId":null,"avatar":null,"lastModified":"1970-01-01T00:00:00Z","dateRegistered":"1970-01-01T00:00:00Z","trakt":null,"premium_expire":null,"gdpr_consent":{"tos":true,"privacy":true,"marketing":false,"from":null}}`

func dispatchCtx(r *runtime.Runtime, name string, args any) {
	r.Dispatch(runtime.RuntimeAction{Action: msg.NewAction("Ctx", msg.NewAction(name, args))})
}

func hasCoreEvent(events []runtime.RuntimeEvent, name string) bool {
	for _, event := range events {
		if event.Name != "CoreEvent" { continue }
		var coreEvent msg.Event
		if json.Unmarshal(event.Args, &coreEvent) == nil && coreEvent.Event == name { return true }
	}
	return false
}

func TestAuthenticationFetchesAndPersistsUserState(t *testing.T) {
	env := newProbeEnv()
	seen := map[string]int{}
	item := types.LibraryItem{ID: "qc-remote-item", Name: "remote", Type: "movie", Mtime: env.Now()}
	items, err := json.Marshal([]types.LibraryItem{item})
	if err != nil { t.Fatal(err) }
	env.FetchFn = func(method, url string, headers map[string]string, body []byte) ([]byte, *runtime.EnvError) {
		seen[url]++
		if method != "POST" { t.Errorf("API method=%s", method) }
		switch {
		case strings.HasSuffix(url, "/loginWithToken"):
			var request map[string]any
			if json.Unmarshal(body, &request) != nil || request["token"] != "qc-token" { t.Errorf("authentication request=%s", body) }
			return []byte(`{"result":{"authKey":"qc-key","user":` + probeUser + `}}`), nil
		case strings.HasSuffix(url, "/addonCollectionGet"):
			return []byte(`{"result":{"addons":[],"lastModified":"1970-01-01T00:00:00Z"}}`), nil
		case strings.HasSuffix(url, "/datastoreGet"):
			return []byte(`{"result":` + string(items) + `}`), nil
		case strings.HasSuffix(url, "/logout"):
			return []byte(`{"result":{"success":true}}`), nil
		default:
			t.Errorf("unexpected API request %s %s", url, body)
			return nil, runtime.EnvErrorFetch("unexpected probe request")
		}
	}
	model := models.New(env)
	r, rx := runtime.New(model, nil, 256, env)
	dispatchCtx(r, "Authenticate", map[string]any{"type": "LoginWithToken", "token": "qc-token"})
	env.drain()
	for _, path := range []string{"loginWithToken", "addonCollectionGet", "datastoreGet"} {
		if seen["https://api.strem.io/api/"+path] != 1 { t.Errorf("authentication must fetch %s, requests=%v", path, seen) }
	}
	if model.Ctx.Profile.Auth == nil { t.Fatal("successful authentication did not set the profile") }
	if _, ok := model.Ctx.Library.Items[item.ID]; !ok { t.Error("authentication discarded the fetched library") }
	var persisted types.Profile
	if err := json.Unmarshal(env.Storage[core.PROFILE_STORAGE_KEY], &persisted); err != nil || persisted.Auth == nil { t.Fatalf("authenticated profile was not persisted: %s (%v)", env.Storage[core.PROFILE_STORAGE_KEY], err) }
	if !hasCoreEvent(collectEvents(rx), "UserAuthenticated") { t.Error("successful authentication did not emit UserAuthenticated") }
	dispatchCtx(r, "Logout", nil)
	env.drain()
	if model.Ctx.Profile.Auth != nil || seen["https://api.strem.io/api/logout"] != 1 { t.Errorf("logout did not clear the user and delete the session: %v", seen) }
	if !hasCoreEvent(collectEvents(rx), "UserLoggedOut") { t.Error("logout did not emit UserLoggedOut") }
}

func TestAuthenticationFailureDoesNotCreateAnEmptyUser(t *testing.T) {
	env := newProbeEnv()
	env.FetchFn = func(method, url string, headers map[string]string, body []byte) ([]byte, *runtime.EnvError) { return nil, runtime.EnvErrorFetch("offline") }
	model := models.New(env)
	r, rx := runtime.New(model, nil, 256, env)
	dispatchCtx(r, "Authenticate", map[string]any{"type":"LoginWithToken", "token":"qc-token"})
	env.drain()
	if model.Ctx.Profile.Auth != nil { t.Error("failed authentication created a user") }
	events := collectEvents(rx)
	if !hasCoreEvent(events, "Error") || hasCoreEvent(events, "UserAuthenticated") { t.Errorf("failed authentication events=%+v", events) }
}

func TestLibrarySyncUploadsLocalOnlyItems(t *testing.T) {
	env := newProbeEnv()
	uploaded := false
	env.FetchFn = func(method, url string, headers map[string]string, body []byte) ([]byte, *runtime.EnvError) {
		if strings.HasSuffix(url, "/datastoreMeta") || strings.HasSuffix(url, "/datastoreGet") { return []byte(`{"result":[]}`), nil }
		if strings.HasSuffix(url, "/datastorePut") {
			var request struct { Changes []types.LibraryItem `json:"changes"` }
			if err := json.Unmarshal(body, &request); err != nil { t.Error(err) }
			for _, item := range request.Changes { if item.ID == "qc-local-item" { uploaded = true } }
			return []byte(`{"result":{"success":true}}`), nil
		}
		t.Errorf("unexpected sync request %s %s", url, body)
		return nil, runtime.EnvErrorFetch("unexpected probe request")
	}
	model := models.New(env)
	profile := types.NewProfile()
	// Profile.settings is required by the source serde contract. Start from
	// the public default settings so strict decoders reach the sync assertion.
	if err := json.Unmarshal([]byte(`{"auth":{"key":"qc-key","user":`+probeUser+`},"addons":[],"addonsLocked":false,"settings":`+mustJSON(t, profile.Settings)+`}`), &profile); err != nil { t.Fatal(err) }
	model.Ctx.Profile = profile
	var uid types.UID
	if err := json.Unmarshal([]byte(`"qc-user"`), &uid); err != nil { t.Fatal(err) }
	item := types.LibraryItem{ID: "qc-local-item", Name: "local", Type: "movie", Mtime: env.Now()}
	model.Ctx.Library = types.NewLibraryBucket(uid, []types.LibraryItem{item})
	r, _ := runtime.New(model, nil, 256, env)
	dispatchCtx(r, "SyncLibraryWithAPI", nil)
	env.drain()
	if !uploaded { t.Error("library sync must upload local items missing from the remote library") }
}

func TestAllTraversedSchemaDefaultsAndExistingSettings(t *testing.T) {
	defaults := []struct { version uint32; name, value string }{
		{3,"streamingServerWarningDismissed",`null`}, {4,"seekTimeDuration",`20000`},
		{5,"audioLanguage",`"eng"`}, {5,"audioPassthrough",`false`},
		{6,"playerType",`null`}, {6,"nextVideoNotificationDuration",`35000`},
		{7,"frameRateMatchingStrategy",`"FrameRateOnly"`},
		{9,"seekShortTimeDuration",`3000`}, {9,"escExitFullscreen",`true`}, {9,"pauseOnMinimize",`false`},
		{11,"surroundSound",`false`}, {14,"subtitlesOpacity",`100`},
		{16,"serverInForeground",`false`}, {16,"sendCrashReports",`true`}, {17,"quitOnClose",`true`},
		{18,"hideSpoilers",`false`}, {19,"gamepadSupport",`false`}, {20,"videoMode",`null`},
		{21,"assSubtitlesStyling",`false`}, {22,"subtitlesAutoSelect",`true`},
		{23,"discordRpcEnabled",`false`}, {24,"interfaceScale",`100`}, {25,"gpuVideoProcessing",`false`},
	}
	for version := uint32(1); version < core.SCHEMA_VERSION; version++ {
		env := newProbeEnv()
		rawVersion, _ := json.Marshal(version)
		env.Storage[core.SCHEMA_VERSION_STORAGE_KEY] = rawVersion
		env.Storage[core.PROFILE_STORAGE_KEY] = json.RawMessage(`{"settings":{"interfaceLanguage":"fra"}}`)
		if version == 1 { env.Storage[core.PROFILE_STORAGE_KEY] = json.RawMessage(`{"settings":{"interface_language":"fra"}}`) }
		if err := runtime.MigrateStorageSchema(env); err != nil { t.Fatalf("v%d: %v", version, err) }
		var profile struct { Settings map[string]json.RawMessage `json:"settings"` }
		if err := json.Unmarshal(env.Storage[core.PROFILE_STORAGE_KEY], &profile); err != nil { t.Fatalf("v%d profile: %v", version, err) }
		if string(profile.Settings["interfaceLanguage"]) != `"fra"` { t.Errorf("v%d lost a persisted setting", version) }
		for _, entry := range defaults {
			if version >= entry.version { continue }
			var got, want any
			if err := json.Unmarshal(profile.Settings[entry.name], &got); err != nil { t.Errorf("v%d missing %s", version, entry.name); continue }
			if err := json.Unmarshal([]byte(entry.value), &want); err != nil { t.Fatal(err) }
			if !reflect.DeepEqual(got, want) { t.Errorf("v%d %s=%s want %s", version, entry.name, profile.Settings[entry.name], entry.value) }
		}
	}
}

func TestMigrationDoesNotEmptyAProfileWithoutSettings(t *testing.T) {
	env := newProbeEnv()
	env.Storage[core.SCHEMA_VERSION_STORAGE_KEY] = json.RawMessage(`12`)
	env.Storage[core.PROFILE_STORAGE_KEY] = json.RawMessage(`{"auth":{"key":"qc-key","user":`+probeUser+`},"addons":[]}`)
	if err := runtime.MigrateStorageSchema(env); err != nil { t.Fatal(err) }
	var profile map[string]json.RawMessage
	if err := json.Unmarshal(env.Storage[core.PROFILE_STORAGE_KEY], &profile); err != nil || len(profile["auth"]) == 0 { t.Fatalf("migration discarded a non-empty profile: %s (%v)", env.Storage[core.PROFILE_STORAGE_KEY], err) }
}

'''


def _run_contract_probe(
    candidate: Path,
    *,
    timeout: float,
    environment: Mapping[str, str],
) -> tuple[dict[str, object], dict[str, object]]:
    """Execute the verifier-owned public-contract probe against the candidate.

    The probe exercises the declared wire contracts (RuntimeEvent/Action/Event
    envelopes, EnvError codes, protected storage keys, legacy JSON-RPC request)
    through the candidate's real API, independent of the candidate's own tests.
    """

    def _atom(status: str, evidence: object) -> dict[str, object]:
        return {"status": status, "source": "verifier-contract-probe", "evidence": evidence}

    root = Path(tempfile.mkdtemp(prefix="harbor-contract-", dir="/tmp"))
    receipt: dict[str, object] = {"kind": "contract-probe", "command": [], "returncode": None, "timed_out": False}
    try:
        (root / "go.mod").write_text(
            _CONTRACT_PROBE_GOMOD.format(candidate=str(candidate)), encoding="utf-8"
        )
        package = root / "contractprobe"
        package.mkdir(parents=True, exist_ok=True)
        (package / "probe_test.go").write_text(_CONTRACT_PROBE_GO, encoding="utf-8")
        (package / "behavior_test.go").write_text(_BEHAVIOR_PROBE_GO, encoding="utf-8")
        # Execute the shipped behavioral/state probes as independent packages.
        # The main harness's status wrappers and candidate's own tests do not
        # establish these postconditions. Neither package inspects the probe
        # module's filesystem or assumes it is the candidate repository.
        target_assets = Path(__file__).resolve().parents[1] / "assets" / "target" / "batches"
        for batch, filenames in (
            ("BATCH-F2P-0001", ("wire_probes_test.go",)),
            ("BATCH-STATE-0001", ("persisted_state_test.go", "protected_wire_test.go")),
        ):
            batch_package = root / batch.lower().replace("-", "_")
            batch_package.mkdir()
            for filename in filenames:
                shutil.copyfile(target_assets / batch / filename, batch_package / filename)
        probe_environment = dict(environment)
        probe_environment["GOFLAGS"] = "-mod=mod"
        probe_environment["GOPROXY"] = "off"
        budget = max(30, min(float(timeout), 300.0))
        command = ["go", "test", "-count=1", "-timeout", f"{int(budget)}s", "./..."]
        completed, raw_receipt = _run_with_receipt(
            command,
            kind="contract-probe",
            index=0,
            cwd=root,
            timeout=budget + 30.0,
            environment=probe_environment,
            unprivileged=False,
        )
        receipt = raw_receipt
        if completed is None:
            return _atom("INCONCLUSIVE", {"reason": "contract probe did not complete", "command": command}), receipt
        evidence = {
            "command": command,
            "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-2000:],
            "stderr_tail": (completed.stderr or "")[-2000:],
        }
        return _atom("PASS" if completed.returncode == 0 else "FAIL", evidence), receipt
    except OSError as exc:
        receipt["infrastructure_error"] = True
        receipt["diagnostic"] = str(exc)[-2000:]
        return _atom("INCONCLUSIVE", {"reason": f"contract probe setup failed: {exc}"}), receipt
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
        receipts=receipts,
        config=config,
        runtime=runtime,
        candidate=candidate,
        assets=assets,
        logs=logs,
        timeout=timeout,
        environment=environment,
    )
    contract_override, contract_receipt = _run_contract_probe(
        candidate,
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
    by_kind = {
        kind: [row for row in compared if row.get("kind") == kind]
        for kind in ("P2P", "F2P", "DIFFERENTIAL", "STATE")
    }
    # Candidate obligations (must exist / must pass) must derive from the
    # *source* baseline: the target baseline is generated from the migrated
    # reference solution and therefore contains test files the reference added.
    # Using it for the tamper "must exist" set falsely flags a legitimate
    # candidate that does not reproduce those reference-authored tests (the
    # standard negative control "reference solution minus its new test files").
    # The source baseline is authoritative; the target baseline is only an
    # expectation, not an obligation.
    baseline_name = (
        config.get("assets", {}).get("source_test_baseline")
        or config.get("assets", {}).get("target_test_baseline")
    )
    test_baseline = _load_json(assets / str(baseline_name)) if baseline_name else {"files": {}, "marker_counts": {}}
    evidence = build_hardgate_evidence(
        candidate,
        config=config,
        observations=compared,
        observation_errors=contract_errors,
        receipts=receipts,
        patch_applicable=not apply_failed,
        source_test_baseline=test_baseline,
        test_baseline_source="source-test-baseline",
        public_contract_override=contract_override,
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
        candidate_root=candidate,
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
    result["contract_probe"] = contract_receipt
    result["cheating"] = bool(hardgate.get("cheating"))
    if apply_failed:
        result["candidate_patch_error"] = patch_message
    result["receipts"] = {"commands": receipts, "harness_returncode": completed.returncode}
    logs.mkdir(parents=True, exist_ok=True)
    return _write_result(logs, result)


__all__ = ["REPO_ROOT_ENVIRONMENT_KEYS", "repo_root_environment", "run_harbor_verifier"]
