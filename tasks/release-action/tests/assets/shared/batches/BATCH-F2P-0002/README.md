# BATCH-F2P-0002

Fail-to-pass (F2P) probe assets for the TypeScript 6.0.3 -> Go 1.23.0 migration
of `ncipollo/release-action`.

## What this batch owns

- `shared/batches/BATCH-F2P-0002/` - shared F2P predicate and runner.
- `source/batches/BATCH-F2P-0002/` - source-side probe entry point.
- `target/batches/BATCH-F2P-0002/` - target-side probe entry point.
- `fixtures/batches/BATCH-F2P-0002/` - observation -> probe/requirement map.
- `fragments/BATCH-F2P-0002.json` - the asset fragment for this batch.

## Predicate

`f2p_probe_test.go.txt` contains one Go test per owned observation
(`TestF2P025` ... `TestF2P048`). It is stored with a `.txt` suffix so it is
never compiled by a `go test ./...` that happens to run over the asset tree;
the runner installs it as `f2p_probe_test.go` in a scratch copy of the
repository under test. Each test exercises the concrete entry named by its
observation and asserts the observation's required postcondition in the
required target technology (Go). The same predicate is run in both
environments:

- On the migrated target every test passes.
- On the TypeScript source there is no Go module, so the target condition is
  not satisfied and every observation is a semantic FAIL.

The two environments therefore use the same semantic predicate; the result is
derived from the repository under test, never hardcoded per side.

## Runner

`run_probe.sh <repo> <report> [probe-test-file]` copies the repository to a
scratch directory, installs the predicate, and executes

```
timeout "${F2P_TIMEOUT_SECS:-300}" go test -run '^TestF2P' -count=1 -v .
```

Every potentially long running command is wrapped in `timeout`; the runner
never waits indefinitely. A timeout is reported as `inconclusive`, a missing
Go module as a semantic `fail`.

`source/batches/BATCH-F2P-0002/probe.sh` and
`target/batches/BATCH-F2P-0002/probe.sh` are thin wrappers around the shared
runner. They must not be confused with the INTEGRATION-owned `source/runner`
and `target/runner` entry points.
