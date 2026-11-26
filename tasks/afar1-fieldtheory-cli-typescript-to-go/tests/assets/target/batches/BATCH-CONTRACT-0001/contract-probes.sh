#!/bin/sh
# Target-side public-contract probes for BATCH-CONTRACT-0001.
#
# Executed against the real Go reference target (default /input/target,
# override with FT_TARGET_REPO). Builds ./cmd/ft once and runs the resulting
# `ft` / `fieldtheory` binary. Every potentially long-running command
# (`go build`) and every CLI invocation is wrapped with an explicit `timeout`;
# no network, credential, or interactive behavior is exercised.
#
# Usage: contract-probes.sh [CONTRACT-00N | all]
# Each selected observation prints one normalized JSON record:
#   {"observation_id":"CONTRACT-00N","status":"PASS|FAIL","value":"..."}

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
. "$ROOT/shared/batches/BATCH-CONTRACT-0001/lib.sh"

TGT="${FT_TARGET_REPO:-/input/target}"
FIX="$ROOT/fixtures/batches/BATCH-CONTRACT-0001"
FT_WANT="${1:-all}"

FT_DATA="$(ft_make_tmpdir)"
trap 'rm -rf "$FT_DATA"' EXIT INT TERM

FT_LIBRARY_DIR="$FT_DATA/md"
FT_COMMANDS_DIR="$FT_DATA/commands"
export FT_LIBRARY_DIR FT_COMMANDS_DIR

cp "$FIX/bookmarks.jsonl" "$FT_DATA/bookmarks.jsonl"

# Deterministic engine stubs so engine-discovery probes never touch the network.
mkdir -p "$FT_DATA/bin"
cp "$FIX/engine-claude-stub" "$FT_DATA/bin/claude"
cp "$FIX/engine-codex-stub" "$FT_DATA/bin/codex"
chmod 755 "$FT_DATA/bin/claude" "$FT_DATA/bin/codex"
PATH="$FT_DATA/bin:$PATH"
export PATH

FT_BIN="$FT_DATA/ft"

# ft_all_fail <reason> - emit a FAIL record for every selected observation so
# the harness always receives a complete inventory.
ft_all_fail() {
  _reason="$1"
  for _obs in CONTRACT-001 CONTRACT-002 CONTRACT-003 CONTRACT-004 CONTRACT-005 \
              CONTRACT-006 CONTRACT-007 CONTRACT-008 CONTRACT-009 CONTRACT-010 \
              CONTRACT-011 CONTRACT-012 CONTRACT-013 CONTRACT-014 CONTRACT-015 \
              CONTRACT-016 CONTRACT-017 CONTRACT-018 CONTRACT-019 CONTRACT-020 \
              CONTRACT-021 CONTRACT-022 CONTRACT-023 CONTRACT-024 CONTRACT-025 \
              CONTRACT-026 CONTRACT-027 CONTRACT-028; do
    if [ "$FT_WANT" != "all" ] && [ "$FT_WANT" != "$_obs" ]; then
      continue
    fi
    ft_record "$_obs" FAIL "$_reason"
  done
}

# Consume a shared module cache when one is mounted, then build offline.
for _cache in "$ROOT/.gomodcache" "$TGT/.gomodcache" "${GOMODCACHE:-}"; do
  if [ -n "${_cache:-}" ] && [ -d "$_cache" ]; then
    GOMODCACHE="$_cache"
    export GOMODCACHE
    break
  fi
done
export GOFLAGS="${GOFLAGS:--mod=mod}"

if ! run_capture "${FT_HARNESS_DEFAULT_TIMEOUT}" "$FT_DATA/.build.out" "$FT_DATA/.build.err" \
     sh -c "cd \"$TGT\" && go build -o \"$FT_BIN\" ./cmd/ft"; then
  echo "target build failed:" >&2
  cat "$FT_DATA/.build.err" >&2 2>/dev/null || true
  ft_all_fail "build_failed"
  exit 0
fi

if [ ! -x "$FT_BIN" ]; then
  ft_all_fail "binary_missing"
  exit 0
fi

# ft_cli <out> <err> <args...> - run the built binary under a timeout.
ft_cli() {
  _out="$1"
  _err="$2"
  shift 2
  FT_DATA_DIR="$FT_DATA" \
    timeout -k "${FT_HARNESS_KILL_AFTER}" "${FT_HARNESS_DEFAULT_TIMEOUT}" \
    "$FT_BIN" "$@" >"$_out" 2>"$_err"
  return $?
}

# ft_cli_named <name> <out> <err> <args...> - run under the alternate name.
ft_cli_named() {
  _name="$1"
  _out="$2"
  _err="$3"
  shift 3
  ln -sf "$FT_BIN" "$FT_DATA/$_name" 2>/dev/null || true
  FT_DATA_DIR="$FT_DATA" \
    timeout -k "${FT_HARNESS_KILL_AFTER}" "${FT_HARNESS_DEFAULT_TIMEOUT}" \
    "$FT_DATA/$_name" "$@" >"$_out" 2>"$_err"
  return $?
}

# Prime the SQLite index so query commands have data to operate on.
ft_run index >/dev/null 2>&1 || true
rm -f "$FT_DATA/.run.out" "$FT_DATA/.run.err"

ft_contract_all "$FT_WANT"
exit 0
