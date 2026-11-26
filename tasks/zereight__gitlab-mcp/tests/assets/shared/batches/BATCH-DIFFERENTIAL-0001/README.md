# shared/batches/BATCH-DIFFERENTIAL-0001

Language-agnostic assets shared by the source and target differential probes
owned by `BATCH-DIFFERENTIAL-0001`.

- `corpus.json` — the shared case corpus. Every case carries the observation id
  (`DIFF-0NN`), the contract requirement ids, an observable surface, and an
  ordered list of steps (`setup`, `tool`, `read`, `read_dir`, `module`,
  `tools`, `identity`, `hash_paths`). It also declares the normalization
  contract (`mask_keys`, timestamp/id regexes, workspace masking, and list
  ordering) that both probes apply before the grader compares results with
  relation `equal_after_normalization`.

The corpus is descriptive, not executable on its own: the source probe
(`source/batches/BATCH-DIFFERENTIAL-0001/probe.mjs`) and the target probe
(`target/batches/BATCH-DIFFERENTIAL-0001/probe.py`) consume it and emit the same
observation shape. The INTEGRATION batch owns the top-level `source/runner` and
`target/runner` entry points; this batch deliberately does not create them.
