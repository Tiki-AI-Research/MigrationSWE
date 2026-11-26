#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../../.." && pwd)
exec python3 "$root/shared/batches/BATCH-DIFFERENTIAL-0001/probe.py" --binary "${1:?binary path required}" --expected "$root/fixtures/batches/BATCH-DIFFERENTIAL-0001/source-normalized.json"
