# Shared corpus — BATCH-DIFFERENTIAL-0001

This batch appends the differential case corpus for the Rust-to-Go migration of
the `oauth2` library. It extends the container established by
`BATCH-SKELETON-0001` (`shared/skeleton/corpus.json`).

`corpus.json` is the single registry of observable scenarios consumed by both
the source-side (Rust) probe and the target-side (Go) probe, so the reference
behavior and the migrated behavior are compared on identical inputs.

Each scenario has:

- `id` — the DIFFERENTIAL observation id (`DIFF-001` … `DIFF-022`).
- `requirement_ids` — the contract requirements the observation exercises.
- `surface` — the observable surface the projection is taken over.
- `projection` — `normalized-result`: the request method/URL/headers/form-body,
  the response fields, or the `RequestTokenError` kind, after case/parameter
  normalization.
- `source_probe` / `source_tests` — the reference Rust tests that implement the
  scenario (see `source/batches/BATCH-DIFFERENTIAL-0001/probe-manifest.json`).
- `target_probe` — the Go probe function in
  `target/batches/BATCH-DIFFERENTIAL-0001/probe_test.go`.
- `fixtures` — the static fixture data in
  `fixtures/batches/BATCH-DIFFERENTIAL-0001/diff_cases.json`.

The comparison relation for every scenario is `equal_after_normalization`: the
normalized source projection must equal the normalized target projection.

The runner entrypoints `source/runner` and `target/runner` belong exclusively to
the INTEGRATION batch and are intentionally absent here.
