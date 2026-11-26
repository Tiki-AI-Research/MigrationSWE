#!/usr/bin/env python3
"""P2P results measured through public behavior, independent of Go test names."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared/integration"))
from public_behavior import main
if __name__ == "__main__":
    sys.argv += ["--batch", "BATCH-P2P-0001"]
    raise SystemExit(main())
