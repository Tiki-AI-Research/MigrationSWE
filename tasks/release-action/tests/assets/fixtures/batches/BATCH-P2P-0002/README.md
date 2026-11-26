# BATCH-P2P-0002 fixtures

This batch authors the executable state-preservation (P2P) probes for the
TypeScript 6.0.3 -> Go 1.23.0 migration of `ncipollo/release-action`.

## Layout

- `../shared/batches/BATCH-P2P-0002/p2p_manifest.json` — the authoritative
  mapping of each owned P2P observation to the concrete source (vitest) and
  target (`go test`) probes that exercise it.
- `../source/batches/BATCH-P2P-0002/probe.sh` — runs the pre-migration
  TypeScript suite in `/input/source` and emits the raw source-side projection.
- `../target/batches/BATCH-P2P-0002/probe.sh` — runs the post-migration Go
  suite in `/input/target` and emits the raw target-side projection.

No raw input/expected fixtures are needed: every observation is bound to an
existing repository test, and the common projection is the normalized status
wrapper `{"result": "PASS"}` (source and target are compared for equality after
normalization).

## Execution rules

- Both probes run the repository's own tests; they do not re-implement behavior.
- Every potentially long-running command is wrapped in `timeout` (default
  600s). No command may block indefinitely and none requires privileges.
- The source and target sides are executed independently; neither substitutes
  for the other.
