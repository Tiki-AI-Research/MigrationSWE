#!/bin/sh
# Shared helpers for BATCH-F2P-0003 probes.
#
# All shell code here is POSIX `sh`. Every potentially long-running command is
# funnelled through the skeleton timeout helpers so the harness can never block
# indefinitely. No probe may require root privileges or an interactive TTY.
#
# Result protocol (stdout): one line per observation
#   OBS <observation_id> <PASS|FAIL|INFRA_ERROR|INCONCLUSIVE>
# The status is the raw observation for this side; it is never derived from an
# expectation, a role, or a hardcoded side value.
#
# Results are accumulated in a private per-invocation file so this batch never
# truncates or races on a shared results file owned by the integration entry
# point. When the caller exports FT_RESULTS_FILE the emitted lines are also
# appended there (never truncated), which lets the runner fan many batches into
# one stream without losing earlier observations.

set -eu

# ---------------------------------------------------------------------------
# Locate the reserved timeout helpers from BATCH-SKELETON-0001.
# ---------------------------------------------------------------------------
ft_find_timeout_sh() {
  _candidates="
${FT_ASSETS_ROOT:-}/shared/skeleton/timeout.sh
${FT_ASSETS_ROOT:-}/dependencies/batches/BATCH-AUTHOR-BATCH-SKELETON-0001/shared/skeleton/timeout.sh
/assets/shared/skeleton/timeout.sh
/assets/dependencies/batches/BATCH-AUTHOR-BATCH-SKELETON-0001/shared/skeleton/timeout.sh
"
  for _c in ${_candidates}; do
    if [ -n "${_c}" ] && [ -f "${_c}" ]; then
      printf '%s\n' "${_c}"
      return 0
    fi
  done
  return 1
}

if _timeout_sh="$(ft_find_timeout_sh)"; then
  # shellcheck disable=SC1090
  . "${_timeout_sh}"
else
  # Minimal fallback: identical contract to the skeleton helper.
  FT_HARNESS_DEFAULT_TIMEOUT="${FT_HARNESS_DEFAULT_TIMEOUT:-120}"
  FT_HARNESS_KILL_AFTER="${FT_HARNESS_KILL_AFTER:-5}"
  run_timed() {
    _t="$1"; shift
    timeout -k "${FT_HARNESS_KILL_AFTER}" "${_t}" "$@"
  }
  run_default() { run_timed "${FT_HARNESS_DEFAULT_TIMEOUT}" "$@"; }
  run_capture() {
    _t="$1"; _o="$2"; _e="$3"; shift 3
    set +e
    timeout -k "${FT_HARNESS_KILL_AFTER}" "${_t}" "$@" >"${_o}" 2>"${_e}"
    _s=$?
    set -e
    return "${_s}"
  }
fi

FT_BUILD_TIMEOUT="${FT_BUILD_TIMEOUT:-300}"
FT_TEST_TIMEOUT="${FT_TEST_TIMEOUT:-180}"
FT_CMD_TIMEOUT="${FT_CMD_TIMEOUT:-60}"

# ---------------------------------------------------------------------------
# Fixtures / source locations.
# ---------------------------------------------------------------------------
ft_fixtures_dir() { printf '%s' "${FT_FIXTURES_DIR:?FT_FIXTURES_DIR not set}"; }

# Resolve the repository tree a probe should run against. Preference order is:
# explicit REPO_ROOT, then the conventional authoring mount, then the task
# working tree when it carries the expected entry point.
ft_resolve_repo() {
  for _cand in "${REPO_ROOT:-}" "$1" /input/source /input/target /workspace/repo; do
    [ -n "${_cand}" ] || continue
    if [ -f "${_cand}/$2" ]; then
      printf '%s\n' "${_cand}"
      return 0
    fi
  done
  # Fall back to the first non-empty candidate so the probe can still report a
  # precise, non-fatal observation instead of aborting the whole harness.
  for _cand in "${REPO_ROOT:-}" "$1" /input/source /input/target; do
    [ -n "${_cand}" ] && { printf '%s\n' "${_cand}"; return 0; }
  done
  printf '%s\n' "/input/source"
}

# The observation inventory for this batch, in authoring order.
FT_OBS_LIST="F2P-049 F2P-050 F2P-051 F2P-052 F2P-053 F2P-054 F2P-055 F2P-056 F2P-057 F2P-058 F2P-059 F2P-060 F2P-061 F2P-062 F2P-063 F2P-064 F2P-065 F2P-066 F2P-067 F2P-068 F2P-069 F2P-070 F2P-071 F2P-072"

# Private per-invocation result sink. FT_RESULTS_FILE, when exported by the
# caller, is only ever appended to; it is never truncated or recreated here.
FT_RESULTS_PRIVATE="$(mktemp "${TMPDIR:-/tmp}/ft-f2p-0003-results.XXXXXX")"
ft_probe_cleanup() { rm -f "${FT_RESULTS_PRIVATE}" 2>/dev/null || true; }
trap 'ft_probe_cleanup' EXIT INT TERM

ft_emit() {
  _id="$1"; _status="$2"
  printf 'OBS %s %s\n' "${_id}" "${_status}" >>"${FT_RESULTS_PRIVATE}"
  if [ -n "${FT_RESULTS_FILE:-}" ]; then
    printf 'OBS %s %s\n' "${_id}" "${_status}" >>"${FT_RESULTS_FILE}"
  fi
  printf 'OBS %s %s\n' "${_id}" "${_status}"
}

# ft_write_report <side> <batch_id> [report_path]
#   Optional JSON report consumed by the integration entry point. When no report
#   path is supplied (neither argument nor ${FT_REPORT}) this is a no-op.
ft_write_report() {
  _side="$1"; _batch="$2"; _out="${3:-${FT_REPORT:-}}"
  [ -n "${_out}" ] || return 0
  mkdir -p "$(dirname -- "${_out}")" 2>/dev/null || true
  {
    printf '{"batch_id":"%s","side":"%s","observations":[' "${_batch}" "${_side}"
    _first=1
    while read -r _tag _id _status; do
      [ "${_tag}" = "OBS" ] || continue
      [ "${_first}" -eq 1 ] || printf ','
      _first=0
      printf '{"id":"%s","status":"%s"}' "${_id}" "${_status}"
    done <"${FT_RESULTS_PRIVATE}"
    printf ']}\n'
  } >"${_out}"
}

ft_mktempdir() {
  mktemp -d "${TMPDIR:-/tmp}/ft-f2p-0003.XXXXXX"
}
