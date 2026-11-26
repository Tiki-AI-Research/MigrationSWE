# target/batches/BATCH-P2P-0001

Target-side P2P probe assets for `BATCH-P2P-0001`.

`probe_p2p.py` executes against the migrated Python repository
(`mcp-server/omg_mcp`) using the target native test command
`python -m unittest discover -s mcp-server/tests -t mcp-server -v`. It consumes
the shared corpus at `shared/batches/BATCH-P2P-0001/probes.json` and, per
observation, emits:

* `status` - the raw side result (`PASS`/`FAIL`);
* `value` - the normalized projection shared with the source side.

The harness entry points `target/runner` and `run_tests.py` are intentionally
**not** created here; they are owned exclusively by the INTEGRATION batch. This
batch only contributes the probe module the runner invokes.
