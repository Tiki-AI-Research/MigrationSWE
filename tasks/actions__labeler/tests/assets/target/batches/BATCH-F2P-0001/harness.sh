#!/bin/sh
# F2P target harness for BATCH-F2P-0001 (TypeScript -> Go labeler migration).
#
# Runs the shared F2P probe package against a candidate repository that uses the
# Go target technology. Every potentially long-running command is wrapped in an
# explicit `timeout`; there is no unbounded wait. The probe servers bind only to
# loopback via httptest, so no privileged or network-blocking cases are needed.
#
# Usage: harness.sh <target-repo>
set -eu

REPO="${1:?usage: harness.sh <target-repo>}"
PROBE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/../../../shared/batches/BATCH-F2P-0001/probes" && pwd)"

if [ ! -f "$REPO/go.mod" ]; then
  echo "F2P target probe: no go.mod in $REPO; target technology absent" >&2
  exit 1
fi

rm -rf "$REPO/f2pprobe"
mkdir -p "$REPO/f2pprobe"
cp "$PROBE_DIR/doc.go" "$PROBE_DIR/probe_test.go" "$REPO/f2pprobe/"

cd "$REPO"
timeout 300 go build ./... 2>&1 || true
timeout 300 go test ./f2pprobe/ -count=1 -timeout 240s
