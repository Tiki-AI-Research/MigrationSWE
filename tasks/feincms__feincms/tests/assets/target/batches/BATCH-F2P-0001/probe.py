"""Entry point for the target side of batch BATCH-F2P-0001.

The target technology is Django 5.2.  The probe must be executed against a real
reference target checkout before its result is reported.  The default
repository root is consumed from the dependency batch ``BATCH-SKELETON-0001``
so the integration runner can dispatch this module without arguments.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys


def _repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from target.skeleton import TARGET_ROOT  # type: ignore

        return TARGET_ROOT
    except Exception:
        return os.environ.get("MIGRATION_TARGET_ROOT", "/workspace/repo")


def _load_shared():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    shared = os.path.join(root, "shared", "batches", "BATCH-F2P-0001", "probes.py")
    spec = importlib.util.spec_from_file_location("batch_f2p_0001_probes", shared)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_all(repo: str):
    return _load_shared().run_all(os.path.abspath(repo))


def run_observation(observation_id: str, repo: str):
    return _load_shared().run_observation(observation_id, os.path.abspath(repo))


def _write(results, output):
    payload = json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True)
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(payload)
    else:
        print(payload)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Target side probes for BATCH-F2P-0001")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    repo = os.path.abspath(args.repo or _repo_root())
    _write(run_all(repo), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
