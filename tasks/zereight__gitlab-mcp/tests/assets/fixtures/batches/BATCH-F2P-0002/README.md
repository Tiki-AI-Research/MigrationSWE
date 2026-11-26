# fixtures/batches/BATCH-F2P-0002

Deterministic fixture inputs consumed by the shared F2P predicates in
`shared/batches/BATCH-F2P-0002/f2p_predicates.py`.

- `team-state.json` — state file content used by the `omg_check_completion`
  team-line predicates (`F2P-035`, `F2P-036`, `F2P-040`). The `current_phase`
  value is the JSON boolean `true`, which must render as `team: true`.
- `omg-autopilot-state.json` — autopilot state used by the `omg_next_phase`
  predicate (`F2P-046`); persisting phase 4 lets the call advance to phase 5.

The predicates copy these files into a fresh temporary `WORKSPACE_ROOT`
(`<tmp>/.omc/state/`) for every run and delete the directory afterwards, so no
fixture state leaks between observations.
