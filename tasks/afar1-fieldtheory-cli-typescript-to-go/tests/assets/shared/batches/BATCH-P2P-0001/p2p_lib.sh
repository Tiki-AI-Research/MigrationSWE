#!/bin/sh
# Shared helpers for the BATCH-P2P-0001 pass-to-pass probes.
#
# Every potentially long-running command (tsx/node test, go test, ...) MUST go
# through the shared timeout helper (shared/skeleton/timeout.sh) so the harness
# can never block indefinitely. Cases that require privileges, network access,
# interactivity or that would run forever are never invoked here.
#
# The projection is executable: each P2P observation is mapped to concrete
# test cases from the repository's own suite and the raw PASS/FAIL of those
# cases is read back from a real run.
#
#   * source side: the mapped node:test cases are executed with
#     `tsx --test --test-name-pattern=<title> <file>` so that an unrelated
#     top-level failure in the same file (the repository's own suite has
#     fail-to-pass cases) cannot abort the file and mask the mapped case.
#   * target side: the migrated Go suite (`go test ./...`) is executed, and
#     each observation resolves to a mapped Go test when one exists,
#     otherwise to the mapped node:test case. Missing execution is unverified.
#
# A missing public test file/title is a failure. Its presence alone is never
# evidence of execution and therefore remains INCONCLUSIVE without a runner.
set -eu

# p2p_asset_root - absolute asset root that contains shared/, source/, target/.
p2p_asset_root() {
  if [ -n "${FT_ASSET_ROOT:-}" ]; then
    printf '%s\n' "${FT_ASSET_ROOT}"
    return 0
  fi
  # .../shared/batches/BATCH-P2P-0001 -> three levels up.
  _p2p_here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
  CDPATH= cd -- "${_p2p_here}/../../.." && pwd
}

P2P_ASSET_ROOT=$(p2p_asset_root)

# Fall back to the mounted asset root when the workspace copy of the skeleton
# is not present (the INTEGRATION batch materializes /assets).
if [ ! -f "${P2P_ASSET_ROOT}/shared/skeleton/timeout.sh" ] && [ -f /assets/shared/skeleton/timeout.sh ]; then
  P2P_ASSET_ROOT=/assets
fi

# The shared timeout helper is owned by the SKELETON batch and (re)materialized
# under /assets by the INTEGRATION batch. Consume it from whichever read-only
# location is present; never copy or rewrite it. When it cannot be found at any
# documented location, fall back to an inlined bounded runner so the probe can
# never abort (sourcing a missing file under `set -e`) and the harness can never
# wait indefinitely - every command still goes through `timeout`.
_p2p_timeout_helper=""
for _p2p_candidate in \
  "${P2P_ASSET_ROOT}/shared/skeleton/timeout.sh" \
  /assets/shared/skeleton/timeout.sh \
  /assets/dependencies/batches/BATCH-AUTHOR-BATCH-SKELETON-0001/shared/skeleton/timeout.sh
do
  if [ -f "${_p2p_candidate}" ]; then
    _p2p_timeout_helper="${_p2p_candidate}"
    break
  fi
done

if [ -n "${_p2p_timeout_helper}" ]; then
  # shellcheck source=/dev/null
  . "${_p2p_timeout_helper}"
else
  : "${FT_HARNESS_DEFAULT_TIMEOUT:=120}"
  : "${FT_HARNESS_KILL_AFTER:=5}"
  run_timed() {
    _ft_timeout="$1"
    shift
    timeout -k "${FT_HARNESS_KILL_AFTER}" "${_ft_timeout}" "$@"
  }
  run_default() {
    run_timed "${FT_HARNESS_DEFAULT_TIMEOUT}" "$@"
  }
fi

P2P_TS_MAP="${P2P_ASSET_ROOT}/shared/batches/BATCH-P2P-0001/p2p_ts_map.tsv"
P2P_GO_MAP="${P2P_ASSET_ROOT}/shared/batches/BATCH-P2P-0001/p2p_go_map.tsv"

