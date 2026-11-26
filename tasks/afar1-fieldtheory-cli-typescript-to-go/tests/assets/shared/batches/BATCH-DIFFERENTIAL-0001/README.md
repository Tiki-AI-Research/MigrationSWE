# BATCH-DIFFERENTIAL-0001 shared helpers

Shared scaffolding for the DIFFERENTIAL probes that compare the TypeScript
`source` behavior with the Go `target` behavior for the same shared case corpus.

- `lib.sh` - timeout wrappers (`run_timed`, `run_default`, `run_capture`), exit
  code bucketing, per-observation projection emitters (`ft_emit`, `ft_check`,
  `ft_result`, `ft_finalize`), output normalization helpers, and the
  fixture/source/target root resolvers.
- `source/batches/BATCH-DIFFERENTIAL-0001/source-probe.sh` - records the
  TypeScript source baseline by running the repository's own `npm test` suite
  and the source CLI against the deterministic fixtures.
- `target/batches/BATCH-DIFFERENTIAL-0001/target-probe.sh` - builds the Go
  reference target, runs `go test ./...`, and exercises the migrated command
  surface with the same fixtures.
- `fixtures/batches/BATCH-DIFFERENTIAL-0001/` - deterministic inputs.

Both probes emit one normalized projection line per DIFFERENTIAL observation
(`<OBS-ID>\t<key>=<value>`) plus a per-observation disposition (`<OBS-ID>\tresult=PASS|FAIL`).
The two projections are compared after whitespace/order normalization, so a
correct migration makes the source and target projections identical.

## Harness rules

- `source/runner` and `target/runner` are created by the INTEGRATION batch only.
  This batch never creates, copies, or declares those paths.
- Every potentially long-running command (`go build`, `go test`, `npm test`,
  `tsx --test`, ...) is wrapped with an explicit `timeout`; the harness never
  waits indefinitely.
- Cases that require root privileges, network access, a TTY, or that can block
  forever are excluded.
- Probes assert observable behavior only: process exit codes, structured
  error types/codes, JSON structure/field/ordering, presence, and normalized
  numeric/boolean values. Human-readable message text is only compared after
  whitespace/newline normalization.
