# target/batches/BATCH-DIFFERENTIAL-0001

Target-side differential probe assets for the `omg-mcp-server`
TypeScript-to-Python migration.

- `probe.py` — executes `shared/batches/BATCH-DIFFERENTIAL-0001/corpus.json`
  against the reference Python target. Tool handlers are imported read-only from
  `<target>/mcp-server/omg_mcp` and invoked with the corpus arguments; module
  steps import the same package's helpers. Each case runs in a throwaway
  `WORKSPACE_ROOT`; the probe never copies or rewrites target files.

Usage (run by the INTEGRATION-owned `run_tests.py` harness):

```
python target/batches/BATCH-DIFFERENTIAL-0001/probe.py \
  --corpus shared/batches/BATCH-DIFFERENTIAL-0001/corpus.json \
  --fixtures fixtures/batches/BATCH-DIFFERENTIAL-0001 \
  --server-dir <target>/mcp-server \
  --repo-root <target> \
  --output <report>
```

The probe emits `{batch_id, probe:"target", observations:[...]}`. This probe was
executed against `/input/target` during authoring and every non-static
observation resolved to a concrete normalized result.
