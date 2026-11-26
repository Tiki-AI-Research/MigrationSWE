#!/bin/sh
# BATCH-P2P-0001 target-side probe.
#
# Executes the mapped tests against the Go reference target:
#   1. `go test -v -count=1 -run <mapped names> ./...` for migrated packages,
#      under an explicit timeout;
#   2. the preserved public TypeScript suite (node:test through tsx), each
#      mapped case executed in isolation, when a JavaScript toolchain is
#      available.
#
# Each observation resolves to the matching Go test when one is mapped,
# otherwise to the mapped public test case when the JavaScript toolchain is
# available, otherwise remains unverified. The emitted value is the
# same stable status wrapper used by the source probe so both sides normalize
# to an identical value.
#
# Every invocation is bounded by shared/skeleton/timeout.sh; the probe never
# waits indefinitely and never relies on privileges or network access.
#
# Usage: probe.sh [report_path]
set -eu

_here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "${_here}/../../../shared/batches/BATCH-P2P-0001/p2p_lib.sh"

REPO="${FT_TARGET_REPO:-/input/target}"
WORK="${FT_HARNESS_WORKDIR:-$(mktemp -d)}"
mkdir -p "${WORK}"
REPORT="${1:-${FT_REPORT:-${WORK}/target-report.json}}"
mkdir -p "$(dirname -- "${REPORT}")" 2>/dev/null || REPORT="${WORK}/target-report.json"

echo "target probe: repo=${REPO} report=${REPORT} work=${WORK}" >&2

if [ ! -f "${P2P_TS_MAP}" ]; then
  echo "target probe: missing observation map ${P2P_TS_MAP}" >&2
  exit 1
fi

# 1. Execute only mapped Go tests. Unrelated candidate tests cannot stand in
# for these observations or spend the entire batch budget on other features.
_go_pattern=$(awk -F'\t' '!seen[$2]++ && $2!="" {if (count++) printf "|"; printf "%s",$2}' "${P2P_GO_MAP}")
set +e
( cd "${REPO}" && run_timed 600 go test -v -count=1 -run "^(${_go_pattern})$" ./... ) > "${WORK}/go.log" 2>&1
_go_exit=$?
set -e

# 2. Preserved public TypeScript suite, resolved per mapped case (only when a
#    JavaScript toolchain is actually available).
TS_RESULTS=$(p2p_collect_ts "${REPO}" "${WORK}/ts")

# Per-observation status: prefer a mapped Go test, otherwise the mapped
# TypeScript case, otherwise INCONCLUSIVE (or an observed runner problem).
awk -F'	' '!seen[$1]++{print $1}' "${P2P_TS_MAP}" | while IFS= read -r _id; do
  _val=""
  _go_names=$(awk -F'	' -v id="${_id}" '$1==id{print $2}' "${P2P_GO_MAP}")
  if [ -n "${_go_names}" ]; then
    _val=$(p2p_go_status "${WORK}/go.log" ${_go_names})
    [ "${_val}" = "INCONCLUSIVE" ] && _val=""
  fi
  if [ -z "${_val}" ] && [ -n "${TS_RESULTS}" ]; then
    _val=$(printf '%s\n' "${TS_RESULTS}" | grep "^${_id}:" | head -n 1 | cut -d: -f2)
  fi
  if [ -z "${_val}" ]; then
    _val=INCONCLUSIVE
  fi
  if [ "${_val}" = INCONCLUSIVE ]; then
    case "${_go_exit}" in
      124|137) _val=TIMEOUT ;;
      126|127) _val=INFRA_ERROR ;;
    esac
  fi
  printf '%s:%s\n' "${_id}" "${_val}"
done > "${WORK}/target_results.txt"

RESULTS=$(cat "${WORK}/target_results.txt")
SUITE=$(p2p_aggregate_status ${RESULTS})

ARGS=""
while IFS= read -r _line; do
  [ -n "${_line}" ] || continue
  _id=${_line%%:*}
  _st=${_line#*:}
  ARGS="${ARGS} ${_id}:${_st}"
done < "${WORK}/target_results.txt"

# shellcheck disable=SC2086
set +e
p2p_write_report "${REPORT}" "target" "${SUITE}" ${ARGS}
_rc=$?
set -e
if [ "${_rc}" -ne 0 ]; then
  REPORT="${WORK}/target-report.json"
  # shellcheck disable=SC2086
  p2p_write_report "${REPORT}" "target" "${SUITE}" ${ARGS}
fi
cat "${REPORT}"
