"""Shared corpus loader for BATCH-DIFFERENTIAL-0001.

Both the source-side probe and the target-side harness consume the same
immutable fixture corpus so that the two sides are compared on identical
inputs and the same normalized projection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

BATCH_ID = "BATCH-DIFFERENTIAL-0001"
FAMILY = "shared-case-corpus"
PROJECTION = "normalized-result"


def assets_root() -> Path:
    """Return the asset bundle root that contains fixtures/, shared/, ..."""
    return Path(__file__).resolve().parents[3]


def corpus_path() -> Path:
    return assets_root() / "fixtures" / "batches" / BATCH_ID / "corpus.json"


def load_corpus(path: Path | None = None) -> Dict[str, Any]:
    with open(path or corpus_path(), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("batch_id") != BATCH_ID:
        raise ValueError(f"corpus batch_id {data.get('batch_id')!r} != {BATCH_ID!r}")
    return data


def observation_ids(corpus: Dict[str, Any]) -> List[str]:
    return [case["observation_id"] for case in corpus["cases"]]


def resolve_input(corpus: Dict[str, Any], token: str) -> str:
    """Resolve a case input token to its literal text.

    A token that starts with "@" is an inline synthetic input; otherwise it is
    a key in the corpus ``inputs`` map.
    """
    if token == "not a plan":
        return token
    if token.startswith("@node_type:"):
        return token.split(":", 1)[1]
    if token.startswith("@split_balanced:"):
        return token.split(":", 1)[1]
    if token == "@array_first_element":
        return json.dumps([{"Plan": {"Node Type": "Seq Scan"}}])
    if token == "@object_no_plan":
        return json.dumps({"foo": 1})
    if token in corpus["inputs"]:
        return corpus["inputs"][token]
    return token


def case_by_id(corpus: Dict[str, Any], observation_id: str) -> Dict[str, Any]:
    for case in corpus["cases"]:
        if case["observation_id"] == observation_id:
            return case
    raise KeyError(observation_id)
