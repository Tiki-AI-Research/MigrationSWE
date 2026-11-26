#!/usr/bin/env python3
"""Target-side INTEGRATION entry point for the FeinCMS Django 3.2 -> 5.2 task.

Executed as ``python3 {assets}/target/runner.py {report}``.  It loads the shared
integration runner and dispatches every target-side dependency batch, emitting
the merged ``migration-observations:v6`` report.
"""

from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_ROOT = os.path.dirname(HERE)
MODULE_PATH = os.path.join(ASSET_ROOT, "shared", "integration", "integration_runner.py")


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "migration_integration_runner", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    runner = _load_runner()
    return runner.main(list(sys.argv[1:] if argv is None else argv), side="target")


if __name__ == "__main__":
    raise SystemExit(main())
