Target-side probe assets for `BATCH-STATE-0001`.

`state_probe.py` imports `omg_mcp` from `sys.path` (supplied by the target
runner) and asserts the persisted-state invariants of observations
`STATE-001`..`STATE-004`: the `.omc` layout under `WORKSPACE_ROOT` and the
process cwd, the autopilot state and PRD story field shapes, and the project
memory entry field shapes. It was executed against the reference target
(`/input/target`) during authoring. The target harness entry point
`target/runner` is created by the INTEGRATION batch only.