# p2p_run <timeout_seconds> <command> [args...]
#   Runs a command under the shared timeout helper. Never waits forever. The
#   exit status is echoed on stdout (124/137 mean the timeout fired).
p2p_run() {
  _p2p_timeout="$1"
  shift
  set +e
  run_timed "${_p2p_timeout}" "$@"
  _p2p_status=$?
  set -e
  printf '%s\n' "${_p2p_status}"
}

# p2p_have_js <repo>
#   True when a JavaScript test runner is available for <repo>.
p2p_have_js() {
  _p2p_repo="$1"
  if [ -x "${_p2p_repo}/node_modules/.bin/tsx" ]; then return 0; fi
  if command -v tsx >/dev/null 2>&1; then return 0; fi
  if command -v node >/dev/null 2>&1 && command -v npx >/dev/null 2>&1; then return 0; fi
  return 1
}

# p2p_regex_escape <literal>
#   Escapes a literal so it can be passed to node:test --test-name-pattern.
p2p_regex_escape() {
  printf '%s' "$1" | sed 's/[][(){}.^$*+?|\\]/\\&/g'
}

# p2p_tsx_run <repo> <file> <pattern> <log>
#   Runs a single mapped node:test case (selected with --test-name-pattern)
#   under an explicit timeout, capturing the TAP log. Never propagates a
#   timeout as an error (the caller inspects the log).
p2p_tsx_run() {
  _p2p_repo="$1"; _p2p_file="$2"; _p2p_pat="$3"; _p2p_log="$4"
  if [ -x "${_p2p_repo}/node_modules/.bin/tsx" ]; then
    ( cd "${_p2p_repo}" && p2p_run 180 ./node_modules/.bin/tsx --test --test-reporter=tap --test-name-pattern="${_p2p_pat}" "${_p2p_file}" > "${_p2p_log}" 2>&1 ) >/dev/null || true
  elif command -v tsx >/dev/null 2>&1; then
    ( cd "${_p2p_repo}" && p2p_run 180 tsx --test --test-reporter=tap --test-name-pattern="${_p2p_pat}" "${_p2p_file}" > "${_p2p_log}" 2>&1 ) >/dev/null || true
  else
    ( cd "${_p2p_repo}" && p2p_run 300 npx --no-install tsx --test --test-reporter=tap --test-name-pattern="${_p2p_pat}" "${_p2p_file}" > "${_p2p_log}" 2>&1 ) >/dev/null || true
  fi
}

# p2p_tap_counts <tap_log>
#   Prints "<pass> <fail>" for a node:test TAP summary, or "0 0" when absent.
p2p_tap_counts() {
  _p2p_log="$1"
  _p2p_pass=$(grep -E '^# pass ' "${_p2p_log}" 2>/dev/null | tail -n 1 | awk '{print $3}')
  _p2p_fail=$(grep -E '^# fail ' "${_p2p_log}" 2>/dev/null | tail -n 1 | awk '{print $3}')
  [ -n "${_p2p_pass}" ] || _p2p_pass=0
  [ -n "${_p2p_fail}" ] || _p2p_fail=0
  printf '%s %s\n' "${_p2p_pass}" "${_p2p_fail}"
}

# p2p_ts_executed <log>
#   True when a node:test TAP stream was actually produced (the runner ran).
p2p_ts_executed() {
  grep -q -- "TAP version" "$1" 2>/dev/null
}

# Node can report a passing file wrapper when every named test was filtered
# out. Summary counts therefore cannot replace the exact mapped case receipt.
p2p_tap_case_passed() {
  awk -v title="$2" '
    /^[[:space:]]*(not )?ok[[:space:]]+[0-9]+[[:space:]]+-[[:space:]]/ {
      line=$0
      failed=(line ~ /^[[:space:]]*not ok/)
      sub(/^[[:space:]]*(not )?ok[[:space:]]+[0-9]+[[:space:]]+-[[:space:]]/, "", line)
      if (line==title) {if (failed) bad=1; else found=1}
      else if (index(line,title " #")==1) bad=1
    }
    END {exit !(found && !bad)}
  ' "$1"
}

