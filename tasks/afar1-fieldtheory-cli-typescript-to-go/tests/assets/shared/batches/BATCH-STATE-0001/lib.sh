#!/bin/sh
# Shared helpers for the BATCH-STATE-0001 probes.
#
# Sourced by every probe. Provides:
#   - the timeout helpers from shared/skeleton/timeout.sh (with a local fallback)
#   - temp-dir bookkeeping
#   - tiny assertion helpers that emit observable results
#
# Every potentially long-running command MUST run through run_timed/run_default
# so the harness can never block indefinitely.

set -eu

_ft_lib_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shared/batches/BATCH-STATE-0001 -> assets root is three levels up.
FT_ASSETS_ROOT=$(CDPATH= cd -- "$_ft_lib_dir/../../.." && pwd)

# Consume the BATCH-SKELETON-0001 dependency: the shared timeout helpers.
_ft_timeout_lib="$FT_ASSETS_ROOT/shared/skeleton/timeout.sh"
if [ -f "$_ft_timeout_lib" ]; then
  # shellcheck source=/dev/null
  . "$_ft_timeout_lib"
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
  ft_repo_root() {
    if [ -n "${FT_REPO:-}" ]; then
      printf '%s\n' "${FT_REPO}"
    else
      printf '%s\n' "/workspace/repo"
    fi
  }
fi

# ft_target_repo - repository under test (the Go target at eval time, the
# reference target while authoring).
ft_target_repo() {
  if [ -n "${FT_REPO:-}" ]; then
    printf '%s\n' "${FT_REPO}"
  elif command -v ft_repo_root >/dev/null 2>&1; then
    ft_repo_root
  else
    printf '%s\n' "/workspace/repo"
  fi
}

# ft_source_repo - TypeScript source tree (read-only).
#
# The authoring container mounts the source repo at /input/source, while the
# runtime source harness may run with the repo at ${FT_REPO:-/workspace/repo}.
# Resolve in that order of specificity so the same probes work in both places.
ft_source_repo() {
  if [ -n "${FT_SOURCE_REPO:-}" ]; then
    printf '%s\n' "${FT_SOURCE_REPO}"
    return
  fi
  if [ -n "${FT_REPO:-}" ]; then
    printf '%s\n' "${FT_REPO}"
    return
  fi
  if [ -n "${REPO_ROOT:-}" ]; then
    printf '%s\n' "${REPO_ROOT}"
    return
  fi
  if [ -d /input/source ]; then
    printf '%s\n' "/input/source"
    return
  fi
  printf '%s\n' "/workspace/repo"
}

# ft_fixtures_dir - deterministic fixture directory.
ft_fixtures_dir() {
  printf '%s\n' "$FT_ASSETS_ROOT/fixtures/batches/BATCH-STATE-0001"
}

ft_die() {
  printf '  error: %s\n' "$1" >&2
  exit 1
}

# ft_assert_eq <expected> <actual> <label>
ft_assert_eq() {
  if [ "$1" != "$2" ]; then
    printf '  assertion failed: %s (expected %s, got %s)\n' "$3" "$1" "$2" >&2
    return 1
  fi
}

# ft_assert_contains <haystack> <needle> <label>
ft_assert_contains() {
  case "$1" in
    *"$2"*) return 0 ;;
    *)
      printf '  assertion failed: %s (missing %s)\n' "$3" "$2" >&2
      return 1
      ;;
  esac
}

# ft_json_value <json> <key> - extract a scalar string value for a top-level
# (or first-matching) key from pretty-printed JSON without a JSON parser.
ft_json_value() {
  printf '%s\n' "$1" | grep -F "\"$2\":" | head -n 1 \
    | sed -e 's/^[^:]*: *//' -e 's/^"//' -e 's/",\{0,1\}$//'
}

# ft_mk_workdir - create and echo a fresh scratch directory.
ft_mk_workdir() {
  _ft_wd=$(mktemp -d "${TMPDIR:-/tmp}/ftstate.XXXXXX")
  printf '%s\n' "$_ft_wd"
}
