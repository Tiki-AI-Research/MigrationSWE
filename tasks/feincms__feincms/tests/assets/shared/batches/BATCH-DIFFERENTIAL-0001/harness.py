"""Shared harness helpers for the BATCH-DIFFERENTIAL-0001 probes.

Both the source and the target probe run inside their own container.  Every
child process started from here is wrapped in an explicit wall-clock ``timeout``
so the harness can never block indefinitely; no command is allowed to run
without an upper bound.

The helpers are deliberately dependency free so that the probes can import them
without pulling in Django.
"""

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

DEFAULT_REPO = os.environ.get("MIGRATION_REPO", "/workspace/repo")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("MIGRATION_COMMAND_TIMEOUT", "120"))
MAX_TIMEOUT_SECONDS = 600


def _clamp_timeout(timeout: int) -> int:
    """Keep every command bounded between 1 second and ``MAX_TIMEOUT_SECONDS``."""

    try:
        value = int(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        value = DEFAULT_TIMEOUT_SECONDS
    return min(value, MAX_TIMEOUT_SECONDS)


@dataclass
class CheckResult:
    """A single observation result produced by a probe."""

    observation_id: str
    passed: bool
    expected: Any = None
    observed: Any = None
    error_type: str = ""
    evidence: list = field(default_factory=list)


def run_command(
    argv: Sequence[str],
    cwd: str = DEFAULT_REPO,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Run ``argv`` with an explicit timeout and capture its output."""

    merged = dict(os.environ)
    if env:
        merged.update({str(key): str(value) for key, value in env.items()})
    command = [str(part) for part in argv]
    limit = _clamp_timeout(timeout)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            timeout=limit,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": command,
            "returncode": None,
            "timed_out": True,
            "timeout": limit,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
        }
    except OSError as exc:
        return {
            "argv": command,
            "returncode": 127,
            "timed_out": False,
            "timeout": limit,
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "argv": command,
        "returncode": completed.returncode,
        "timed_out": False,
        "timeout": limit,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def normalize(value: Any) -> Any:
    """Normalise a probe result into a stable, JSON comparable shape.

    Mappings are sorted by key, sets are converted to sorted lists and floats
    are rounded so that two runs which only differ by noise compare equal.
    Human readable text is never used as an answer by the probes; the values
    that flow through here are booleans, status codes, class names, field
    names, numbers and file content hashes.
    """

    if isinstance(value, dict):
        return {str(key): normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [normalize(item) for item in sorted(value, key=str)]
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return repr(value)


def equal_after_normalization(left: Any, right: Any) -> bool:
    """The DIFFERENTIAL relation used by this batch's gates."""

    return normalize(left) == normalize(right)


def write_report(path: str, results: Sequence[Any]) -> str:
    """Persist probe results as JSON, creating parent directories."""

    payload = [
        result if isinstance(result, dict) else asdict(result) for result in results
    ]
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path
