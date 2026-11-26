# BATCH-F2P-0001 shared assets

Fail-to-pass (F2P) probes for the TypeScript 6.0.3 -> Go 1.23.0 migration of
`ncipollo/release-action`.

- `f2p_checks.py` is the single implementation of every observation predicate.
  Both `source/batches/BATCH-F2P-0001/probe.sh` and
  `target/batches/BATCH-F2P-0001/probe.sh` invoke it and differ only in the
  repository root they select.  The engine never decides the outcome from the
  side it runs on; it reports the raw observation status.

## Predicates

Each observation runs the *same* atomic predicate against the supplied tree.
The predicate fails on the pre-migration TypeScript tree because the required
Go execution path does not exist, and passes on the reference Go target:

| check kind | observable predicate |
| --- | --- |
| `action_entrypoint_docker` | `action.yml` `runs.using: docker`, `runs.image: Dockerfile`, and the Dockerfile builds and enters the Go binary |
| `go_build` | `go build ./...` compiles the migrated production code |
| `go_scope_and_tests` | `go.mod` + Go test files exist and `go test ./...` passes (behavioural assertions re-established in Go) |
| `go_module_policy` | `go.mod` declares `github.com/ncipollo/release-action` with only `github.com/bmatcuk/doublestar/v4` and the Dockerfile builds the Go binary |
| `workflow_uses_go` | `.github/workflows/test.yml` installs Go and runs `go test ./...` |
| `go_test` | the named Go behavioural test / subtest passes |

## Safety

Every `go build` / `go test` invocation is wrapped in the `timeout` binary and
additionally guarded by a `subprocess` timeout.  No probe requires privileges
and none can block indefinitely.  Errors that are not semantic failures are
reported as `INFRA_ERROR` / `INCONCLUSIVE`, never as `FAIL`.

`source/runner` and `target/runner` are created only by the INTEGRATION batch
and are deliberately absent here.
