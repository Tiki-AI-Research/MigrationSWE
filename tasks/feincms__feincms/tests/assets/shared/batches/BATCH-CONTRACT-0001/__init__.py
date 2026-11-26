"""Shared executable public-contract probes for BATCH-CONTRACT-0001.

The sibling module :mod:`contract_harness` is loaded by path (the batch
directory name contains hyphens and is therefore not a valid Python package
name), so this package namespace only exposes the stable metadata.
"""

OBSERVATION_IDS = [
    "CONTRACT-001",
    "CONTRACT-002",
    "CONTRACT-003",
    "CONTRACT-004",
    "CONTRACT-005",
    "CONTRACT-006",
    "CONTRACT-007",
    "CONTRACT-008",
    "CONTRACT-009",
    "CONTRACT-010",
    "CONTRACT-011",
    "CONTRACT-012",
    "CONTRACT-013",
    "CONTRACT-014",
    "CONTRACT-015",
    "CONTRACT-016",
    "CONTRACT-017",
]

HARNESS_MODULE = "contract_harness.py"

__all__ = ["HARNESS_MODULE", "OBSERVATION_IDS"]
