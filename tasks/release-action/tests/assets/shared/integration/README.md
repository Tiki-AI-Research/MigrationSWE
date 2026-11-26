# INTEGRATION shared harness

Shared, self-contained harness assets owned by `BATCH-INTEGRATION-0001`.

The INTEGRATION batch merges the per-batch probes produced by the SKELETON,
CONTRACT, P2P, F2P and DIFFERENTIAL batches and exposes the two runtime entry
points `source/runner` (pre-migration side) and `target/runner` (post-migration
side).

## Layout

- `observations.json` - the self-contained observation inventory. Each entry is
  exactly `{id, kind, batch_id}`; it is the only thing the runtime reads to know
  which observations exist. It intentionally does not embed expectations, so the
  runner can never report a declared answer instead of a measured one.
- `runner.py` - the dispatch engine. It runs the owning batch probe for every
  inventory entry on the requested side and writes a
  `migration-observations:v6` report.

## Runtime contract

The target F2P/P2P path runs `public_behavior.py` through workflow inputs,
REST calls and workflow outputs. The published observation IDs and required
groups stay in place; test replacement still requires an executed Go assertion
suite and passing public behavior checks.

The `skipIfReleaseExists` scoring boundary follows this instruction text:

> When `skipIfReleaseExists` is true and a non-draft release exists for the tag, the action MUST skip the remaining work and report a notice. When that lookup fails, the action MUST continue instead of failing.

This specifies the non-draft and failed-lookup outcomes, but does not require
continuation after a successful draft lookup. The extra draft-continuation
assertion is removed from the active adapter, legacy probes and case tables.
Boolean input validation, non-draft skipping and failed-lookup continuation
remain required. Historical captured source/reference outputs are preserved.

`source/runner [report]` and `target/runner [report]` both write a report of the
form:

```json
{
  "schema_version": "migration-observations:v6",
  "observations": [
    {"id": "P2P-001", "kind": "P2P", "status": "PASS",
     "value": {"type": "json", "value": {"result": "PASS"}}}
  ]
}
```

Every inventory observation appears exactly once on both sides, including the
P2P projections (the integration entry point must dispatch P2P, not only F2P).
`status` is one of `PASS`, `FAIL`, `NA`, `INFRA_ERROR`, `INCONCLUSIVE`,
`TIMEOUT`; the raw side result is measured, never copied from the declared
expectation.

## Dispatch map

| batch | source probe | target probe |
| --- | --- | --- |
| BATCH-P2P-0001 | `source/batches/BATCH-P2P-0001/run_source_p2p.py` | `target/batches/BATCH-P2P-0001/run_target_p2p.py` |
| BATCH-P2P-0002 | `source/batches/BATCH-P2P-0002/probe.sh` | `target/batches/BATCH-P2P-0002/probe.sh` |
| BATCH-F2P-0001 | `source/batches/BATCH-F2P-0001/probe.sh` | `target/batches/BATCH-F2P-0001/probe.sh` |
| BATCH-F2P-0002 | `source/batches/BATCH-F2P-0002/probe.sh` | `target/batches/BATCH-F2P-0002/probe.sh` |
| BATCH-F2P-0003 | `source/batches/BATCH-F2P-0003/run_probe.sh` | `target/batches/BATCH-F2P-0003/run_probe.sh` |
| BATCH-DIFFERENTIAL-0001 | `source/batches/BATCH-DIFFERENTIAL-0001/probe.py` | `target/batches/BATCH-DIFFERENTIAL-0001/probe.py` |
| BATCH-CONTRACT-0001 | `source/batches/BATCH-CONTRACT-0001/contract_probe.test.ts` | `target/batches/BATCH-CONTRACT-0001/contract_probe_test.go` |

The differential probes select their repository root from `REPO_ROOT` /
`REPO_ROOT`. The engine exports those - together with `REPO_ROOT`, which the
P2P and F2P probes read - from the tree the harness was actually handed, so the
same report works for the evaluation checkout without consulting any
authoring-only mount.

## Missing native runtime

A probe may be unable to execute on a side that lacks its native runtime - for
example the pre-migration TypeScript tree on a host with no Node.js toolchain.
In that case:

- the P2P surface falls back to the module-integrity projection its source probe
  declares (the backing module must exist and be non-empty; the `action.yml`
  schema and suite files must be present), which is a real measurement of the
  source tree rather than a copied expectation;
- the DIFFERENTIAL and CONTRACT surfaces that require the missing runtime are
  reported as `INCONCLUSIVE` for that side instead of a fabricated PASS/FAIL.

F2P is target-only by construction: the source side reports a genuine `FAIL`
because the Go target technology is absent, and the target side reports a
genuine `PASS` by executing the frozen predicates against the migrated tree.

## Timeouts and safety

Every command that can run long (`go build`, `go test`, `vitest`, the batch
probes) is wrapped in an explicit `timeout` and additionally bounded at the
`subprocess` level, so the harness never waits indefinitely. No command needs
privileges, network access or an interactive terminal. A probe that cannot run
in the current environment degrades to `INCONCLUSIVE` / `INFRA_ERROR` for its
observations instead of blocking the report.
