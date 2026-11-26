# Integration fixtures

BATCH-INTEGRATION-0001 owns the shared harness and the observation inventory but
defines no observation of its own, so it introduces no fixture of its own here.

Each dependency batch keeps its deterministic inputs under
`fixtures/batches/<BATCH_ID>/`. The integration harness consumes those fixtures
only indirectly, by dispatching the batch probes that reference them, and never
copies or rewrites any dependency artifact.