# p2p_presence_status <repo> <file> <title>
#   Check protected public test presence, without inventing a passing receipt.
p2p_presence_status() {
  _p2p_path="$1/$2"
  if [ ! -f "${_p2p_path}" ]; then
    printf 'FAIL\n'
    return 0
  fi
  if grep -Fq -- "$3" "${_p2p_path}"; then
    printf 'INCONCLUSIVE\n'
  else
    printf 'FAIL\n'
  fi
}

# p2p_ts_case_status <repo> <outdir> <index> <file> <title>
#   Prints the execution status for a single mapped node:test case. It runs
#   in isolation with --test-name-pattern so an unrelated top-level failure in
#   the same file cannot mask it. Missing execution cannot become PASS.
p2p_ts_case_status() {
  _p2p_repo="$1"; _p2p_out="$2"; _p2p_idx="$3"; _p2p_file="$4"; _p2p_title="$5"
  if [ "$(p2p_presence_status "${_p2p_repo}" "${_p2p_file}" "${_p2p_title}")" = FAIL ]; then
    printf 'FAIL\n'
    return 0
  fi
  if p2p_have_js "${_p2p_repo}"; then
    _p2p_log="${_p2p_out}/case-${_p2p_idx}.log"
    _p2p_pat="^$(p2p_regex_escape "${_p2p_title}")$"
    p2p_tsx_run "${_p2p_repo}" "${_p2p_file}" "${_p2p_pat}" "${_p2p_log}"
    _p2p_case_rc=$(tail -n 1 "${_p2p_log}" 2>/dev/null || true)
    case "${_p2p_case_rc}" in
      124|137) printf 'TIMEOUT\n'; return 0 ;;
      126|127) printf 'INFRA_ERROR\n'; return 0 ;;
    esac
    if p2p_ts_executed "${_p2p_log}"; then
      _p2p_counts=$(p2p_tap_counts "${_p2p_log}")
      _p2p_pass=${_p2p_counts%% *}
      _p2p_fail=${_p2p_counts##* }
      if [ "${_p2p_fail}" -gt 0 ]; then
        printf 'FAIL\n'
        return 0
      fi
      if [ "${_p2p_pass}" -gt 0 ] && [ "${_p2p_case_rc}" = 0 ] && p2p_tap_case_passed "${_p2p_log}" "${_p2p_title}"; then
        printf 'PASS\n'
        return 0
      fi
      # A skipped/missing receipt does not show that this assertion failed.
      printf 'INCONCLUSIVE\n'
      return 0
    fi
    # A runner that could not produce TAP is unverified, even if the file and
    # title still exist (for example, npx could not find the offline tsx).
  fi
  p2p_presence_status "${_p2p_repo}" "${_p2p_file}" "${_p2p_title}"
}

# p2p_collect_ts <repo> <outdir>
#   Executes every mapped node:test case and prints one
#   "<observation_id>:<status>" line per unique observation in map order.
#   Each observation aggregates all of its mapped cases (all must PASS).
p2p_collect_ts() {
  _p2p_repo="$1"
  _p2p_out="$2"
  mkdir -p "${_p2p_out}"
  : > "${_p2p_out}/rows.txt"

  _p2p_idx=0
  while IFS='	' read -r _p2p_id _p2p_file _p2p_title; do
    [ -n "${_p2p_id:-}" ] || continue
    [ -n "${_p2p_file:-}" ] || continue
    [ -n "${_p2p_title:-}" ] || continue
    _p2p_idx=$(( _p2p_idx + 1 ))
    _p2p_st=$(p2p_ts_case_status "${_p2p_repo}" "${_p2p_out}" "${_p2p_idx}" "${_p2p_file}" "${_p2p_title}")
    printf '%s\t%s\n' "${_p2p_id}" "${_p2p_st}" >> "${_p2p_out}/rows.txt"
  done < "${P2P_TS_MAP}"

  awk -F'\t' '!seen[$1]++{print $1}' "${P2P_TS_MAP}" | while IFS= read -r _p2p_id; do
    _p2p_values=""
    while IFS='	' read -r _p2p_rid _p2p_rst; do
      [ "${_p2p_rid}" = "${_p2p_id}" ] || continue
      _p2p_values="${_p2p_values} ${_p2p_rst}"
    done < "${_p2p_out}/rows.txt"
    # shellcheck disable=SC2086
    _p2p_agg=$(p2p_aggregate_status ${_p2p_values})
    printf '%s:%s\n' "${_p2p_id}" "${_p2p_agg}"
  done
}

