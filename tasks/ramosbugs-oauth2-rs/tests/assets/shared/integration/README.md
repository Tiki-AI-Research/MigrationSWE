# BATCH-INTEGRATION-0001 — merged dependency harness

This batch owns the two runtime entrypoints `source/runner` and `target/runner`
and merges the executable projections authored by the dependency batches:

| batch | contribution | merged by |
| --- | --- | --- |
| `BATCH-SKELETON-0001` | shared corpus container | not executable |
| `BATCH-P2P-0001` | Rust/Go reference test selectors | both runners |
| `BATCH-F2P-0001` | external Go target probe + static predicates | target runner |
| `BATCH-F2P-0002` | static target predicates | target runner (structural checks) |
| `BATCH-F2P-0003` | internal Go target probes | target runner |
| `BATCH-DIFFERENTIAL-0001` | Rust test manifest + internal Go probe | both runners |

## Files

- `observations.json` — self-contained observation inventory (`id`/`kind`/
  `batch_id` for all 116 required runtime observations). The runners read this
  to know which IDs the report must contain; it contains no authoring paths.
- `observation_map.json` — the merged dispatch table. For every observation it
  records the source-side selector set (cargo), the target-side selector set
  (`go test`), or the structural check the target runner evaluates locally.
- `harness.py` — shared execution logic imported by both runners.

## Execution model

`target/runner [report]` copies the repository under test into a scratch Go
module workspace, drops the merged internal/external probes next to the package
sources, runs `go test -json` once and projects every observation onto the stable
status wrapper `{"result": "PASS"}` / `{"result": "FAIL"}`.

`source/runner [report]` runs the Rust reference tests (`cargo test --tests
--examples`) and projects the P2P/DIFFERENTIAL observations onto the same
wrapper. The F2P observations are frozen target-technology postconditions: the
source side verifies that the source technology (Rust) is still the active build
technology, under which the target predicate does not hold, and reports the raw
observation `FAIL`.

Both runners write a `migration-observations:v6` report with exactly the 116
inventory IDs and never emit an empty observation list. When a toolchain or
module is unavailable the affected observations are reported as `INCONCLUSIVE`
rather than fabricated.

The repository under test is discovered only from `MIGRATION_REPO_ROOT`,
`MIGRATION_REPO`, `REPO_ROOT` or `REPO` (default `/workspace/repo`); the asset
root is derived from the runner's own `__file__`.
