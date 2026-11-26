# Target-side probe skeleton

Probes in this directory run against the Go reference target (`/input/target`)
and assert the migrated behavior of the `ft` / `fieldtheory` executable and its
`internal/` packages. They are created by later authoring batches;
`BATCH-SKELETON-0001` only reserves the directory.

## Conventions

- Reuse the timeout helpers in `shared/skeleton/timeout.sh`; every allowed
  long-running command (`go build ./...`, `go vet ./...`, `go test ./...`) MUST
  be wrapped with an explicit `timeout`.
- `go test ./...` is the only test entry point; do not rely on network access,
  root privileges, or any command that can block forever.
- Target probes MUST be executed against the real reference target before a
  result is declared PASS; never declare a result from inspection alone.
- Assertions compare observable behavior only (exit codes, structured error
  types/codes, JSON structure/field/ordering, presence, normalized values).
