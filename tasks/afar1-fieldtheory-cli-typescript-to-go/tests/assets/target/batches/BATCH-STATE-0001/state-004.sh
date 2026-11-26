#!/bin/sh
# STATE-004 (required_state, R011) - after `ft index` the bookmarks.db carries
# the required schema: schema_version 6, the 37 documented columns in order,
# the required indexes, and the content-linked FTS5 virtual table declared
# `content=bookmarks, content_rowid=rowid, tokenize='porter unicode61'`.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=/dev/null
. "$HERE/../../../shared/batches/BATCH-STATE-0001/lib.sh"

work="$FT_WORK/state-004"
rm -rf "$work"
mkdir -p "$work/home" "$work/data"
cp "$FT_FIXTURES/bookmarks.jsonl" "$work/data/bookmarks.jsonl"
db="$work/data/bookmarks.db"

run_timed "$FT_CMD_TIMEOUT" env HOME="$work/home" FT_DATA_DIR="$work/data" \
  "$FT_FT_BIN" index >/dev/null 2>&1

expected_cols="id,tweet_id,url,text,author_handle,author_name,author_profile_image_url,posted_at,bookmarked_at,synced_at,conversation_id,in_reply_to_status_id,quoted_status_id,language,like_count,repost_count,reply_count,quote_count,bookmark_count,view_count,media_count,link_count,links_json,tags_json,ingested_via,categories,primary_category,github_urls,domains,primary_domain,quoted_tweet_json,article_title,article_text,article_site,enriched_at,folder_ids,folder_names"
cols=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query \
  "SELECT name FROM pragma_table_info('bookmarks') ORDER BY name" | tr '\n' ',' | sed 's/,$//')
expected_cols=$(printf '%s' "$expected_cols" | tr ',' '\n' | sort | tr '\n' ',' | sed 's/,$//')
ft_assert_eq "$expected_cols" "$cols" "documented columns"

ncols=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query \
  "SELECT COUNT(*) FROM pragma_table_info('bookmarks')")
ft_assert_eq "37" "$ncols" "column count"

ver=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query "SELECT value FROM meta WHERE key='schema_version'")
ft_assert_eq "6" "$ver" "schema_version"

fts=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query \
  "SELECT sql FROM sqlite_master WHERE name='bookmarks_fts'")
run_timed "$FT_CMD_TIMEOUT" python3 "$HERE/../../../shared/integration/fts_contract.py" "$fts"

idx=$(run_timed "$FT_CMD_TIMEOUT" "$FT_SQLQ" "$db" query \
  "SELECT DISTINCT ii.name FROM pragma_index_list('bookmarks') il JOIN pragma_index_info(il.name) ii WHERE ii.name IN ('author_handle','posted_at','language','primary_category','primary_domain') ORDER BY ii.name" \
  | tr '\n' ',' | sed 's/,$//')
ft_assert_eq "author_handle,language,posted_at,primary_category,primary_domain" "$idx" "required indexes"

exit 0
