# BATCH-P2P-0001 shared assets

Pass-to-pass (P2P) state-preservation assets for the TypeScript 6.0.3 -> Go
1.23.0 migration of `ncipollo/release-action`.

The pre-migration TypeScript behaviour and the post-migration Go behaviour must
produce the same observable projection. `observation-map.json` binds each
observation owned by this batch to the pre-migration TypeScript test and to the
post-migration Go test (or static `action.yml` / REST-mapping check) that
establish it.

Both probe scripts emit exactly one JSON line per observation:

```
{"observation_id": "P2P-001", "result": "PASS"}
```

`PASS` is emitted only when the mapped repository test really executes and
succeeds. A missing or vacuous test match is reported as `FAIL`. The common
normalized projection is the stable wrapper `{"result": "PASS"}` on both sides,
so the fragment gate relation `equal_after_normalization` holds without
comparing human-readable text.

Every external command is wrapped in an explicit `timeout`; the probes never wait
indefinitely.
