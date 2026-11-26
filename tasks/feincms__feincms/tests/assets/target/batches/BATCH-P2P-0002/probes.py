"""Target-side entry point for the BATCH-P2P-0002 state-preservation probes."""

from __future__ import annotations

import importlib.util
import os
import sys

BATCH_DIR = "BATCH-P2P-0002"


def _default_root() -> str:
    env = os.environ.get("MIGRATION_REPO")
    if env:
        return env
    for candidate in ("/workspace/repo", "/input/target"):
        if os.path.isdir(os.path.join(candidate, "feincms")):
            return candidate
    return "/workspace/repo"


TARGET_ROOT = _default_root()


def load_shared_harness():
    here = os.path.dirname(os.path.abspath(__file__))
    assets_root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    harness_path = os.path.join(
        assets_root, "shared", "batches", BATCH_DIR, "harness.py"
    )
    spec = importlib.util.spec_from_file_location(
        "batch_p2p_0002_shared_harness", harness_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def observations() -> tuple:
    """Return the observation IDs owned by this batch."""

    return load_shared_harness().OBSERVATION_IDS


def run_probes(repo: str | None = None) -> list:
    """Return the target-side executable projections (no report written)."""

    harness = load_shared_harness()
    return harness.run_probes(repo or TARGET_ROOT, "target")


def run(report_path: str, repo: str | None = None) -> list:
    harness = load_shared_harness()
    results = harness.run_probes(repo or TARGET_ROOT, "target")
    harness.write_report(report_path, results)
    return results


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    report = argv[0] if argv else "/logs/result.json"
    run(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
