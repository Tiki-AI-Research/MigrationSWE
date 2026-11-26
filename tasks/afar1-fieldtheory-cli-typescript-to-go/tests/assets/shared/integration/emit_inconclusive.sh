#!/bin/sh
# Fallback runtime-report writer for the INTEGRATION harness.
#
# Used only when python3 is unavailable so that the harness still emits a
# well-formed `migration-observations:v6` document listing every observation in
# the self-contained inventory. Every status is the honest INCONCLUSIVE: no
# probe was executed, so no PASS/FAIL is fabricated.
set -eu

SELF=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ASSETS=$(CDPATH= cd -- "${SELF}/../.." && pwd)
REPORT="${1:-/logs/result.json}"
INVENTORY="${ASSETS}/shared/integration/observations.json"

[ -f "${INVENTORY}" ] || { echo "missing inventory: ${INVENTORY}" >&2; exit 1; }
mkdir -p "$(dirname -- "${REPORT}")" 2>/dev/null || true

{
  printf '%s\n' '{'
  printf '%s\n' '  "schema_version": "migration-observations:v6",'
  awk '
    BEGIN { print "  \"observations\": [" ; first = 1 }
    /"id": *"[^"]*", *"kind": *"[^"]*"/ {
      s = $0
      sub(/.*"id": *"/, "", s); id = s; sub(/".*/, "", id)
      t = $0
      sub(/.*"kind": *"/, "", t); kind = t; sub(/".*/, "", kind)
      if (!first) print ","
      first = 0
      printf "    {\"id\": \"%s\", \"kind\": \"%s\", \"status\": \"INCONCLUSIVE\", \"value\": {\"type\": \"json\", \"value\": {\"result\": \"INCONCLUSIVE\"}}}", id, kind
    }
    END { print ""; print "  ]"; print "}" }
  ' "${INVENTORY}"
} > "${REPORT}.tmp"

mv "${REPORT}.tmp" "${REPORT}"
