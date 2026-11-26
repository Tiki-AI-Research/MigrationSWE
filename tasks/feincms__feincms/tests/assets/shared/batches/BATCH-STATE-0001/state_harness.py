"""Shared subprocess helpers for the BATCH-STATE-0001 persistent-state checks.

Every command that could run for a long time is executed through
:func:`run_command`, which always applies an explicit ``timeout``.  The harness
never waits indefinitely and never invokes a privileged or interactive command.

The per-observation executable projection itself lives next to the batch in
``source/batches/BATCH-STATE-0001/state_probe.py`` and
``target/batches/BATCH-STATE-0001/state_probe.py``; both expose ``run()`` and
``project()`` returning entries of the shape
``{"observation_id": ..., "status": "PASS"|"FAIL", "value": {...}}``.
"""

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

DEFAULT_REPO = os.environ.get("MIGRATION_REPO", "/workspace/repo")
DEFAULT_TIMEOUT_SECONDS = 300
OBSERVATIONS = ("STATE-001", "STATE-002")


@dataclass
class CheckResult:
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
    """Run *argv* with a hard timeout and return a JSON-serialisable mapping."""

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


def normalize(entry: Mapping[str, Any]) -> dict:
    """Reduce a raw projection entry to its observable fields.

    Only the observation id, the normalised status and the JSON value are kept
    so that assertions compare observable state and never human readable text.
    """

    status = str(entry.get("status") or ("PASS" if entry.get("result") == "PASS" else "FAIL"))
    return {
        "observation_id": entry.get("observation_id"),
        "status": status.upper(),
        "value": entry.get("value"),
    }


def collect(entrypoints: Mapping[str, Mapping[str, Any]]) -> list:
    """Normalise ``{observation_id: {"status": ..., "value": ...}}`` mappings."""

    return [normalize(value) for _, value in sorted(entrypoints.items())]


def summarize(
    observation_id: str,
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
    evidence_refs: Sequence[str] | None = None,
) -> CheckResult:
    """Build a :class:`CheckResult` from raw probe output.

    ``observed`` and ``expected`` are normalised to booleans so assertions only
    compare observable state, never human readable messages.
    """

    passed = bool(observed.get("ok")) and all(
        observed.get(key) == value for key, value in expected.items()
    )
    error_type = "" if passed else str(observed.get("error_type", "StateMismatch"))
    return CheckResult(
        observation_id=observation_id,
        passed=passed,
        expected=dict(expected),
        observed=dict(observed),
        error_type=error_type,
        evidence=list(evidence_refs or []),
    )


def write_report(path: str, results: Sequence[Any]) -> str:
    payload = [
        result if isinstance(result, dict) else asdict(result) for result in results
    ]
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path
