#!/bin/sh
# Public action behavior adapter; module layout and candidate test names are free.
set -eu
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$here/../../../shared/integration/public_behavior.py" --batch BATCH-F2P-0002 --repo "${1:-/workspace/repo}" --out "${2:-/logs/result.json}"
