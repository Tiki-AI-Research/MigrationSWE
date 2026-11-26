# BATCH-F2P-0003 shared assets

Language-agnostic material for the F2P observations owned by
`BATCH-F2P-0003` of the `omg-mcp-server` TypeScript-to-Python migration.

## Batch contract

- Kind: `F2P` (target-only postcondition: source side must be `FAIL`,
  target side must be `PASS`).
- Contract requirements exercised: R001, R034, R035, R036, R040, R041, R043,
  R044, R045, R046.
- Declared target technology: Python 3.13.0 (`omg_mcp`).
- Declared source technology: TypeScript 5.9.3.

## Asset layout

- `fragments/BATCH-F2P-0003.json` - the fragment response (gates and rules).
- `target/batches/BATCH-F2P-0003/probe_target_f2p.py` - executes every
  observation's postcondition against the reference target and records the raw
  target observation.
- `target/batches/BATCH-F2P-0003/target_probe_results.json` - recorded target
  results.
- `source/batches/BATCH-F2P-0003/probe_source_f2p.py` - records the raw source
  observation against the TypeScript/Node.js source tree.
- `source/batches/BATCH-F2P-0003/source_probe_results.json` - recorded source
  results.
- `fixtures/batches/BATCH-F2P-0003/` - deterministic fixture material.

The `source/runner` and `target/runner` entry points are intentionally absent:
they are owned by the INTEGRATION batch.

## Reproducing

```sh
PYTHONPATH=/input/target/mcp-server /opt/migration/dependencies/venv/bin/python \
  target/batches/BATCH-F2P-0003/probe_target_f2p.py

python source/batches/BATCH-F2P-0003/probe_source_f2p.py
```
