# BATCH-CONTRACT-0001 shared assets

Shared, language-neutral contract corpus for the TypeScript 6.0.3 -> Go 1.23.0
migration of `ncipollo/release-action`.

`contract_cases.json` enumerates every CONTRACT observation owned by this batch
(`CONTRACT-001` .. `CONTRACT-031`). Each case records the public entry point that
is exercised, the declared requirement it belongs to, and the normalized value
that the pre-migration TypeScript implementation produces. Both side probes read
this corpus so that the source value and the target value are compared after
normalization (whitespace/newline collapse, numeric/boolean coercion and
case-insensitive keys where noted).

The corpus is declarative only. Executable probes live in:

- `source/batches/BATCH-CONTRACT-0001/contract_probe.test.ts` (vitest, runs
  against `/input/source`).
- `target/batches/BATCH-CONTRACT-0001/contract_probe_test.go` (go test, runs
  against `/input/target`).

No harness entry point (`source/runner`, `target/runner`) is created here; those
belong to the INTEGRATION batch. Every probe invocation must be wrapped in an
explicit `timeout` by the caller.
