"""Harness helpers for BATCH-F2P-0002.

The generic command/report helpers are consumed from the read-only skeleton
dependency (BATCH-SKELETON-0001) instead of being copied.  The dependency is
located through the dependency snapshot path when it is not available from the
asset root.

Every command executed through :func:`run_command` is bounded by an explicit
``timeout`` so that the harness can never block forever.
"""

import importlib.util
import os
from dataclasses import dataclass, field

_SKELETON_REL = ("shared", "skeleton", "harness.py")
_DEPENDENCY_SKELETON = (
    "/assets/dependencies/batches/"
    "BATCH-AUTHOR-BATCH-SKELETON-0001/shared/skeleton/harness.py"
)


@dataclass
class ProbeResult:
    observation_id: str
    passed: bool
    expected: str = "target-postcondition"
    observed: dict = field(default_factory=dict)
    error_type: str = ""
    evidence: list = field(default_factory=list)


def _asset_root_candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.abspath(os.path.join(here, "..", "..", ".."))
    configured = os.environ.get("MIGRATION_ASSET_ROOT")
    if configured:
        yield configured
    yield "/workspace/repo"


def _find_skeleton():
    for root in _asset_root_candidates():
        candidate = os.path.join(root, *_SKELETON_REL)
        if os.path.isfile(candidate):
            return candidate
    if os.path.isfile(_DEPENDENCY_SKELETON):
        return _DEPENDENCY_SKELETON
    raise FileNotFoundError("skeleton harness dependency not found")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_skeleton = _load_module("f2p0002_skeleton_harness", _find_skeleton())

run_command = _skeleton.run_command
write_report = _skeleton.write_report
DEFAULT_REPO = _skeleton.DEFAULT_REPO
DEFAULT_TIMEOUT_SECONDS = _skeleton.DEFAULT_TIMEOUT_SECONDS

__all__ = [
    "DEFAULT_REPO",
    "DEFAULT_TIMEOUT_SECONDS",
    "ProbeResult",
    "run_command",
    "write_report",
]
