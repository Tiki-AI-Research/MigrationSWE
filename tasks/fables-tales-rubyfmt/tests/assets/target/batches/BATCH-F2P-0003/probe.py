#!/usr/bin/env python3
import sys
from pathlib import Path

batch_dir = Path(__file__).resolve().parents[3] / "shared" / "batches" / "BATCH-F2P-0003"
sys.path.insert(0, str(batch_dir))

from probe_support import main

if __name__ == "__main__":
    sys.exit(main())
