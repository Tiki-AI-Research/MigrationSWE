import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

DEFAULT_REPO = os.environ.get("MIGRATION_REPO", "/workspace/repo")
DEFAULT_TIMEOUT_SECONDS = 300


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
