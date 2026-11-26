#!/bin/sh
# F2P target-side probes for BATCH-F2P-0002.
#
# Shared semantic predicate (identical to source/probe.sh): the Go 1.24.0
# implementation MUST be the real build/execution path for the labeler packages
# and the named target postconditions MUST hold when that Go code is built and
# run. Because the predicate asserts the target technology itself, it is FALSE
# on the TypeScript source side and TRUE on the Go target side.
#
# Every potentially long-running command is bounded by an explicit `timeout`
# so the harness can never wait indefinitely. Privileged or permanently
# blocking cases are excluded; only local package tests are executed.
set -eu

root="${TARGET_ROOT:-/input/target}"
cd "$root"

if ! command -v go >/dev/null 2>&1; then
  echo "F2P BATCH-F2P-0002: target technology Go 1.24.0 is not on the execution path" >&2
  exit 1
fi

if [ ! -f go.mod ]; then
  echo "F2P BATCH-F2P-0002: no Go module; the Go implementation is not the execution path" >&2
  exit 1
fi

timeout 60 go version
timeout 60 go env GOVERSION

timeout 300 go build ./...

timeout 600 go test \
  ./internal/config \
  ./internal/labeler \
  ./internal/githubapi \
  ./internal/minimatch \
  ./internal/match
