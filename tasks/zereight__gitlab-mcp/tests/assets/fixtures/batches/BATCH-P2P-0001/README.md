# fixtures/batches/BATCH-P2P-0001

Deterministic `.omc` fixture inputs consumed by the `BATCH-P2P-0001` probes.

Every fixture is a plain JSON document with fixed timestamps so that repeated
probe runs are byte-stable. The probe scripts copy a fixture tree into a unique
temporary `WORKSPACE_ROOT` before exercising the server, and remove it
afterwards; the probes never mutate the fixtures in place.

| Fixture | Observation(s) | Purpose |
| --- | --- | --- |
| `omc/state/omg-autopilot-state.json` | P2P-015, P2P-016, P2P-024 | Persisted autopilot state at phase 4 for phase advancement and phase-table checks. |
| `omc/state/ralph-state.json` | P2P-008, P2P-011, P2P-017, P2P-020 | Active `ralph` state for read/merge/list/clear checks. |
| `omc/prd.json` | P2P-007, P2P-014 | PRD with one passing and one pending story for verification checks. |
| `omc/project-memory.json` | P2P-009, P2P-012, P2P-018 | Memory store with two entries for read/write/delete checks. |
