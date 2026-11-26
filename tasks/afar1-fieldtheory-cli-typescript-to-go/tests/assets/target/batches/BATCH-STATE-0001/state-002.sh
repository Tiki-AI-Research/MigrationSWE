#!/bin/sh
# STATE-002 (required_state, R009) - the documented on-disk layout is exposed
# at the required locations under the default root and under the
# FT_DATA_DIR / FT_LIBRARY_DIR / FT_COMMANDS_DIR overrides, with the documented
# legacy fallback.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/../../../shared/batches/BATCH-STATE-0001/lib.sh"


assert_active_path() {
  python3 - "$1" "$2" "$3" <<'PYJSON'
import json, sys
value = json.loads(sys.argv[1])["active"][sys.argv[2]]
assert value == sys.argv[3], (sys.argv[2], value, sys.argv[3])
PYJSON
}

work="$FT_WORK/state-002"
rm -rf "$work"
mkdir -p "$work/home"

# (1) Default root.
json=$(run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" "$FT_FT_BIN" paths --json)
assert_active_path "$json" "bookmarksDir" "$work/home/.fieldtheory/bookmarks"
assert_active_path "$json" "libraryDir" "$work/home/.fieldtheory/library"
assert_active_path "$json" "commandsDir" "$work/home/.fieldtheory/library/Commands"
assert_active_path "$json" "bookmarksCachePath" "$work/home/.fieldtheory/bookmarks/bookmarks.jsonl"
assert_active_path "$json" "bookmarksIndexPath" "$work/home/.fieldtheory/bookmarks/bookmarks.db"

# (2) FT_DATA_DIR overrides the data root and implies <FT_DATA_DIR>/md for the
# library while commands stay at the canonical location.
data="$work/data"
got=$(run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" FT_DATA_DIR="$data" "$FT_FT_BIN" path)
ft_assert_eq "$data" "$got" "FT_DATA_DIR ft path"
json=$(run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" FT_DATA_DIR="$data" "$FT_FT_BIN" paths --json)
assert_active_path "$json" "libraryDir" "$data/md"
assert_active_path "$json" "bookmarksCachePath" "$data/bookmarks.jsonl"
assert_active_path "$json" "bookmarksIndexPath" "$data/bookmarks.db"

# (3) FT_LIBRARY_DIR overrides the library root and places commands under it.
# (4) FT_COMMANDS_DIR overrides the commands root.
libdir="$work/lib"
cmddir="$work/cmds"
json=$(run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" FT_LIBRARY_DIR="$libdir" "$FT_FT_BIN" paths --json)
assert_active_path "$json" "libraryDir" "$libdir"
assert_active_path "$json" "commandsDir" "$libdir/Commands"
json=$(run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" FT_COMMANDS_DIR="$cmddir" "$FT_FT_BIN" paths --json)
assert_active_path "$json" "commandsDir" "$cmddir"

# (5) Legacy fallback: when only ~/.ft-bookmarks exists it is the active root,
# while the canonical ~/.fieldtheory/bookmarks is still reported.
home3="$work/home3"
mkdir -p "$home3/.ft-bookmarks"
got=$(run_timed "$FT_CMD_TIMEOUT" env HOME="$home3" "$FT_FT_BIN" path)
ft_assert_eq "$home3/.ft-bookmarks" "$got" "legacy data dir fallback"
json=$(run_timed "$FT_CMD_TIMEOUT" env HOME="$home3" "$FT_FT_BIN" paths --json)
ft_assert_contains "$json" "$home3/.ft-bookmarks" "legacy dir reported"
ft_assert_contains "$json" "$home3/.fieldtheory/bookmarks" "canonical dir reported"

exit 0
