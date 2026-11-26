"""Shared, side-agnostic assets for batch BATCH-F2P-0001.

``probes.py`` implements the same semantic predicate for the source and the
target side; ``harness.py`` provides the timeout guarded command runner used to
execute every side probe.  The modules are loaded by absolute path from the
``source/`` and ``target/`` entry points because the batch directory name is
not a valid Python identifier.
"""

BATCH_ID = "BATCH-F2P-0001"

__all__ = ["BATCH_ID"]
