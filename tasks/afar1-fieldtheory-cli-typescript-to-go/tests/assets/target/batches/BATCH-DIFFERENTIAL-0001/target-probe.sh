#!/bin/sh
# Target-side probe for BATCH-DIFFERENTIAL-0001.
#
# Builds the Go reference target and exercises the migrated command surface
# with deterministic fixtures, emitting one normalized observation per
# DIFFERENTIAL observation. It is executed against the real reference target
# (/input/target) before a result is declared PASS.
#
# Every potentially long-running command is wrapped by the timeout helpers in
# shared/batches/BATCH-DIFFERENTIAL-0001/lib.sh; nothing here can block
# indefinitely. Commands requiring privileges, network access, a TTY, or that
# permanently block are excluded.

set -eu

_here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
_assets=$(CDPATH= cd -- "${_here}/../../.." && pwd)
. "${FT_DIFF_LIB:-${_assets}/shared/batches/BATCH-DIFFERENTIAL-0001/lib.sh}"

TARGET_ROOT="$(ft_target_root)"
FIXTURES_ROOT="${FT_FIXTURES:-${_assets}/fixtures/batches/BATCH-DIFFERENTIAL-0001}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ft-diff-target.XXXXXX")"
FT_DIFF_FAILS="${WORK}/fails"
: >"${FT_DIFF_FAILS}"
trap 'rm -rf "${WORK}"' EXIT

FT_BIN="${WORK}/ft"
FT_ALT="${WORK}/fieldtheory"

# Build inside the Go timeout budget. No network is required (module cache is
# pre-populated) and no privilege is needed.
if ! (cd "${TARGET_ROOT}" && GOTOOLCHAIN=local \
        run_timed "${FT_HARNESS_BUILD_TIMEOUT:-300}" python3 "${_assets}/shared/integration/build_cli.py" "${TARGET_ROOT}" "${FT_BIN}"); then
  printf 'FATAL go build failed\n' >&2
  exit 1
fi

# Each observation is derived from its own contract, in a fresh temporary
# data/home/library/commands root. No zero-exit smoke result substitutes for
# returned data or persisted side effects.
cli_probe() {
  ft_probe "$1" run_timed 180 python3 "${_assets}/shared/integration/cli_contract.py" "$2" "${FT_BIN}"
}
module_probe() {
  ft_probe "$1" run_timed 240 python3 "${_assets}/shared/integration/module_contract.py" "${TARGET_ROOT}" "$2"
}

cli_probe DIFF-001 help
cli_probe DIFF-002 surface
cli_probe DIFF-003 json
cli_probe DIFF-004 errors
cli_probe DIFF-005 alias
cli_probe DIFF-006 welcome
cli_probe DIFF-007 paths
cli_probe DIFF-008 search
cli_probe DIFF-009 list
cli_probe DIFF-010 show
cli_probe DIFF-011 stats
cli_probe DIFF-012 categories
cli_probe DIFF-013 domains
cli_probe DIFF-014 folders
cli_probe DIFF-015 classify
module_probe DIFF-016 argv
cli_probe DIFF-017 engine-resolution
cli_probe DIFF-018 model
cli_probe DIFF-019 regex-no-engine
module_probe DIFF-020 prompt
cli_probe DIFF-021 preferences
cli_probe DIFF-022 database
cli_probe DIFF-023 library
cli_probe DIFF-024 commands
module_probe DIFF-025 dates
module_probe DIFF-026 legacy
module_probe DIFF-027 env
cli_probe DIFF-028 surface

ft_finalize
printf 'DONE target-probe\n'
