# BATCH-DIFFERENTIAL-0001 fixtures

- `observations.json` - maps every `DIFF-*` observation to the corpus cases and
  contract requirements that cover it.
- `expected.json` - the reference normalized observations captured by running
  the target probe against the reference Go implementation. The source probe
  must reproduce these values; `shared/batches/BATCH-DIFFERENTIAL-0001/lib/compare.py`
  decides equality.

`expected.json` is a reference capture, not a hardcoded answer: it is produced by
executing the corpus, and the comparison stays strict (ordered requests, parsed
bodies, parsed outputs, normalized diagnostics).
