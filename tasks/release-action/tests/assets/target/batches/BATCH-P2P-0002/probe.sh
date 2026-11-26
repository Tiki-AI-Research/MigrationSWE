#!/bin/sh
# Public action behavior adapter; module layout and candidate test names are free.
set -eu
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$here/../../../shared/integration/public_behavior.py" --batch BATCH-P2P-0002 --repo "${REPO:-${REPO_ROOT:-/workspace/repo}}" --out "${1:-/logs/p2p.json}"
