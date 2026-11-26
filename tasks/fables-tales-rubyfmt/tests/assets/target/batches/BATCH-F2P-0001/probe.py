#!/usr/bin/env python3
import importlib.util
import pathlib
import sys

BATCH_ID = "BATCH-F2P-0001"

shared = pathlib.Path(__file__).resolve().parents[3] / "shared" / "batches" / BATCH_ID / "probe.py"
spec = importlib.util.spec_from_file_location("rubyfmt_f2p_probe", shared)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

if __name__ == "__main__":
    sys.exit(module.main(sys.argv[1:]))
