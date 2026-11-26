#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../../.." && pwd)
repo=${SERIE_TARGET_REPO:-}
if [ -z "$repo" ]; then
  if [ -f /workspace/repo/go.mod ] && [ -d /workspace/repo/core ]; then
    repo=/workspace/repo
  elif [ -f /input/target/go.mod ]; then
    repo=/input/target
  elif [ -f "$root/target/go.mod" ]; then
    repo=$root/target
  else
    repo=/workspace/repo
  fi
fi
exec "$root/shared/batches/BATCH-F2P-0001/run_probe.sh" "$repo" "F2P-004"
