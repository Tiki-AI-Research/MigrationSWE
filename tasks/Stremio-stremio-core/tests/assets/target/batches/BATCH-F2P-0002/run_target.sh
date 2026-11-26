#!/usr/bin/env bash
# Target-side F2P probes for BATCH-F2P-0002.
#
# Executes the Go probe suite in probe_test.go against the reference target at
# /input/target (mounted through the go.mod `replace` directive) and maps the
# per-observation test results to a JSON report.
#
# Usage: run_target.sh [REPORT]
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPORT=${1:-/logs/target-f2p-report.json}
OUT=$(mktemp)

cd "$HERE"
GOFLAGS=-mod=mod go test -count=1 -timeout 300s -v ./... | tee "$OUT"

python3 - "$OUT" "$REPORT" <<'PY'
import json
import re
import sys

raw = open(sys.argv[1]).read()
results = {}
for m in re.finditer(r'--- (PASS|FAIL): (TestF2P\d+)\S*', raw):
    status, name = m.group(1), m.group(2)
    num = re.search(r'F2P(\d+)', name).group(1)
    results["F2P-" + num] = status
order = [
    "F2P-025", "F2P-030", "F2P-032", "F2P-034", "F2P-037", "F2P-040",
    "F2P-028", "F2P-031", "F2P-038", "F2P-042", "F2P-047", "F2P-048",
    "F2P-026", "F2P-027", "F2P-029", "F2P-033", "F2P-035", "F2P-036",
    "F2P-039", "F2P-043", "F2P-044", "F2P-045", "F2P-046", "F2P-041",
]
report = [{"observation_id": oid, "status": results.get(oid, "INCONCLUSIVE")} for oid in order]
json.dump(report, open(sys.argv[2], "w"), indent=1)
print("wrote", sys.argv[2])
PY
