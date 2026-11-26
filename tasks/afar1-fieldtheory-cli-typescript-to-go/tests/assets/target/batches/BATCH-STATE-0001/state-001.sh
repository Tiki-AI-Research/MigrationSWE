#!/bin/sh
# STATE-001 (critical_data, R009) - the JSONL bookmark cache is read from and
# the SQLite index is written to the active data directory, with the default
# `~/.fieldtheory/bookmarks` root and the FT_DATA_DIR override, and
# pre-existing files remain usable.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/../../../shared/batches/BATCH-STATE-0001/lib.sh"

work="$FT_WORK/state-001"
rm -rf "$work"
mkdir -p "$work/home" "$work/data"
cp "$FT_FIXTURES/bookmarks.jsonl" "$work/data/bookmarks.jsonl"
cp "$work/data/bookmarks.jsonl" "$work/jsonl.before"

# (1) Default HOME resolves the active data dir to ~/.fieldtheory/bookmarks.
got=$(run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" "$FT_FT_BIN" path)
ft_assert_eq "$work/home/.fieldtheory/bookmarks" "$got" "default data dir"

# (2) `ft index` reads the pre-existing JSONL and writes bookmarks.db beside it,
# leaving the pre-existing JSONL byte-for-byte usable.
run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" FT_DATA_DIR="$work/data" \
  "$FT_FT_BIN" index >/dev/null 2>&1
[ -f "$work/data/bookmarks.db" ] || ft_die "bookmarks.db not written to the data dir"
if ! cmp -s "$work/jsonl.before" "$work/data/bookmarks.jsonl"; then
  ft_die "pre-existing bookmarks.jsonl was modified by ft index"
fi
rows=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$work/data/bookmarks.db" query \
  "SELECT COUNT(*) FROM bookmarks")
ft_assert_eq "2" "$rows" "indexed row count"

# (3) With no override the index lands under $HOME/.fieldtheory/bookmarks.
mkdir -p "$work/home2/.fieldtheory/bookmarks"
cp "$FT_FIXTURES/bookmarks.jsonl" "$work/home2/.fieldtheory/bookmarks/bookmarks.jsonl"
run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home2" "$FT_FT_BIN" index >/dev/null 2>&1
[ -f "$work/home2/.fieldtheory/bookmarks/bookmarks.db" ] \
  || ft_die "bookmarks.db not written under ~/.fieldtheory/bookmarks"

exit 0
