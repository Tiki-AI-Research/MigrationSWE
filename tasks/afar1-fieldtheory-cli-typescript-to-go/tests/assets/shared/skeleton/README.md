# Migration test-asset skeleton

Shared scaffolding for the TypeScript (`source`) -> Go (`target`) migration test
assets. This directory is owned by `BATCH-SKELETON-0001` and contains no
observations or gates of its own: later batches add probes under
`source/skeleton`, `target/skeleton`, and `fixtures/skeleton`.

## Layout

- `shared/skeleton/` - helpers shared by the source and target probes.
- `source/skeleton/` - probes executed against the TypeScript source tree.
- `target/skeleton/` - probes executed against the Go reference target.
- `fixtures/skeleton/` - deterministic inputs used by both sides.
- `fragments/` - one fragment file per authoring batch.

## Harness rules

- `source/runner` and `target/runner` are created by the INTEGRATION batch only;
  no other batch creates, copies, or declares those paths.
- Every potentially long-running command (`go test`, `npm test`, `cargo test`,
  ...) MUST be wrapped by an explicit `timeout` via `shared/skeleton/timeout.sh`.
  The harness never waits indefinitely.
- Cases that require privileges, network access, or that can block forever are
  excluded from the harness.
- Probes assert observable behavior only: process exit codes, structured error
  types/codes, JSON structure/field/ordering, presence checks, and normalized
  numeric/boolean values.
