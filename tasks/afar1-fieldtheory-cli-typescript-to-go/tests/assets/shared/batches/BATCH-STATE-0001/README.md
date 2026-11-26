# BATCH-STATE-0001 state-preservation probes

Shared scaffolding for the `BATCH-STATE-0001` authoring batch. This batch owns
the `STATE` observations `STATE-001`..`STATE-004` and their F2P/DIFF probes:

- `STATE-001` / `STATE-002` - on-disk layout under `~/.fieldtheory`
  (`bookmarks/bookmarks.jsonl`, `bookmarks/bookmarks.db`,
  `bookmarks/bookmarks-meta.json`, `bookmarks/.preferences`, `library/`,
  `commands/`) together with the `FT_DATA_DIR` / `FT_LIBRARY_DIR` /
  `FT_COMMANDS_DIR` overrides and the documented legacy fallbacks (R009).
- `STATE-003` / `STATE-004` - `ft index` schema contract: `schema_version = 6`,
  refresh preserves previously stored classification/enrichment/folder columns,
  `--force` drops and rebuilds, migrations are driven by actual column
  existence, and the content-linked FTS5 table is rebuilt only when
  `article_text` is missing (R011).

## Layout

- `source/batches/BATCH-STATE-0001/` - probes executed against the TypeScript
  source tree (see the source runtime, `source/runner`).
- `target/batches/BATCH-STATE-0001/` - probes executed against the Go target
  (`target/runner`); they build the `ft`/`fieldtheory` entry point from
  `${FT_REPO:-/workspace/repo}` and drive it against temporary data
  directories.
- `fixtures/batches/BATCH-STATE-0001/` - deterministic, self-contained inputs
  shared by both sides (a JSONL bookmark cache and a legacy-schema SQL script).
- `fragments/BATCH-STATE-0001.json` - the fragment merged into the final
  `grader-spec`.

## Harness rules

- Every potentially long-running command (`go build`, `go test`, `npm test`,
  ...) goes through the explicit `timeout` helpers from
  `shared/skeleton/timeout.sh` (or the local fallback in `lib.sh`). The harness
  never waits indefinitely.
- Cases that need privileges, network access, or that can block forever are
  excluded.
- Assertions compare observable behavior only: process exit codes, structured
  output fields, file presence, and normalized values.
