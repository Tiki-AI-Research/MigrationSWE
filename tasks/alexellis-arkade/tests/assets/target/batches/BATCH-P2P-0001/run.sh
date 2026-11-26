#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ASSET_ROOT=$(CDPATH= cd -- "$HERE/../../.." && pwd)
REPORT=${1:-"/tmp/arkade-BATCH-P2P-0001-target-report.json"}
REPO=${MIGRATION_REPO:-${REPO:-/workspace/repo}}
exec python3 "$ASSET_ROOT/shared/batches/BATCH-P2P-0001/probe.py" \
  --repo "$REPO" --side target --out "$REPORT"
