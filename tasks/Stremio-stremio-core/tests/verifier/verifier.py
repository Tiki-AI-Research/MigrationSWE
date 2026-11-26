"""Standalone entry point for the delivered public verifier bundle.

The graded Harbor path is ``grader.py`` -> ``harbor_entry.run_harbor_verifier``.
This module is a thin CLI wrapper around that exact implementation so the
independent verifier gate can score a candidate repository directly::

    python3 tests/verifier/verifier.py \
        --candidate-root /path/to/candidate \
        --config /tests/config.json \
        --output /logs/verifier/verifier-result.json

It never reads the legacy bundle layout and never starts Docker itself;
candidate isolation, integrity validation and scoring all stay inside
``run_harbor_verifier``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

sys.dont_write_bytecode = True

# ``verifier.py`` lives inside the ``verifier`` package directory, so running it
# as a file puts that directory (not its parent) first on ``sys.path`` and the
# wrapper would shadow the package when importing ``verifier.harbor_entry``.
# Drop that entry for the import and resolve the package from the bundle root.
_HERE = Path(__file__).resolve().parent
sys.path = [str(_HERE.parent)] + [
    entry for entry in sys.path if Path(entry or ".").resolve() != _HERE
]

from verifier.harbor_entry import run_harbor_verifier  # noqa: E402


def _infrastructure_result(message: str) -> dict[str, object]:
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
        "receipts": {"commands": [], "harness_returncode": None},
        "reward": {"branch": "inconclusive", "value": 0.0},
    }


def _write_result(output: Path, result: Mapping[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dict(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score a candidate repository with the migration verifier."
    )
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    bundle = args.config.resolve().parent
    logs = output.parent
    try:
        result = run_harbor_verifier(
            candidate_root=args.candidate_root.resolve(),
            bundle_root=bundle,
            logs_root=logs,
        )
    except Exception as exc:
        # The standalone gate must always leave a valid artifact behind
        # (fail closed), so an unexpected failure becomes an infrastructure
        # result instead of an unhandled traceback.
        result = _infrastructure_result(f"{type(exc).__name__}: {exc}")
    # ``run_harbor_verifier`` writes ``<logs>/verifier-result.json``; when
    # ``--output`` names a different path, mirror that authoritative result.
    authoritative = logs / "verifier-result.json"
    if authoritative.is_file() and authoritative != output:
        result = json.loads(authoritative.read_text(encoding="utf-8"))
    _write_result(output, result)
    return 0 if str(result.get("status")) == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
