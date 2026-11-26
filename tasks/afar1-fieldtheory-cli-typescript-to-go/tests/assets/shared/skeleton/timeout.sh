#!/bin/sh
# Shared timeout helpers for migration test-asset probes.
#
# Every potentially long-running command MUST go through one of these helpers so
# that the harness can never block indefinitely. Commands that need privileges
# or that are expected to block (interactive TTY prompts, watchers, servers)
# MUST NOT be invoked here.

set -eu

# Wall-clock budget for a single probe command, in seconds.
: "${FT_HARNESS_DEFAULT_TIMEOUT:=120}"
# Grace period between SIGTERM and SIGKILL, in seconds.
: "${FT_HARNESS_KILL_AFTER:=5}"

# run_timed <timeout_seconds> <command> [args...]
#   Runs <command> under `timeout`, escalating to SIGKILL after the grace period.
run_timed() {
  _ft_timeout="$1"
  shift
  timeout -k "${FT_HARNESS_KILL_AFTER}" "${_ft_timeout}" "$@"
}

# run_default <command> [args...]
#   Runs <command> under the default wall-clock budget.
run_default() {
  run_timed "${FT_HARNESS_DEFAULT_TIMEOUT}" "$@"
}

# run_capture <timeout_seconds> <stdout_file> <stderr_file> <command> [args...]
#   Runs <command> under `timeout`, capturing stdout/stderr to files. Returns the
#   command exit status; exit status 124/137 indicates the timeout fired.
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

# ft_repo_root - absolute repository working tree.
ft_repo_root() {
  if [ -n "${FT_REPO:-}" ]; then
    printf '%s\n' "${FT_REPO}"
  else
    printf '%s\n' "/workspace/repo"
  fi
}
