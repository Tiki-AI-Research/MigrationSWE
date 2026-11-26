#!/bin/sh
# P2P target-side entry point for BATCH-P2P-0001.
# Runs the repository's own tests against the target (6.3.2) checkout and
# emits the target-side PASS/FAIL report. Every long command is bounded by
# an explicit timeout inside the harness; this wrapper adds an outer bound.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)

REPO="${REPO_ROOT:-}"
if [ -z "$REPO" ] || [ ! -d "$REPO/packages" ]; then
    if [ -d /workspace/repo/packages ]; then
        REPO=/workspace/repo
    else
        REPO=$(cd "$HERE/../../.." && pwd)
    fi
fi

HARNESS="$HERE/../../../shared/batches/BATCH-P2P-0001/p2p_harness.js"
if [ ! -f "$HARNESS" ]; then
    HARNESS="$REPO/shared/batches/BATCH-P2P-0001/p2p_harness.js"
fi

exec timeout 3600 node "$HARNESS" --side target --repo "$REPO" "$@"
