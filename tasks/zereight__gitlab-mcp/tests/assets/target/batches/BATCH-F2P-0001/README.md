# target/batches/BATCH-F2P-0001

Target-side F2P probe assets.

- `f2p_target_probe.py` — runs the shared predicates against the migrated
  Python `mcp-server` tree. Defaults to `/input/target/mcp-server` during
  authoring and `/workspace/repo/mcp-server` at grading time; override with
  `--mcp-root`.

Run with the target interpreter:

```
mcp-server/.venv/bin/python target/batches/BATCH-F2P-0001/f2p_target_probe.py
```

Every one of the 24 observations must report `PASS`. The legacy harness entry
point `target/runner` is owned by the INTEGRATION batch and is intentionally
not created here.
