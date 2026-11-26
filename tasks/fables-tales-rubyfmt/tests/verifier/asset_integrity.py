#!/usr/bin/env python3
"""Delivery-time entrypoint for the public asset inventory check.

The packaging gate invokes this file with ``--assets <task>/tests/assets`` and
``--out <report>``.  It reuses the same validator the verifier runs
(``integrity.validate_public_integrity``), so the archive is checked against
exactly the manifest the task ships.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from integrity import validate_public_integrity  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", required=True, help="path to tests/assets")
    parser.add_argument("--out", required=True, help="path of the JSON report to write")
    args = parser.parse_args(argv)

    assets = Path(args.assets).resolve()
    tests_root = assets.parent
    report: dict[str, object] = {
        "schema_version": "migration-asset-integrity-check:v1",
        "passed": False,
        "success": False,
        "errors": [],
        "summary": "",
    }
    errors = report["errors"]
    try:
        validate_public_integrity(tests_root)
    except Exception as exc:  # fail closed and report the reason
        assert isinstance(errors, list)
        errors.append(str(exc))
        report["summary"] = f"public asset inventory failed: {exc}"
        status = 2
    else:
        report["passed"] = True
        report["success"] = True
        report["summary"] = "public asset inventory verified"
        status = 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"success": report["success"], "summary": report["summary"]}))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
