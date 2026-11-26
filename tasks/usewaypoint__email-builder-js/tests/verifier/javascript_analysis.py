"""Trusted TypeScript AST analysis of text; candidate code is never executed."""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=512)
def analyze_javascript(source: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", str(Path(__file__).with_suffix(".cjs"))],
        input=json.dumps([{"filename": "candidate.tsx", "source": source}]),
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
        cwd="/",
        # Do not inherit candidate NODE_OPTIONS or module search paths.
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("invalid JavaScript analysis response")
    return rows[0]
