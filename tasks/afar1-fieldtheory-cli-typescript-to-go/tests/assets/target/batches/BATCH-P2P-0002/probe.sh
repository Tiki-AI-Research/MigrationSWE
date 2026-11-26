#!/bin/sh
# Independent behavior receipts for the existing P2P observation inventory.
set -eu
. "$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)/shared/batches/BATCH-P2P-0002/lib.sh"
REPORT="${1:-}"
REPO="$(p2p_target_root)"
P2P_SIDE=target
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT INT TERM
STATUSES="${WORK}/statuses"
: >"${STATUSES}"

build_ok=0
if [ -f "${REPO}/go.mod" ] &&
   (cd "${REPO}" && run_default python3 "$(p2p_asset_root)/shared/integration/build_cli.py" "${REPO}" "${WORK}/ft") >"${WORK}/build.log" 2>&1; then
  build_ok=1
else
  cat "${WORK}/build.log" >&2
fi

p2p_probe() {
  _id="$1"; shift
  if [ "${build_ok}" -ne 1 ]; then
    printf '%s INFRA_ERROR\n' "${_id}" >>"${STATUSES}"
    return
  fi
  set +e
  run_timed 240 python3 "$(p2p_asset_root)/shared/integration/$1" "$2" "$3" >"${WORK}/${_id}.log" 2>&1
  _rc=$?
  set -e
  case "${_rc}" in
    0) _status=PASS ;;
    2) _status=INCONCLUSIVE ;;
    124|137) _status=TIMEOUT ;;
    126|127) _status=INFRA_ERROR ;;
    *) _status=FAIL ;;
  esac
  printf '%s %s\n' "${_id}" "${_status}" >>"${STATUSES}"
  if [ "${_rc}" -ne 0 ]; then
    printf '%s diagnostics:\n' "${_id}" >&2
    cat "${WORK}/${_id}.log" >&2
  fi
}
cli_probe() { p2p_probe "$1" cli_contract.py "$2" "${WORK}/ft"; }
module_probe() { p2p_probe "$1" module_contract.py "${REPO}" "$2"; }

cli_probe P2P-025 database
cli_probe P2P-026 preferences
module_probe P2P-027 dates
cli_probe P2P-028 help
cli_probe P2P-029 paths
cli_probe P2P-030 alias
cli_probe P2P-031 library
cli_probe P2P-032 search
module_probe P2P-033 prompt
cli_probe P2P-034 commands
cli_probe P2P-035 engine-resolution
cli_probe P2P-036 llm
cli_probe P2P-037 classify
cli_probe P2P-038 folders
cli_probe P2P-039 surface
cli_probe P2P-040 json

# Missing execution receipts cannot be inferred from a batch-wide verdict.
p2p_status_for() {
  _status=$(awk -v id="$1" '$1==id {status=$2} END {print status}' "${STATUSES}")
  printf '%s\n' "${_status:-INCONCLUSIVE}"
}
export P2P_SIDE
if [ -n "${REPORT}" ]; then p2p_write_report "${REPORT}"; fi
p2p_emit_jsonl
