"""Fixture assets for BATCH-F2P-0002."""

import json
import os

FIXTURES_DIR = os.path.dirname(os.path.abspath(__file__))
RESIDUE_TOKENS_PATH = os.path.join(FIXTURES_DIR, "residue_tokens.json")


def residue_tokens():
    with open(RESIDUE_TOKENS_PATH, encoding="utf-8") as handle:
        return json.load(handle)


__all__ = ["FIXTURES_DIR", "RESIDUE_TOKENS_PATH", "residue_tokens"]
