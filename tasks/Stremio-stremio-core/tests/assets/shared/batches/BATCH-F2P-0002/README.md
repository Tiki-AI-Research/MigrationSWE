# BATCH-F2P-0002

Fail-to-pass probes for the Rust -> Go migration of `stremio-core`.

- `target/batches/BATCH-F2P-0002/` — Go probe suite executed against the reference
  target `/input/target` through a `replace` directive. All 24 observations PASS
  (`run_target.sh`).
- `source/batches/BATCH-F2P-0002/` — the same semantic predicates evaluated on the
  Rust source `/input/source`. All 24 observations FAIL, because the frozen
  target technology (Go 1.23.0) is not present on the source execution path.
- `fixtures/batches/BATCH-F2P-0002/expected_wires.json` — frozen wire strings used
  by the probes.

Observations covered: F2P-025..F2P-048 (24 items).
