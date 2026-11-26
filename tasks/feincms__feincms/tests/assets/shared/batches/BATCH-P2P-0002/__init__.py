from __future__ import annotations

import importlib.util
import os


def load_harness():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "harness.py")
    spec = importlib.util.spec_from_file_location("batch_p2p_0002_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


__all__ = ["load_harness"]
