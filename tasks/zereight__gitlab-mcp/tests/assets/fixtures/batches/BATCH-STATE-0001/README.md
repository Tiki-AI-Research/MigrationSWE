Deterministic persisted-state fixtures for `BATCH-STATE-0001`.

* `autopilot-state.json` - a valid `omg-autopilot-state.json` body.
* `prd.json` - a valid `.omc/prd.json` document with passing and pending stories.
* `project-memory.json` - a valid `.omc/project-memory.json` store.

Fixtures are pre-images only; probes copy them into a unique temporary
`WORKSPACE_ROOT` and never mutate the originals.
