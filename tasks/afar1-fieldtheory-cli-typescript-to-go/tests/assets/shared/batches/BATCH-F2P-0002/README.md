# BATCH-F2P-0002 probes

Target-only postcondition (F2P) probes for the TypeScript -> Go migration of the
`fieldtheory` CLI. Each observation in this batch is an F2P: the predicate must
be `FAIL` under the TypeScript source and `PASS` under the Go target.

## Layout

- `lib.sh` - shared predicate helpers. Sourced by every probe script.
- `../../fixtures/batches/BATCH-F2P-0002/` - deterministic JSONL caches and
  command documents used by the predicates.
- `../../source/batches/BATCH-F2P-0002/F2P-*.sh` - source-side baseline probes.
- `../../target/batches/BATCH-F2P-0002/F2P-*.sh` - target-side probes.
- `../../target/batches/BATCH-F2P-0002/gotest/` - Go tests injected into a
  writable copy of the module to exercise packages that have no CLI surface.

## Contract

- Every probe sources `lib.sh`, runs its predicate, and prints one JSON line:
  `{"observation_id": ..., "status": "PASS"|"FAIL", "evidence": ...}`.
- The predicate is identical on both sides; only `FT_REPO` changes. No side is
  hardcoded to `PASS` or `FAIL`.
- Repository selection: `FT_REPO` wins when set. Otherwise the source side uses
  `/input/source` (the TypeScript origin) and the target side prefers the
  migrated working tree at `/workspace/repo`, falling back to the read-only
  authoring reference at `/input/target` only when `/workspace/repo` is not a
  repository. The side is taken from `FT_SIDE` or from the probe path.
- Every potentially long-running command (`go build`, `go test`, the module
  copy used by the injected-Go probes) is wrapped by an explicit `timeout`
  (600s build, 900s test, 300s Go probe test, 120s copy). `run_timed` is
  fail-closed: when the `timeout` binary is unavailable it returns an error
  instead of executing the command unbounded, so the harness can never wait
  indefinitely.
- `go test` is the only test entry point; no network, root, or blocking command
  is invoked. The engine probes use bounded `#!/bin/sh` stubs only, and the
  prompt probes use in-process pipes rather than a TTY.

## Residue and L2 rules

- `semantic_rules` scope is the Go production surface (`cmd/`, `internal/`,
  `Makefile`, `go.mod`). Retained TypeScript/Node assets (`src/`, `tests/`,
  `package.json`, `package-lock.json`, `.gitignore`) are intentionally kept by
  the reference target and are never treated as residue.
- `deceptive_fallback_rules` target only the L2 fake-migration signature - a Go
  build that delegates the migrated responsibilities back to the Node/TS
  runtime through a wrapper, subprocess, or dynamic loading. Their targets
  (`node_modules/.bin`, `/usr/bin/node`, `exec.Command("node"`, `ts-node`) do
  not occur anywhere in the reference target's Go surface.
