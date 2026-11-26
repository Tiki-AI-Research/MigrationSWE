# BATCH-F2P-0003 shared probes

Fail-to-pass probes for the TypeScript 6.0.3 -> Go 1.23.0 migration of
`ncipollo/release-action`.

## Layout

- `probe_common.sh` — shared dispatcher and helpers (POSIX `sh`).
- `../../target/batches/BATCH-F2P-0003/f2p_probe_test.go` — the Go behaviour
  probe: one subtest per observation, selected with
  `go test -run 'TestProbeF2P/<OBS>'`.
- `../../target/batches/BATCH-F2P-0003/run_probe.sh` — target-side entry point
  (`/input/target`, expected PASS).
- `../../source/batches/BATCH-F2P-0003/run_probe.sh` — source-side entry point
  (`/input/source`, expected FAIL).
- `../../fixtures/batches/BATCH-F2P-0003/observations.json` — declarative
  observation manifest.

## Semantics

Each observation is a target-only postcondition. The same predicate is evaluated
in both trees:

- in `/input/target` the Go implementation satisfies it (PASS);
- in `/input/source` the postcondition is expressed in terms of the Go
  implementation, which does not exist in the pre-migration TypeScript tree, so
  the predicate is genuinely unsatisfied (FAIL). This is a semantic failure of
  the target capability, not an install or harness error.

`F2P-060`, `F2P-071` and `F2P-072` are dependency/runtime postconditions and are
evaluated with explicit shell predicates (`go test ./...`, a Node-free `go
build` + execution, and a `go.mod` closure check).

## Portability

The shell assets are POSIX `sh` compatible and run under `/bin/sh` (dash), ash,
busybox sh and bash. They use no bash arrays, no `BASH_SOURCE` and no bash
parameter substitutions, so an `sh -n` syntax check passes in every one of those
shells.

## Bounded execution

Every `go` invocation is wrapped in `timeout "$PROBE_TIMEOUT"` (default 300s)
and the probes avoid network, privilege and interactive input. A missing
toolchain or module cache is reported as `INFRA_ERROR` and never disguised as a
semantic PASS/FAIL.
