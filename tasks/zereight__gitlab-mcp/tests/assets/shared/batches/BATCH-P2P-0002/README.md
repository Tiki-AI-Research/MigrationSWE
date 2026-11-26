# shared/batches/BATCH-P2P-0002

Language-agnostic contract for the `BATCH-P2P-0002` pass-to-pass checks of the
`omg-mcp-server` TypeScript-to-Python migration.

`BATCH-P2P-0002` owns 16 already-passing observations. Each side of the
migration runs its own probe and emits one *shared projection* per observation:

- `source/batches/BATCH-P2P-0002/probe.mjs` executes the original TypeScript
  sub-project. It imports `mcp-server/src/*.ts` directly using Node's built-in
  type stripping, resolves the schema-only dependencies (`zod`,
  `@modelcontextprotocol/sdk`) to in-memory stubs, and remaps the `.js` relative
  specifiers to their `.ts` sources with a module customization hook. It
  therefore needs neither a pre-built `mcp-server/dist/` nor a populated
  `mcp-server/node_modules/`. The repository's own public suite
  (`mcp-server/test/run-tests.mjs`) is executed in a throwaway directory whose
  `dist/` shims re-export the TypeScript sources.
- `target/batches/BATCH-P2P-0002/probe.py` runs against the migrated Python
  package (`mcp-server/omg_mcp/`).

`observations.json` records the observation-to-probe mapping, the requirement
ids, and the normalization rules. `fixtures/batches/BATCH-P2P-0002/inputs.json`
records the exact deterministic inputs. The two probes must produce equal
projections after normalization (`equal_after_normalization`); every case is
verified with the repository's own tests as well.

Neither probe creates `source/runner` or `target/runner`; those harness entry
points are owned by the INTEGRATION batch.
