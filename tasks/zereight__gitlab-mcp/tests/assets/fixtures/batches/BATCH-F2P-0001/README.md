# fixtures/batches/BATCH-F2P-0001

Deterministic fixture inputs owned by `BATCH-F2P-0001`.

- `team-state.json` — the `.omc/state/team-state.json` payload used by
  `pred_team_line_formatting` (R043 / F2P-014). With
  `{"active":true,"current_phase":true}` the `omg_check_completion` line must
  render `team: true` (JS string semantics), never Python's `team: True`.

Probes copy fixtures into a unique temporary `WORKSPACE_ROOT`; nothing here is
mutated in place.
