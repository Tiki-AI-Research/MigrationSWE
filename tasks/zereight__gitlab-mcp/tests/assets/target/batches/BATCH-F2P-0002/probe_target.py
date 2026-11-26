#!/usr/bin/env python3
"""Target-side probe runner for BATCH-F2P-0002.

Executes every shared F2P predicate against the reference Python target that is
selected by ``OMG_REPO_ROOT`` (default ``/input/target``) and writes a JSON
report mapping each observation id to its raw result.

Exit code is 0 whenever the report is produced; the report itself carries the
per-observation status. A missing target capability shows up as ``FAIL`` in the
report, never as a silent pass.
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_CANDIDATES = [
    os.environ.get("OMG_F2P_SHARED"),
    "/assets/shared/batches/BATCH-F2P-0002",
    os.path.normpath(os.path.join(_HERE, "..", "..", "..", "shared", "batches", "BATCH-F2P-0002")),
    os.path.normpath(os.path.join(_HERE, "..", "..", "shared", "batches", "BATCH-F2P-0002")),
]
_SHARED = next(
    (path for path in _SHARED_CANDIDATES if path and os.path.isdir(path)),
    _SHARED_CANDIDATES[1],
)
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

os.environ.setdefault("OMG_REPO_ROOT", "/input/target")

import f2p_predicates  # noqa: E402


def main(argv: list) -> int:
    report_path = argv[1] if len(argv) > 1 else None
    results = f2p_predicates.run_all()
    payload = {
        "batch_id": "BATCH-F2P-0002",
        "side": "target",
        "repo_root": os.environ["OMG_REPO_ROOT"],
        "observations": results,
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
