#!/bin/sh
# BATCH-STATE-0001 target probe driver.
#
# Builds the `ft`/`fieldtheory` entry point from the repository under test
# (${REPO_ROOT:-/workspace/repo}) plus the small SQLite inspection helper, then
# runs one probe per STATE observation. Each probe prints:
#
#   STATE-00N<TAB>pass|fail
#
# The driver exits non-zero if any probe fails. All long-running commands are
# wrapped with the shared timeout helpers so the harness can never block.

set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/../../../shared/batches/BATCH-STATE-0001/lib.sh"

: "${FT_BUILD_TIMEOUT:=300}"
: "${FT_CMD_TIMEOUT:=60}"

# Report an explicit result for every owned observation so the runtime report is
# always complete, even when a toolchain precondition fails.
report_all_fail() {
  for obs in STATE-001 STATE-002 STATE-003 STATE-004; do
    printf '%s\tfail\n' "$obs"
  done
}

repo=$(ft_target_repo)
if [ ! -d "$repo" ]; then
  printf '  error: target repository not found: %s\n' "$repo" >&2
  report_all_fail
  exit 1
fi

work=$(ft_mk_workdir)
trap 'rm -rf "$work"' EXIT INT TERM

ft_bin="$work/ft"
sqlq_bin="$work/sqlq"
mkdir -p "$work/module"

printf 'building %s ...\n' "$repo" >&2
if ! run_timed "$FT_BUILD_TIMEOUT" python3 "$HERE/../../../shared/integration/build_cli.py" "$repo" "$ft_bin" >&2; then
  printf '  error: go build ./cmd/ft failed\n' >&2
  report_all_fail
  exit 1
fi

# Build the SQLite inspection helper from a writable copy of the shipped
# module (the asset tree may be mounted read-only).
cp -R "$HERE/sqlq/." "$work/module/"
if ! run_timed "$FT_BUILD_TIMEOUT" sh -c 'cd "$1" && go build -o "$2" .' sh "$work/module" "$sqlq_bin" >&2; then
  printf '  error: building sqlq helper failed\n' >&2
  report_all_fail
  exit 1
fi

export FT_FT_BIN="$ft_bin"
export FT_SQLQ="$sqlq_bin"
export FT_WORK="$work/cases"
export FT_FIXTURES="$(ft_fixtures_dir)"
export FT_CMD_TIMEOUT
mkdir -p "$FT_WORK"

status=0
for obs in STATE-001 STATE-002 STATE-003 STATE-004; do
  script="$HERE/$(printf '%s' "$obs" | tr 'A-Z' 'a-z').sh"
  if run_timed "$FT_BUILD_TIMEOUT" "$script" >"$work/$obs.out" 2>"$work/$obs.err"; then
    printf '%s\tpass\n' "$obs"
  else
    printf '%s\tfail\n' "$obs"
    status=1
    sed 's/^/    /' "$work/$obs.err" >&2 || true
  fi
done

exit "$status"
