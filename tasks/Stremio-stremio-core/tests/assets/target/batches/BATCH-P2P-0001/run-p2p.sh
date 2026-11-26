#!/bin/sh
# BATCH-P2P-0001 target-side (Go) P2P probe.
# Runs the migrated module's own test suite and maps every owned observation to
# the real suite result. The common projection is the status wrapper
# {"result":"PASS"}, identical to the source side.
set -eu

REPO="${REPO_ROOT:-${REPO_ROOT:-/input/target}}"
if [ ! -d "$REPO" ]; then
  REPO=/workspace/repo
fi
LOG="${P2P_LOG:-/tmp/batch-p2p-0001-target.log}"

cd "$REPO"
export CGO_ENABLED=0

if go test ./... >"$LOG" 2>&1; then
  result=PASS
else
  result=FAIL
fi

ids="P2P-001 P2P-002 P2P-003 P2P-004 P2P-005 P2P-006 P2P-007 P2P-008 P2P-009 P2P-010 P2P-011 P2P-012 P2P-013 P2P-014 P2P-015 P2P-016 P2P-017"

printf '{"side":"target","relation":"equal_after_normalization","projection":{"result":"%s"},"observations":{' "$result"
first=1
for id in $ids; do
  if [ "$first" -eq 0 ]; then printf ','; fi
  first=0
  printf '"%s":{"result":"%s"}' "$id" "$result"
done
printf '}}\n'

[ "$result" = PASS ]
