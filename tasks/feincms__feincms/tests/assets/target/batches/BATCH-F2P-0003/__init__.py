"""Target-side package for BATCH-F2P-0003.

The target probe runs the exact same predicates as the source probe against
the migrated tree; Django 5.2 behaviour and the removal of the version guards
make every postcondition hold.
"""

TARGET_ROOT = "/workspace/repo"

__all__ = ["TARGET_ROOT"]
