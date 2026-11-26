-- Deterministic legacy bookmarks.db schema for the STATE-003/004 migration
-- probe. It mirrors the pre-migration schema: it has primary_domain but is
-- missing the `domains` and `article_text` columns, and it carries one row
-- whose primary_category is 'tool'. Running `ft index` against a data
-- directory that contains this file plus bookmarks.jsonl must add the missing
-- columns (self-healing) and preserve the stored classification on a
-- non-force refresh.

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE bookmarks (
  id TEXT PRIMARY KEY,
  tweet_id TEXT NOT NULL,
  url TEXT NOT NULL,
  text TEXT NOT NULL,
  author_handle TEXT,
  author_name TEXT,
  author_profile_image_url TEXT,
  posted_at TEXT,
  bookmarked_at TEXT,
  synced_at TEXT NOT NULL,
  conversation_id TEXT,
  in_reply_to_status_id TEXT,
  quoted_status_id TEXT,
  language TEXT,
  like_count INTEGER,
  repost_count INTEGER,
  reply_count INTEGER,
  quote_count INTEGER,
  bookmark_count INTEGER,
  view_count INTEGER,
  media_count INTEGER DEFAULT 0,
  link_count INTEGER DEFAULT 0,
  links_json TEXT,
  tags_json TEXT,
  ingested_via TEXT,
  categories TEXT,
  primary_category TEXT,
  github_urls TEXT,
  primary_domain TEXT,
  quoted_tweet_json TEXT,
  article_title TEXT,
  article_site TEXT,
  enriched_at TEXT,
  folder_ids TEXT,
  folder_names TEXT
);
INSERT INTO bookmarks (id, tweet_id, url, text, synced_at, categories, primary_category)
VALUES ('1', '101', 'https://x.com/alice/status/101', 'machine learning',
        '2024-02-01T00:00:00.000Z', 'tool', 'tool');
REPLACE INTO meta VALUES ('schema_version', '5');
