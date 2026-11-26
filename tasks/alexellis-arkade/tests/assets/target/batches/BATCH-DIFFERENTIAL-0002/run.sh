#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
shared="$here/../../../shared/batches/BATCH-DIFFERENTIAL-0002/probe.py"
repo="${MIGRATION_REPO:-/input/target}"
out="${TMPDIR:-/tmp}/arkade-differential-target.json"
if [ "$#" -ge 1 ]; then
  case "$1" in
    *.json) out="$1" ;;
    *) repo="$1" ;;
  esac
fi
if [ "$#" -ge 2 ]; then
  out="$2"
fi
exec python3 "$shared" --repo "$repo" --out "$out"
