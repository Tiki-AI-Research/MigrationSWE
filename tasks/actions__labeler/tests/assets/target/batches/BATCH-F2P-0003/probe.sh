#!/bin/sh
# Target-side F2P probe for BATCH-F2P-0003.
#
# Applies the frozen target-technology predicate: every observation is
# exercised against the Go 1.24.0 implementation built from cmd/labeler/main.go.
# Every potentially long-running command is wrapped in an explicit `timeout` so
# the harness can never wait forever, and no privileged or blocking case is run.
set -u

REPO_ROOT="${1:-/workspace/repo}"
cd "$REPO_ROOT" || {
  echo "INFRA_ERROR: repository root not found: $REPO_ROOT" >&2
  exit 2
}

if [ "${GOMODCACHE:-}" = "" ] && [ -d "$REPO_ROOT/.gomodcache" ]; then
  GOMODCACHE="$REPO_ROOT/.gomodcache"
  export GOMODCACHE
fi
GOFLAGS="${GOFLAGS:--mod=mod}"
GOPROXY="${GOPROXY:-off}"
GOSUMDB="${GOSUMDB:-off}"
CGO_ENABLED="${CGO_ENABLED:-0}"
export GOFLAGS GOPROXY GOSUMDB CGO_ENABLED

status=0

echo "== go build ./... (timeout 300s) =="
timeout 300 go build ./... || status=1

echo "== go test ./... (timeout 300s) =="
timeout 300 go test -count=1 ./... || status=1

if [ -d "$REPO_ROOT/target/batches/BATCH-F2P-0003" ]; then
  echo "== go test ./target/batches/BATCH-F2P-0003/ (timeout 300s) =="
  timeout 300 go test -count=1 ./target/batches/BATCH-F2P-0003/ || status=1
fi

echo "target probe exit: $status"
exit "$status"
