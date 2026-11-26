# Refreshing the source order baseline

The verifier Dockerfile automatically runs `tests/verifier/refresh_order_baseline.py`
before its selfcheck. It installs the frozen source lockfile separately from the
target dependencies, rebuilds the bundled source with Node 24 and Zod 4.0.5,
and activates `tests/verifier/order-coverage.patch` together with freshly
collected source evidence. A failed collection aborts the image build. New
target fields are never compared against an old source baseline.
The prepared bundle is retained in a root-only image directory; the grader
re-executes that copy so a Harbor upload of the legacy snapshots cannot disable
the new checks. Missing prepared evidence is an infrastructure error.

Run from a trusted environment with Node 24 and the original Zod 4.0.5
`node_modules`, including the original build dependencies:

```sh
python3 tasks/maintenance/refresh_order_baseline.py \
  --dependencies /path/to/source/node_modules \
  --node /path/to/node24/bin/node \
  --npm /path/to/node24/bin/npm
```

The command rebuilds a fresh checkout from the bundled source repository. It
never loads candidate code or uses candidate outputs as a baseline. Before
publishing changes, it checks that all 36 existing differential observations
still match their frozen values, adds order evidence from the source run,
updates affected snapshots and source/target copies embedded in `test.patch`,
refreshes asset hashes, and runs the staged verifier selfcheck. Failure before
publication leaves `tasks/tests` unchanged.

The first probe preserves each original observation alongside its serialization
before key sorting. The second captures generated documents and components
before canonicalization. It keeps diagnostic assertion counts separate so
permitted test additions remain permitted. Source provenance records the Node
and Zod versions, source bundle, revision, and probe hashes.

The checked-in snapshots retain their certified legacy values. The image build
collects the additional order fields from the source, verifies all legacy
values, and publishes probes, snapshots, embedded assets and hashes atomically.
No candidate code or observations take part in this preparation. The manual
entry point above uses the same implementation when a checked-in refresh is
needed. Runtime validation of this change belongs to the backend rebuild and
rescoring process.
