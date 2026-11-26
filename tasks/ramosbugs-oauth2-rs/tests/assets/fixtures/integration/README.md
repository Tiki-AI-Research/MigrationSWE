# Integration fixtures

The INTEGRATION batch does not author new fixture payloads. The static fixtures
consumed by the merged probes are the ones published by the dependency batches
under `fixtures/batches/` (`BATCH-F2P-0001`, `BATCH-F2P-0002`, `BATCH-F2P-0003`
and `BATCH-DIFFERENTIAL-0001`). This directory is intentionally empty so the
merged asset root keeps the `fixtures/` entrypoint without duplicating or
rewriting any dependency file.
