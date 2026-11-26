# BATCH-CONTRACT-0001 fixtures

Declarative input/expected fixtures consumed by the CONTRACT probes of
`BATCH-CONTRACT-0001`.

- `body.txt` is a release-body file used by the `bodyFile` / release-body
  observations (`CONTRACT-023`). Both probes read it from this directory and
  assert the resolved body is exactly its contents.

The remaining inputs for the 31 contract observations are constructed
programmatically by the probes so that the same normalized value can be
reproduced on both sides without depending on the filesystem layout of either
repository.
