"""Shared constants and helpers for the BATCH-F2P-0001 target probes.

BATCH-F2P-0001 is a target-only F2P batch for the Rust -> Go itertools
migration. Every observation asserts a *Go* postcondition of the frozen
contract, so the source (Rust) side must report FAIL and the target (Go) side
must report PASS for the same concrete entry/call.

Both probe sides import this module (when the shared tree is assembled) so that
the observation list and the source/target expectations stay in sync.
"""

import json
import os

BATCH_ID = "BATCH-F2P-0001"
SCHEMA_VERSION = "migration-asset-fragment:v2"
RESULT_SCHEMA_VERSION = "migration-result:v1"

SOURCE_TECHNOLOGY = {"name": "Rust", "version": "2018"}
TARGET_TECHNOLOGY = {"name": "Go", "version": "1.23.0"}

# Ownership order matches the authoring input for this batch.
OBSERVATION_IDS = [
    "F2P-001", "F2P-003", "F2P-004", "F2P-005", "F2P-006", "F2P-007",
    "F2P-008", "F2P-013", "F2P-016", "F2P-022", "F2P-024", "F2P-002",
    "F2P-009", "F2P-010", "F2P-011", "F2P-012", "F2P-014", "F2P-015",
    "F2P-017", "F2P-018", "F2P-019", "F2P-020", "F2P-021", "F2P-023",
]

CORPUS_RELATIVE_PATH = "fixtures/batches/BATCH-F2P-0001/corpus.json"


def load_corpus(root):
    """Load this batch's corpus relative to a repository root."""
    with open(os.path.join(root, CORPUS_RELATIVE_PATH), encoding="utf-8") as handle:
        return json.load(handle)


def observation_ids():
    return list(OBSERVATION_IDS)
