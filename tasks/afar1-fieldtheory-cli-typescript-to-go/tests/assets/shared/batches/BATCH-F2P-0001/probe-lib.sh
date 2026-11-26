#!/bin/sh
# Shared helpers for the BATCH-F2P-0001 target/source probes.
#
# All potentially long-running commands MUST go through the timeout helpers in
# shared/skeleton/timeout.sh before this file is sourced. Nothing here blocks.

FT_BATCH_ID="BATCH-F2P-0001"

# ft_timeout_helpers <assets_root> - locate the shared/skeleton/timeout.sh
# helpers, preferring the merged asset root and falling back to the read-only
# dependency snapshot.
ft_timeout_helpers() {
  for _c in \
    "$1/shared/skeleton/timeout.sh" \
    "/assets/shared/skeleton/timeout.sh" \
    "/assets/dependencies/batches/BATCH-AUTHOR-BATCH-SKELETON-0001/shared/skeleton/timeout.sh"; do
    if [ -f "$_c" ]; then
      printf '%s\n' "$_c"
      return 0
    fi
  done
  return 1
}

# ft_target_repo - absolute path to the Go target repository.
# ft_load_timeout <assets_root> - source shared/skeleton/timeout.sh when it is
# reachable; otherwise install an equivalent, self-contained fallback so the
# harness still wraps every long-running command with an explicit `timeout` and
# can never block indefinitely.
ft_load_timeout() {
  _assets="$1"
  if _helpers=$(ft_timeout_helpers "$_assets"); then
    # shellcheck source=/dev/null
    . "$_helpers"
  fi
  if command -v run_timed >/dev/null 2>&1; then
    return 0
  fi
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
  export FT_HARNESS_DEFAULT_TIMEOUT FT_HARNESS_KILL_AFTER
  return 0
}

ft_target_repo() {
  printf '%s\n' "${FT_TARGET_REPO:-/input/target}"
}

# ft_source_repo - absolute path to the TypeScript source repository.
ft_source_repo() {
  printf '%s\n' "${FT_SOURCE_REPO:-/input/source}"
}

# ft_emit <observation_id> <relation> [detail]
#   Appends one probe result object to $FT_RESULTS_FILE.
ft_emit() {
  _id="$1"
  _rel="$2"
  _detail="${3:-}"
  if [ -n "$FT_RESULTS_FILE" ]; then
    printf '{"observation_id":"%s","relation":"%s"}\n' "$_id" "$_rel" >>"$FT_RESULTS_FILE"
  else
    printf '{"observation_id":"%s","relation":"%s"}\n' "$_id" "$_rel"
  fi
}

# ft_note - diagnostics to stderr only (never asserted on).
ft_note() {
  printf '  [%s] %s\n' "$FT_BATCH_ID" "$*" >&2
}

# ft_node_absent - true when no node/tsx runtime is reachable on PATH.
ft_node_absent() {
  if command -v node >/dev/null 2>&1; then return 1; fi
  if command -v tsx >/dev/null 2>&1; then return 1; fi
  return 0
}

# ft_has_js_runtime_imports <repo> - true when Go production code imports a
# JavaScript/TypeScript runtime.
ft_has_js_runtime_imports() {
  _repo="$1"
  if [ ! -d "$_repo/cmd" ] || [ ! -d "$_repo/internal" ]; then
    return 0
  fi
  if grep -rInE '"node:|require\(|\.mjs"|node:child_process|from "node' \
      "$_repo/cmd" "$_repo/internal" --include='*.go' >/dev/null 2>&1; then
    return 0
  fi
  return 1
}
