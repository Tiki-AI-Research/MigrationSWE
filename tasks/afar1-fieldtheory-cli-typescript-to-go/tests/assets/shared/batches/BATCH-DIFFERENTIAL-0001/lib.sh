#!/bin/sh
# Shared helpers for the BATCH-DIFFERENTIAL-0001 source/target probes.
#
# This file is owned by BATCH-DIFFERENTIAL-0001. It never creates, copies, or
# declares source/runner or target/runner (those belong to the INTEGRATION
# batch). Every potentially long-running command is executed through the
# timeout helpers below; the harness never waits indefinitely.

set -eu

# Reuse the skeleton timeout helpers when the assembled asset tree provides
# them; otherwise fall back to an equivalent local implementation.
_ft_skeleton_timeout=""
for _cand in \
  "${FT_SKELETON_TIMEOUT:-}" \
  "$(dirname "$0")/../../skeleton/timeout.sh" \
  "$(dirname "$0")/../../../skeleton/timeout.sh" \
  "/assets/shared/skeleton/timeout.sh"; do
  if [ -n "${_cand}" ] && [ -f "${_cand}" ]; then
    _ft_skeleton_timeout="${_cand}"
    break
  fi
done

if [ -n "${_ft_skeleton_timeout}" ]; then
  # shellcheck disable=SC1090
  . "${_ft_skeleton_timeout}"
fi

if ! command -v run_timed >/dev/null 2>&1; then
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

  run_capture() {
    _ft_timeout="$1"
    _ft_stdout="$2"
    _ft_stderr="$3"
    shift 3
    set +e
    timeout -k "${FT_HARNESS_KILL_AFTER}" "${_ft_timeout}" "$@" \
      >"${_ft_stdout}" 2>"${_ft_stderr}"
    _ft_status=$?
    set -e
    return "${_ft_status}"
  }
fi

# ft_source_root - the read-only TypeScript source tree.
ft_source_root() {
  printf '%s\n' "${FT_SOURCE:-/input/source}"
}

# ft_target_root - the read-only Go reference target tree.
ft_target_root() {
  printf '%s\n' "${FT_TARGET:-/input/target}"
}

# ft_normalize <file>
#   Collapse runs of whitespace into single spaces, drop blank lines and sort
#   the remaining lines. Used so DIFFERENTIAL comparisons ignore incidental
#   whitespace/ordering that is not part of the public contract.
ft_normalize() {
  tr -s '[:space:]' ' ' <"$1" | tr ' ' '\n' | sed '/^$/d' | LC_ALL=C sort
}

# ft_normalize_text <file>
#   Whitespace-normalized single line (order preserved). Suitable for comparing
#   human-readable output without pinning exact line breaks.
ft_normalize_text() {
  tr '\n' ' ' <"$1" | tr -s '[:space:]' ' '
}

# ft_json_has_key <file> <key>
ft_json_has_key() {
  grep -q "\"$2\"" "$1"
}

# ft_rc_bucket <exit_code>
#   Normalize a process exit status to a coarse, implementation-independent
#   bucket so the DIFFERENTIAL comparison never pins an exact error code that
#   the contract does not specify. Prints "ok" for 0 and "error" otherwise.
ft_rc_bucket() {
  if [ "$1" -eq 0 ]; then
    printf 'ok\n'
  else
    printf 'error\n'
  fi
}

# ft_bool <condition-exit-code>
#   Prints 1 when the wrapped condition succeeded (exit 0) and 0 otherwise.
ft_bool() {
  if [ "$1" -eq 0 ]; then
    printf '1\n'
  else
    printf '0\n'
  fi
}

# ft_emit <observation_id> <key> <value>
#   Emit one normalized projection line for an observation.
ft_emit() {
  printf '%s\t%s=%s\n' "$1" "$2" "$3"
}

# ft_check <observation_id> <key> <actual> <expected>
#   Emit the normalized projection and record a failure for the observation when
#   the actual value diverges from the documented expectation.
ft_check() {
  _id="$1"
  _key="$2"
  _actual="$3"
  _expected="$4"
  ft_emit "${_id}" "${_key}" "${_actual}"
  if [ "${_actual}" != "${_expected}" ]; then
    ft_mark_fail "${_id}"
  fi
}

# ft_fails_file - the per-run failure list backing ft_mark_fail/ft_result.
ft_fails_file() {
  printf '%s\n' "${FT_DIFF_FAILS:?FT_DIFF_FAILS must be set}"
}

# ft_mark_fail <observation_id>
ft_mark_fail() {
  printf '%s\n' "$1" >>"$(ft_fails_file)"
}

# ft_finalize
#   Emit the per-observation disposition for every owned observation. An
#   observation is PASS when no check diverged from its documented expectation.
ft_finalize() {
  for _id in $(ft_observation_ids); do
    _status=""
    if [ -f "${FT_DIFF_FAILS}.statuses" ]; then
      _status=$(awk -v id="$_id" '$1==id {status=$2} END {print status}' "${FT_DIFF_FAILS}.statuses")
    fi
    if grep -qx "${_id}" "$(ft_fails_file)" 2>/dev/null; then
      ft_result "${_id}" FAIL expected-mismatch
    elif [ -n "$_status" ]; then
      ft_result "${_id}" "$_status" behavior-contract
    else
      ft_result "${_id}" PASS expected-match
    fi
  done
}

# ft_probe <id> <command...> - retain an independently executed postcondition.
# Unsupported adapters and infrastructure failures must never turn into PASS.
ft_probe() {
  _probe_id="$1"; shift
  set +e
  "$@" >"${FT_DIFF_FAILS}.${_probe_id}.log" 2>&1
  _probe_rc=$?
  set -e
  case "$_probe_rc" in
    0) _probe_status=PASS ;;
    2) _probe_status=INCONCLUSIVE ;;
    124|137) _probe_status=TIMEOUT ;;
    126|127) _probe_status=INFRA_ERROR ;;
    *) _probe_status=FAIL ;;
  esac
  printf '%s\t%s\n' "$_probe_id" "$_probe_status" >>"${FT_DIFF_FAILS}.statuses"
  if [ "$_probe_rc" -ne 0 ]; then
    printf '%s diagnostics:\n' "$_probe_id" >&2
    cat "${FT_DIFF_FAILS}.${_probe_id}.log" >&2
  fi
}

# ft_result <observation_id> <PASS|FAIL> <detail>
#   Emit the per-observation disposition that the harness aggregates. The
#   detail is a short, machine-friendly token (never human prose that would
#   become a pinned contract).
ft_result() {
  printf '%s\tresult=%s\n' "$1" "$2"
  printf 'RESULT\t%s\t%s\t%s\n' "$1" "$2" "$3"
}

# ft_observation_ids - the DIFFERENTIAL observations owned by this batch.
ft_observation_ids() {
  cat <<'IDS'
DIFF-001
DIFF-002
DIFF-003
DIFF-004
DIFF-005
DIFF-006
DIFF-007
DIFF-008
DIFF-009
DIFF-010
DIFF-011
DIFF-012
DIFF-013
DIFF-014
DIFF-015
DIFF-016
DIFF-017
DIFF-018
DIFF-019
DIFF-020
DIFF-021
DIFF-022
DIFF-023
DIFF-024
DIFF-025
DIFF-026
DIFF-027
DIFF-028
IDS
}
