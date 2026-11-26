# shared/integration

Language-agnostic integration harness owned by `BATCH-INTEGRATION-0001` of the
`omg-mcp-server` TypeScript-to-Python migration.

This batch merges the dependency batches and provides the two runtime entry
points (`source/runner`, `run_tests.py`) plus the shared dispatch logic that
turns every per-batch probe into the runtime report schema
`migration-observations:v6`.

## Files

- `observations.json` - the self-contained observation inventory. Each item is
  `{id, kind, batch_id}` and covers **all 148** runtime observations (40 P2P,
  32 DIFFERENTIAL, 72 F2P, 4 STATE). The harness reads only this file for its
  inventory; it never reads `dependencies/` or `fragments/`.
- `harness.py` - the shared dispatcher and aggregator.

## Entry points

| Entry point     | Runtime  | Invocation                                          |
| --------------- | -------- | --------------------------------------------------- |
| `source/runner` | source   | `{assets}/source/runner {report}`                   |
| `target/runner` | target   | `{assets}/target/runner {report}`                   |
| `run_tests.py`  | target   | `{venv}/python {assets}/run_tests.py {report}`      |

Both sides emit **all** observation ids with the raw result observed for that
side. P2P, DIFFERENTIAL, and STATE observations are projected from the batch
probes; F2P observations are the target-only postconditions, so the source side
reports the raw source observation (`FAIL`) and the target side the raw target
observation (`PASS`) - never a role-hard-coded value.

## Dispatch

`harness.py` executes, per side:

- `BATCH-P2P-0001` - root-artifact digests and migrated behavior (source
  `probe_p2p.mjs`, target `probe_p2p.py`);
- `BATCH-P2P-0002` - shared projection corpus (`probe.mjs` / `probe.py`);
- `BATCH-DIFFERENTIAL-0001` - the shared case corpus (`probe.mjs` / `probe.py`);
- `BATCH-F2P-0001/0002/0003` - the shared target postcondition predicates;
- `BATCH-STATE-0001` - the persisted-state invariants.

A probe that cannot execute is reported as `INCONCLUSIVE` rather than being
replaced by its declared expectation. `batch-exec` therefore covers P2P, not
only F2P.

## Node stdout capture

`harness.py` redirects every probe's stdout to a temporary regular file rather
than a pipe. Node.js writes asynchronously to pipes, so the P2P-0002
`probe.mjs` and STATE `state-probe.mjs` probes - which call `process.exit()`
immediately after `process.stdout.write()`/`console.log()` - would otherwise
have their JSON truncated and every observation of those batches reported as
`INCONCLUSIVE`. Writes to a regular file are synchronous on POSIX, so the full
document is always captured and the P2P projections stay executable.

## Placeholders

`{assets}` is derived from the harness file location, `{report}` is
`argv[1]`, and the repository under test is resolved only from
`MIGRATION_REPO_ROOT`, `MIGRATION_REPO`, `REPO_ROOT`, or `REPO`.
