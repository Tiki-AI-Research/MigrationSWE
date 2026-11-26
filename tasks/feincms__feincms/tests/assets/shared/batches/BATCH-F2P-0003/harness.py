"""Timeout-bounded harness for the BATCH-F2P-0003 F2P predicates.

Every observation predicate is executed through an explicit ``timeout``
process wrapper *and* a ``subprocess`` timeout, so the harness can never wait
forever on a hanging interpreter or test run.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

DEFAULT_REPO = os.environ.get("MIGRATION_REPO", "/workspace/repo")
CHECK_TIMEOUT_SECONDS = 150
RUNNER = Path(__file__).resolve().parent / "run_one.py"
RESULT_MARKER = "__F2P_RESULT__"
DEPENDENCIES_ROOT = os.environ.get("F2P0003_DEPENDENCIES", "/assets/dependencies")


def _has_repo_marker(path: str) -> bool:
    """True when *path* looks like the FeinCMS repository under test."""
    try:
        if not os.path.isdir(path):
            return False
    except OSError:
        return False
    return os.path.isdir(os.path.join(path, "feincms")) or os.path.isdir(
        os.path.join(path, "tests")
    )


def looks_like_report(value: str) -> bool:
    """Heuristically detect a report path passed by the integration runner."""
    base = os.path.basename(value)
    return (
        value.endswith(".json")
        or value.startswith("/logs")
        or "report" in base.lower()
    )


def resolve_repo_root(side: str, argv_repo: str | None = None) -> str:
    """Locate the repository to probe for *side* (``source``/``target``).

    The integration runner may invoke a batch probe without a repository
    argument, with the repository at ``/workspace/repo`` (the runtime
    materialization point), or, during authoring, with the read-only trees at
    ``/input/source`` and ``/input/target``.  Resolution never hardcodes an
    outcome: it only selects an existing checkout.
    """
    candidates: list[str] = []
    if argv_repo:
        candidates.append(argv_repo)
    env_repo = os.environ.get("MIGRATION_REPO")
    if env_repo:
        candidates.append(env_repo)
    if side == "source":
        candidates.append("/input/source")
    elif side == "target":
        candidates.append("/input/target")
    candidates.append("/workspace/repo")
    candidates.append(os.getcwd())
    for candidate in candidates:
        if candidate and _has_repo_marker(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(argv_repo or env_repo or "/workspace/repo")


def parse_probe_arguments(side: str, argv: Sequence[str]) -> tuple[str, str | None]:
    """Return ``(repo_root, report_path)`` from heterogeneous probe arguments.

    Accepts ``--repo/--report`` flags, bare repository directories and bare
    report paths in either order, so the probe works both when driven by the
    integration runner and when invoked manually during authoring.
    """
    argv_repo: str | None = None
    report: str | None = None
    extras: list[str] = []
    index = 0
    values = [str(part) for part in argv]
    while index < len(values):
        token = values[index]
        if token in ("--repo", "--repo-root", "--root", "--report", "--out", "--output"):
            index += 1
            if index >= len(values):
                break
            if token in ("--report", "--out", "--output"):
                report = values[index]
            else:
                argv_repo = values[index]
        elif token.startswith("--repo="):
            argv_repo = token.split("=", 1)[1]
        elif token.startswith(("--report=", "--out=", "--output=")):
            report = token.split("=", 1)[1]
        else:
            extras.append(token)
        index += 1
    for token in extras:
        if _has_repo_marker(token):
            argv_repo = token
        elif looks_like_report(token):
            report = token
        elif argv_repo is None and os.path.isdir(token):
            argv_repo = token
        elif report is None:
            report = token
    return resolve_repo_root(side, argv_repo), report


def _consume_dependency():
    """Reuse the shared skeleton harness produced by BATCH-SKELETON-0001.

    The dependency snapshot is mounted read-only; nothing is copied or
    rewritten.  When the dependency is not mounted (e.g. during authoring in
    the write workspace) we fall back to the local timeout-bounded helpers.
    """
    dependency_batch = "BATCH-AUTHOR-BATCH-SKELETON-0001"
    shared_dir = os.path.join(
        DEPENDENCIES_ROOT, "batches", dependency_batch, "shared"
    )
    manifest = os.path.join(DEPENDENCIES_ROOT, "dependency-manifest.json")
    batch_ids = []
    try:
        with open(manifest, encoding="utf-8") as handle:
            batch_ids = [b.get("batch_id") for b in json.load(handle)["batches"]]
    except (OSError, ValueError, KeyError):
        pass
    if os.path.isdir(shared_dir) and shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)
    try:
        from skeleton.harness import run_command as shared_run_command

        return batch_ids, shared_run_command
    except Exception:  # noqa: BLE001 - dependency is optional at author time
        return batch_ids, None


DEPENDENCY_BATCH_IDS, _SHARED_RUN_COMMAND = _consume_dependency()

OBSERVATION_IDS = (
    "F2P-049",
    "F2P-051",
    "F2P-054",
    "F2P-055",
    "F2P-056",
    "F2P-057",
    "F2P-052",
    "F2P-050",
    "F2P-053",
    "F2P-058",
)


def run_command(
    argv: Sequence[str],
    cwd: str = DEFAULT_REPO,
    timeout: int = CHECK_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> dict:
    merged = dict(os.environ)
    if env:
        merged.update({str(key): str(value) for key, value in env.items()})
    command = [str(part) for part in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": command,
            "returncode": None,
            "timed_out": True,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
        }
    except OSError as exc:
        return {
            "argv": command,
            "returncode": 127,
            "timed_out": False,
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "argv": command,
        "returncode": completed.returncode,
        "timed_out": False,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _parse_result(stdout: str):
    for line in reversed(stdout.splitlines()):
        if RESULT_MARKER in line:
            return json.loads(line.split(RESULT_MARKER, 1)[1])
    return None


def run_observation(observation_id: str, repo_root: str = DEFAULT_REPO) -> dict:
    argv = [
        "timeout",
        str(CHECK_TIMEOUT_SECONDS),
        sys.executable,
        str(RUNNER),
        observation_id,
        repo_root,
    ]
    runner = _SHARED_RUN_COMMAND or run_command
    outcome = runner(argv, cwd=repo_root, timeout=CHECK_TIMEOUT_SECONDS + 30)
    if outcome["timed_out"]:
        return {
            "observation_id": observation_id,
            "passed": False,
            "error_type": "INFRA_ERROR",
            "observed": {"timed_out": True},
            "evidence": [],
        }
    parsed = _parse_result(outcome["stdout"])
    if parsed is None:
        return {
            "observation_id": observation_id,
            "passed": False,
            "error_type": "INFRA_ERROR",
            "observed": {"returncode": outcome["returncode"]},
            "evidence": [outcome["stderr"][-400:]],
        }
    parsed["observation_id"] = observation_id
    return parsed


def run_batch(repo_root: str = DEFAULT_REPO) -> list:
    return [run_observation(oid, repo_root) for oid in OBSERVATION_IDS]


def write_report(path: str, results: Sequence[Mapping]) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(list(results), handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path


if __name__ == "__main__":
    default_report = os.path.join(
        os.environ.get("MIGRATION_REPORT_DIR", "/tmp"), "batch-f2p-0003.json"
    )
    side = os.environ.get("F2P0003_SIDE", "target")
    repo, report = parse_probe_arguments(side, sys.argv[1:])
    results = run_batch(repo)
    output = report or default_report
    write_report(output, results)
    print(json.dumps(results, sort_keys=True))
