# Integration harness (BATCH-INTEGRATION-0001)

The integration batch merges the dependency batches and owns the shared runtime
harness. It creates the entry points `source/runner` and `target/runner` and the
self-contained inventory `shared/integration/observations.json`. It owns no
observation, gate, semantic rule, residue rule or state check of its own.

## Files

- `observations.json` - self-contained inventory of every runtime observation,
  with only `id`, `kind` and `batch_id`. The harness reads this instead of any
  authoring-only path.
- `harness.py` - dispatches the executable projection of each dependency batch
  for one side and writes the normalized runtime report.
- `emit_inconclusive.sh` - fallback report writer used only when `python3` is
  unavailable; never fabricates a PASS/FAIL.
- `../../source/runner`, `../../target/runner` - side entry points.

## Runtime contract

- The report is `{"schema_version": "migration-observations:v6", "observations":
  [{"id", "kind", "status", "value": {"type": "json", "value": ...}}]}` and
  covers every id in the inventory.
- Both sides emit every observation id. P2P and DIFFERENTIAL values are the
  stable probe projection (`{"result": ...}`) and must compare equal after
  normalization. F2P observations are target-only postconditions: the source
  probes report their raw unmet result and the target probes report theirs.
- A probe that cannot be executed reports `INCONCLUSIVE`; a probe that exceeds
  its wall-clock budget reports `TIMEOUT`. Expected values are never copied in
  as results; each collector parses the raw per-observation projection the batch
  probe actually emitted.

## Repository projection

- The repository under test is resolved once from the documented variables
  `MIGRATION_REPO_ROOT`, `MIGRATION_REPO`, `REPO_ROOT` or `REPO` (in that order).
  Project-name derived `*_REPO` aliases are never consulted.
- When no override is present the read-only authoring references
  `/input/source` and `/input/target` are used when they exist, otherwise the
  runtime working tree `/workspace/repo`.
- The source and target repositories are resolved independently (they are never
  collapsed onto one another) and projected onto every alias the dependency
  probes read: `REPO_ROOT`/`REPO_ROOT`, `FT_SOURCE`/`FT_TARGET`, the
  side-local `REPO_ROOT` and `REPO_ROOT`, and
  `MIGRATION_REPO_ROOT`/`MIGRATION_REPO`/`REPO`, plus `FT_SIDE`.
  This is what makes the executable projection of each batch run against the
  repository for its own side rather than a stale default.
  `FT_ASSET_ROOT`/`FT_ASSETS_ROOT` point at the asset root.

## Bounds and safety

- Every dispatched command is executed with an explicit coreutils `timeout` and
  a subprocess backstop. Probes that need a TTY, root, network access or that
  block forever are never invoked, and the harness can never wait indefinitely.
- The harness never reads `dependencies/`, `fragments/` or any authoring-only
  path.
