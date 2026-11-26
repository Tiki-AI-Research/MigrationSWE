"""Compatibility entry point for the single secure Harbor verifier path."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Mapping

sys.dont_write_bytecode = True

try:
    from .harbor_entry import run_harbor_verifier
except ImportError:  # Executed as ``python verifier/verifier.py``.
    script_dir = Path(__file__).resolve().parent
    sys.path[:] = [item for item in sys.path if Path(item or ".").resolve() != script_dir]
    sys.modules.pop("verifier", None)
    sys.path.insert(0, str(script_dir.parent))
    from verifier.harbor_entry import run_harbor_verifier


def _write_ctrf(path: Path, result: Mapping[str, object]) -> None:
    rows = []
    observations = result.get("observations", [])
    if isinstance(observations, list):
        for index, row in enumerate(observations):
            if not isinstance(row, Mapping):
                continue
            status = str(row.get("status", "INCONCLUSIVE"))
            kind = str(row.get("kind", "UNKNOWN"))
            identifier = str(row.get("id", f"observation-{index + 1}"))
            rows.append({
                "testId": identifier,
                "name": identifier,
                "status": "passed" if status == "PASS" else "failed" if status == "FAIL" else "other",
                "duration": 0,
                "message": str(row.get("reason", "")),
                "extra": {
                    "deepswe": {
                        "group": kind if kind in {"P2P", "F2P"} else "diagnostic",
                        "required": kind in {"P2P", "F2P"},
                    },
                    "migration": {"kind": kind, "status": status},
                },
            })
    payload = {
        "reportFormat": "CTRF",
        "specVersion": "0.1.0",
        "reportId": str(uuid.uuid4()),
        "runId": "migration-verifier",
        "generatedBy": "migration-verifier",
        "results": {
            "tool": {"name": "migration-verifier"},
            "summary": {
                "tests": len(rows),
                "passed": sum(row["status"] == "passed" for row in rows),
                "failed": sum(row["status"] == "failed" for row in rows),
                "skipped": 0,
                "pending": 0,
                "other": sum(row["status"] == "other" for row in rows),
                "start": 0,
                "stop": 0,
            },
            "tests": rows,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_legacy(args: argparse.Namespace) -> int:
    result = run_harbor_verifier(
        candidate_root=args.repo,
        bundle_root=args.config.parent,
        logs_root=args.hard_gate_output.parent,
        apply_failed=bool(args.apply_failed),
        patch_message="legacy verifier reported an unapplied patch" if args.apply_failed else "",
    )
    verifier_path = args.hard_gate_output.parent / "verifier-result.json"
    _write_ctrf(args.ctrf_output, result)
    args.hard_gate_output.parent.mkdir(parents=True, exist_ok=True)
    args.hard_gate_output.write_text(
        json.dumps({
            "schema_version": "migration-hardgate-result:v2",
            "passed": result.get("hardgate") == "PASS" and not bool(result.get("cheating")),
            "status": result.get("hardgate", "INCONCLUSIVE"),
            "cheating": bool(result.get("cheating")),
            "verifier_result": verifier_path.name,
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--ctrf-output", type=Path)
    parser.add_argument("--hard-gate-output", type=Path)
    parser.add_argument("--apply-failed", type=int, choices=(0, 1), default=0)
    parser.add_argument("--docker", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    legacy_args = (args.repo, args.ctrf_output, args.hard_gate_output)
    if any(value is not None for value in legacy_args):
        if not all(value is not None for value in legacy_args):
            parser.error("--repo, --ctrf-output, and --hard-gate-output are required together")
        return _run_legacy(args)
    if not args.candidate_root or not args.output:
        parser.error("--candidate-root and --output are required")
    result = run_harbor_verifier(
        candidate_root=args.candidate_root,
        bundle_root=args.config.parent,
        logs_root=args.output.parent,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
