# BATCH-F2P-0001 - failing-to-passing probe assets

Target-only (F2P) probe assets for the Field Theory `ft`/`fieldtheory` CLI
migration from TypeScript/Node.js to Go 1.23.0. Each observation is expected to
be unmet by the TypeScript source (`source_expectation: FAIL`) and satisfied by
the Go target (`target_expectation: PASS`).

## Files

- `shared/batches/BATCH-F2P-0001/probe-lib.sh` - POSIX helpers (repo roots,
  result emission, node-absence and JS-runtime-import predicates).
- `target/batches/BATCH-F2P-0001/run-target-probes.sh` - builds the Go binary,
  exercises the CLI, overlays the Go probe tests into a writable copy of the
  target, and emits one JSON result per observation.
- `target/batches/BATCH-F2P-0001/probes/*.go` - Go probe tests overlaid into
  `internal/engine`, `internal/store` and `internal/fsutil`.
- `source/batches/BATCH-F2P-0001/run-source-probes.sh` - evaluates the same
  postcondition predicate against the TypeScript source tree.
- `fixtures/batches/BATCH-F2P-0001/` - deterministic bookmark caches.

## Harness rules

- Every potentially long-running command (`go build`, `go test`, CLI runs) is
  wrapped by the `shared/skeleton/timeout.sh` helpers with an explicit budget;
  the harness never waits indefinitely.
- The probes are non-interactive, need no network access and require no
  privileges; no case blocks forever.
- Assertions compare observable behavior only: process exit codes, on-disk file
  modes, SQLite schema/column order, FTS5 declarations, BM25 result order,
  structured error reasons and normalized stdout. Human-readable messages and
  log text are never asserted on.
- `source/runner` and `target/runner` are owned by the INTEGRATION batch and are
  not created here.
