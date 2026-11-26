# BATCH-F2P-0001 fixtures

Deterministic inputs for the Field Theory TypeScript -> Go F2P probes. They need
no network access, no root privileges and no interactivity, and never block.

- `bookmarks.jsonl` - a four-record JSONL cache used for the CLI probes
  (`ft index`, `ft search`, `ft stats`, `ft categories`, `ft domains`,
  `ft folders`, `ft classify --regex`). Record `1` repeats `machine learning`
  three times, so the FTS5 BM25 ranking places it before record `2`.
- `bookmarks_bm25.jsonl` - a two-record cache for the exact BM25 ordering probe
  (`id=1` before `id=2`).

All records use the documented `BookmarkRecord` JSON shape (`id`, `tweetId`,
`url`, `text`, `authorHandle`, `authorName`, `postedAt`, `syncedAt`,
`language`, and optionally `folderIds`/`folderNames`).
