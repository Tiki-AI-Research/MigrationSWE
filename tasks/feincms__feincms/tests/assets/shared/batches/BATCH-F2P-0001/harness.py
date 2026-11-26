"""Process/command harness for batch BATCH-F2P-0001.

Every command that may block is executed through the external ``timeout``
utility *and* a ``subprocess`` timeout so the harness can never wait forever.
Commands that would require privileges or that can block forever are never
issued by this module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

DEFAULT_REPO = os.environ.get("MIGRATION_REPO", "/workspace/repo")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("MIGRATION_PROBE_TIMEOUT", "120"))


@dataclass
class CheckResult:
    observation_id: str
    passed: bool
    expected: Any = None
    observed: Any = None
    error_type: str = ""
    evidence: list = field(default_factory=list)


def _timeout_argv(argv: Sequence[str], timeout: int) -> list[str]:
    """Prefix ``argv`` with the coreutils ``timeout`` wrapper when available."""

    timeout_bin = shutil.which("timeout")
    if timeout_bin:
        return [timeout_bin, "--signal=TERM", "--kill-after=5", str(timeout), *argv]
    return [str(part) for part in argv]


def run_command(
    argv: Sequence[str],
    cwd: str = DEFAULT_REPO,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Run *argv* with a hard timeout. Never waits indefinitely."""

    merged = dict(os.environ)
    if env:
        merged.update({str(key): str(value) for key, value in env.items()})
    command = _timeout_argv([str(part) for part in argv], timeout)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=merged,
            timeout=timeout + 10,
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


def run_side_probe(entry: str, repo: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Execute a side probe entry point with an explicit timeout."""

    return run_command(
        [sys.executable, entry, "--repo", repo],
        cwd=os.path.dirname(os.path.abspath(entry)),
        timeout=timeout,
    )


def write_report(path: str, results: Sequence[Any]) -> str:
    payload = [result if isinstance(result, dict) else asdict(result) for result in results]
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    import json

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path


def run_batch(repo: str = DEFAULT_REPO):
    """Execute every observation owned by this batch and return raw results.

    The dependency batch ``BATCH-SKELETON-0001`` only provides the generic
    ``run_command``/``write_report`` primitives, so the observation dispatch is
    implemented here on top of them.  ``probes.run_all`` is the single source of
    truth for the observation inventory.
    """

    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "batch_f2p_0001_probes",
        os.path.join(here, "probes.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_all(os.path.abspath(repo))


def main(argv=None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run BATCH-F2P-0001 harness")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    results = run_batch(args.repo)
    payload = json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        write_report(args.output, results)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
