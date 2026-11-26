#!/usr/bin/env python3
"""Expose the protected-path gate before the hidden test patch is applied.

``test.sh`` uses this as a preflight: if a candidate already created or edited
an evaluator-owned path (most importantly the hidden harness location), the
hidden patch cannot be applied safely.  The collision must be scored as a
protected-path violation, never as an infrastructure failure.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _load_hardgate() -> Any:
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("jssip_protected_paths_hardgate", here / "hardgate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        protected = [str(item) for item in config.get("protected_paths") or []]
        status, message = _load_hardgate()._protected_path_check(
            args.repo.resolve(), protected
        )
    except Exception as exc:  # noqa: BLE001 - fail closed as infrastructure
        print(json.dumps({"status": "error", "message": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {"status": status, "message": message},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if status in {"passed", "na"} else 1


if __name__ == "__main__":
    sys.exit(main())
