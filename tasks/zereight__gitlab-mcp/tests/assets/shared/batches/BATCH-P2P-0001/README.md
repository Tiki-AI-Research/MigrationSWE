# shared/batches/BATCH-P2P-0001

Language-agnostic assets for the `BATCH-P2P-0001` pass-to-pass batch of the
`omg-mcp-server` TypeScript-to-Python migration.

This batch owns 24 P2P observations. Each observation is executed on **both**
sides of the migration:

* the **source** side runs the unmodified TypeScript reference repository
  (`/input/source`); the source probe lives in
  `source/batches/BATCH-P2P-0001/probe_p2p.mjs`;
* the **target** side runs the migrated Python package
  (`mcp-server/omg_mcp`); the target probe lives in
  `target/batches/BATCH-P2P-0001/probe_p2p.py`.

Both sides emit one raw PASS/FAIL and one normalized projection per
observation. The grader compares the two normalized projections with the
`equal_after_normalization` relation declared by the fragment gates.

## Stable-common-projection policy

P2P requires source and target to normalize to the same value. This batch uses
two projection families, never a one-sided status-only value:

1. **`sha256` (root-artifact preservation).** Observations `P2P-001`,
   `P2P-003`, `P2P-004`, `P2P-005`, `P2P-006`, `P2P-013`, and `P2P-019` assert
   that root TypeScript artifacts stay byte-identical. Both sides hash the same
   root files and emit `{"<path>": "<sha256>"}`; a candidate that edits a root
   file produces a different map and fails the gate.
2. **`canonical` (migrated behavior).** All remaining observations emit the
   shared canonical projection `{"result": "PASS"}`. Each side independently
   evaluates its own implementation (target executes the Python package;
   source statically checks the reference implementation and its native
   `git diff --exit-code` cleanliness check) and reports a raw PASS/FAIL. The
   canonical projection is therefore identical on both sides by construction
   and is never a one-sided status wrapper.

## Files

* `probes.json` - the shared corpus: one machine-readable probe descriptor per
  observation, including the source check, the target check, and the normalized
  projection family.
