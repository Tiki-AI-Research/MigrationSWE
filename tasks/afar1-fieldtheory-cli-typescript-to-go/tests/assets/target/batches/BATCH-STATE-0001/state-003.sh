#!/bin/sh
# STATE-003 (critical_data, R011) - `ft index` sets schema_version 6, a refresh
# preserves previously stored classification/enrichment columns while --force
# drops and rebuilds, and schema migrations add missing columns based on actual
# column existence.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/../../../shared/batches/BATCH-STATE-0001/lib.sh"

work="$FT_WORK/state-003"
rm -rf "$work"
mkdir -p "$work/home" "$work/data" "$work/mig"
cp "$FT_FIXTURES/bookmarks.jsonl" "$work/data/bookmarks.jsonl"
cp "$FT_FIXTURES/bookmarks.jsonl" "$work/mig/bookmarks.jsonl"
db="$work/data/bookmarks.db"

run_index() {
  run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" FT_DATA_DIR="$work/data" \
    "$FT_FT_BIN" index "$@" >/dev/null 2>&1
}

# (1) First build: schema_version 6 and both records indexed.
run_index
ver=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query "SELECT value FROM meta WHERE key='schema_version'")
ft_assert_eq "6" "$ver" "schema_version after first index"
cnt=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query "SELECT COUNT(*) FROM bookmarks")
ft_assert_eq "2" "$cnt" "row count after first index"

# (2) A second refresh adds no duplicates.
run_index
cnt=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query "SELECT COUNT(*) FROM bookmarks")
ft_assert_eq "2" "$cnt" "row count after refresh"

# (3) Stored classification / enrichment / folder columns survive a refresh.
run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" exec \
  "UPDATE bookmarks SET primary_category='tool', categories='tool,custom', article_text='stored article', enriched_at='2024-03-01T00:00:00.000Z', folder_ids='[\"stored-folder\"]', folder_names='[\"Stored folder\"]' WHERE id='1'"
run_index
row=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query \
  "SELECT primary_category, categories, article_text, enriched_at, folder_ids, folder_names FROM bookmarks WHERE id='1'")
ft_assert_eq "tool	tool,custom	stored article	2024-03-01T00:00:00.000Z	[\"stored-folder\"]	[\"Stored folder\"]" "$row" "preserved columns on refresh"

# (4) --force drops and rebuilds, resetting stored classification.
run_index --force
row=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query \
  "SELECT primary_category, categories, article_text, enriched_at FROM bookmarks WHERE id='1'")
ft_assert_eq "unclassified	NULL	NULL	NULL" "$row" "reset columns after --force"

# (5) Self-healing migration: a legacy DB missing the domains and article_text
# columns gains them and keeps its stored primary_category on a non-force run.
mdb="$work/mig/bookmarks.db"
legacy_sql=$(cat "$FT_FIXTURES/legacy-bookmarks.schema.sql")
run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$mdb" exec "$legacy_sql"
run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" FT_DATA_DIR="$work/mig" \
  "$FT_FT_BIN" index >/dev/null 2>&1
cols=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$mdb" query \
  "SELECT name FROM pragma_table_info('bookmarks') WHERE name IN ('domains','article_text') ORDER BY name" \
  | tr '\n' ',' | sed 's/,$//')
ft_assert_eq "article_text,domains" "$cols" "migrated columns added"
pc=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$mdb" query \
  "SELECT primary_category FROM bookmarks WHERE id='1'")
ft_assert_eq "tool" "$pc" "classification preserved through migration"
ver=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$mdb" query "SELECT value FROM meta WHERE key='schema_version'")
ft_assert_eq "6" "$ver" "schema_version after migration"

exit 0
