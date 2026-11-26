# BATCH-DIFFERENTIAL-0001 fixtures

Deterministic, self-contained inputs shared by the source and target probes.
None of them require network access, root privileges, or interactivity.

- `bookmarks.jsonl` - a four-record cache. Record `1` repeats `machine
  learning` three times and record `2` contains `machine learning tutorial`, so
  FTS5/BM25 ranking is observable; record `3` carries a `CVE-2024-1234` text for
  the `security` regex category; record `4` carries a commerce-style text and a
  deliberately unparseable `postedAt` of `garbage` for the date-helper
  fallback.
- `commands/bad.md` - a portable command missing every required section, used
  to drive `ft commands validate` to a non-zero exit.
- `env/.env` and `env/.env.local` - dotenv fixtures exercising load order and
  no-override semantics.

All fixtures are read-only inputs; probes copy them into a temporary data
directory so a failing probe never mutates the shared corpus.
