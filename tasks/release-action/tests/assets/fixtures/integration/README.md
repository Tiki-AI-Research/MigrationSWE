# INTEGRATION fixtures

Fixtures owned by `BATCH-INTEGRATION-0001`.

The INTEGRATION batch does not introduce new observation fixtures of its own; it
reuses the declarative fixtures published by the dependency batches (for example
`fixtures/batches/BATCH-DIFFERENTIAL-0001/expected.json` and
`fixtures/batches/BATCH-F2P-0001/observations.json`) through the batch probes.

The self-contained observation inventory that the runtime harness consumes lives
next to this directory at `shared/integration/observations.json`; it is shared
with both sides, so this directory intentionally stays empty of fixtures.
