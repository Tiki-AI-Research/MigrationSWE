#!/bin/sh
# Target-side runner for BATCH-DIFFERENTIAL-0001.
#
# Copies the /input/target read-only reference checkout into a scratch tree,
# drops the shared differential probe next to the module and executes it. The
# resulting normalized report is compared against the source report by the
# shared "equal_after_normalization" gates.
set -eu

SIDE="target"
REPO="${REPO:-/input/target}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ASSETS="$(cd "$HERE/../../.." && pwd)"
PROBE="$ASSETS/shared/batches/BATCH-DIFFERENTIAL-0001/diffprobe/main.go"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

cp -R "$REPO"/. "$WORK"/
mkdir -p "$WORK/diffprobe"
cp "$PROBE" "$WORK/diffprobe/main.go"

if [ -d "$WORK/.gomodcache" ]; then
	GOMODCACHE="$WORK/.gomodcache"
else
	GOMODCACHE="/opt/migration/dependencies/gomodcache"
fi
export GOMODCACHE
export GOFLAGS="${GOFLAGS:--mod=mod}"
export GOPROXY="${GOPROXY:-off}"
export GOSUMDB="${GOSUMDB:-off}"

cd "$WORK"
if [ -n "${DIFF_OUT:-}" ]; then
	go run ./diffprobe > "$DIFF_OUT"
	echo "$SIDE differential report written to $DIFF_OUT"
else
	go run ./diffprobe
fi