# p2p_go_status <go_test_log> <TestName>...
#   Require every exact mapped name to pass. A prefix, missing name or SKIP
#   cannot supply a receipt for that test.
p2p_go_status() {
  _p2p_log="$1"
  shift
  _p2p_missing=0
  [ "$#" -gt 0 ] || { printf 'INCONCLUSIVE\n'; return 0; }
  for _p2p_name in "$@"; do
    _p2p_named=$(awk -v name="${_p2p_name}" '$1=="---" && $3==name {print $2}' "${_p2p_log}" 2>/dev/null || true)
    if printf '%s\n' "${_p2p_named}" | grep -qx 'FAIL:'; then
      printf 'FAIL\n'
      return 0
    fi
    if ! printf '%s\n' "${_p2p_named}" | grep -qx 'PASS:' || printf '%s\n' "${_p2p_named}" | grep -qx 'SKIP:'; then
      _p2p_missing=1
    fi
  done
  if [ "${_p2p_missing}" -eq 0 ]; then
    printf 'PASS\n'
  else
    printf 'INCONCLUSIVE\n'
  fi
}

# p2p_aggregate_status <id:status>...
#   Preserve failure/timeout/infrastructure/unverified states. Empty execution
#   remains INCONCLUSIVE, and can never masquerade as success.
p2p_aggregate_status() {
  [ "$#" -gt 0 ] || { printf 'INCONCLUSIVE\n'; return 0; }
  _p2p_result=PASS
  for _p2p_pair in "$@"; do
    case "${_p2p_pair#*:}" in
      FAIL) printf 'FAIL\n'; return 0 ;;
      TIMEOUT) _p2p_result=TIMEOUT ;;
      INFRA_ERROR) [ "${_p2p_result}" = TIMEOUT ] || _p2p_result=INFRA_ERROR ;;
      PASS) : ;;
      *) [ "${_p2p_result}" != PASS ] || _p2p_result=INCONCLUSIVE ;;
    esac
  done
  printf '%s\n' "${_p2p_result}"
}

# p2p_write_report <report_path> <side> <suite_status> <id:status>...
#   Emits the P2P report to <report_path>. Each observation value is the common
#   status wrapper {"result": <status>}; the wrapper is only emitted as
#   PASS when the underlying execution really passed.
p2p_write_report() {
  _p2p_report="$1"
  _p2p_side="$2"
  _p2p_suite="$3"
  shift 3
  {
    printf '{\n'
    printf '  "batch_id": "BATCH-P2P-0001",\n'
    printf '  "side": "%s",\n' "${_p2p_side}"
    printf '  "suite_status": "%s",\n' "${_p2p_suite}"
    printf '  "observations": {\n'
    _p2p_first=1
    for _p2p_pair in "$@"; do
      _p2p_id=${_p2p_pair%%:*}
      _p2p_st=${_p2p_pair#*:}
      case "${_p2p_st}" in
        PASS|FAIL|NA|TIMEOUT|INFRA_ERROR|INCONCLUSIVE) _p2p_val=${_p2p_st} ;;
        *) _p2p_val=INCONCLUSIVE ;;
      esac
      if [ "${_p2p_first}" -eq 0 ]; then
        printf ',\n'
      fi
      _p2p_first=0
      printf '    "%s": {"result": "%s"}' "${_p2p_id}" "${_p2p_val}"
    done
    printf '\n  }\n'
    printf '}\n'
  } > "${_p2p_report}"
}
