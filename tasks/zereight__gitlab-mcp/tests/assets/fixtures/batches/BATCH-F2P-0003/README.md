# BATCH-F2P-0003 fixtures

Deterministic inputs consumed by the F2P probes of `BATCH-F2P-0003`.

All probes create their own isolated temporary workspace (`WORKSPACE_ROOT`)
and remove it afterwards, so no fixture leaks across observations. The
canonical fixture shapes used by the probes are:

- `state/<mode>-state.json` with `{"active": true, ...}` for
  `omg_check_completion` / `omg_list_active`.
- `prd.json` produced by `omg_create_prd` for `omg_read_prd` and generated
  story ids.
- `project-memory.json` entries with non-ASCII values for compact JSON
  serialization.

No shared mutable fixture is written here; this directory only names the
fixture contract so later source/target probe batches can reuse the same
shapes.
