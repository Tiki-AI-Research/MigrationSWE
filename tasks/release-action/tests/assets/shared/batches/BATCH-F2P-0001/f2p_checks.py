#!/usr/bin/env python3
"""Shared F2P adapter using public behavior rather than reference test names."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared/integration"))
from public_behavior import main
if __name__ == "__main__":
    raise SystemExit(main())
