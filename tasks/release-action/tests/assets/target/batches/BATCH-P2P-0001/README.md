# BATCH-P2P-0001 target probe

Probes the post-migration Go implementation in the target repository
(`/workspace/repo` when running the assembled assets, `/input/target` during
private authoring; overridable through `REPO_ROOT`).

`run_target_p2p.py` reads `shared/batches/BATCH-P2P-0001/observation-map.json`
and, for every observation, runs the mapped Go tests
(`go test -count=1 -v -run '^TestX$' ./...`) plus the static `action.yml` and
REST-mapping checks. `PASS` requires an exact `--- PASS: <TestName>` line for
each mapped test; a missing or vacuous `-run` match counts as `FAIL`, and a
mapped static check must succeed. All commands carry an explicit `timeout`.

Output: one `{"observation_id": "...", "result": "PASS|FAIL"}` line per
observation on stdout. The `target/runner` harness entry point is owned by the
INTEGRATION batch and is not created here.
