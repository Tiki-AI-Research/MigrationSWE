# shared/batches/BATCH-F2P-0001

Language-agnostic F2P probe library shared by both sides of the migration.

- `f2p_predicates.py` — the 12 semantic predicates (compact JSON, millisecond
  `Z` timestamps, indent-2 PRD text, 8-hex story ids, phase coercion, JS
  boolean rendering, stdio identity, packaging entrypoint, no-Node delegation,
  changed-path scope). No side-specific branching.
- `f2p_observations.py` — the observation-to-predicate map plus the `main`
  entrypoint used by both `f2p_target_probe` and `f2p_source_probe`.
- `observations.json` — declarative metadata for the 24 observations.

The identical predicate list is executed on both sides. The target side must
report `PASS` for all 24; the source side must report `FAIL` for all 24.
