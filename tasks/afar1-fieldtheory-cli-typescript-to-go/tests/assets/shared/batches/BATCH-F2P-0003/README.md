# BATCH-F2P-0003 test assets

F2P (fail-to-pass) probes for the TypeScript -> Go migration of the Field Theory
CLI (`ft` / `fieldtheory`).

Every observation in this batch is a **target-only post-condition**: the
required predicate MUST hold when the Go reference implementation is executed
and MUST NOT hold when the legacy TypeScript implementation is executed.

- The source side evaluates the same predicate against the real `/input/source`
  tree, once per observation, using that observation's own projected entry (its
  legacy scope file plus the concrete Go artifact that must replace it). The Go
  target technology is absent there, so the raw observation is `FAIL`.
- The target side builds `/input/target` and records the observable behavior
  (`PASS`).

## Layout

- `shared/batches/BATCH-F2P-0003/probe_lib.sh` - shared helper library used by
  both sides. It wraps the skeleton timeout helper and implements the result
  protocol (`OBS <id> <status>`) plus the optional JSON report writer.
- `source/batches/BATCH-F2P-0003/run-source-probes.sh` - evaluates each
  observation's target projection against `/input/source` and emits one `OBS`
  line per observation.
- `target/batches/BATCH-F2P-0003/run-target-probes.sh` - builds the Go program
  from `/input/target` and exercises the observable behavior, emitting one `OBS`
  line per observation.
- `fixtures/batches/BATCH-F2P-0003/` - deterministic fixtures (see below).
- `fragments/BATCH-F2P-0003.json` - the asset fragment consumed by the
  integration entry point.

Both probe scripts accept an optional report path and write a JSON report:

```
run-<side>-probes.sh [report.json]
```

## Result protocol

`OBS <observation_id> <status>` is written to stdout and accumulated in a
private per-invocation sink. When the integration entry point exports
`FT_RESULTS_FILE` the same lines are appended there (never truncated), so many
batches can fan into one stream without losing earlier observations. The status
is the raw observation for the side, never an expectation or a role.

## Timeout policy

`shared/skeleton/timeout.sh` (from `BATCH-SKELETON-0001`) is sourced when
available. Every potentially long-running command (`go build`, `go test`,
`ft index`, `ft classify --regex`) is executed through `timeout`/`run_timed`.
The source side evaluates a target-only post-condition and runs no long-lived
command at all. Probes never invoke interactive TTY prompts, network access,
root-only operations, or any command that can block forever. Engine stubs are
bounded and terminate on their own.

## Fixtures

- `bookmarks.jsonl` - five deterministic records exercising HTML-unsafe text,
  both cached timestamp layouts, an unparseable timestamp, and the
  `security`/`commerce` classification priority.
- `go/*_probe_test.go` - Go probe tests transplanted into a private copy of the
  reference tree at probe time. They assert only observable behavior: argv and
  label shape, structured error reasons/codes, UTC display labels, the
  `.preferences` on-disk contract, environment no-override loading, and the
  interactive helper result kind.

## Observation map

| Observation | Requirement | Surface |
|-------------|-------------|---------|
| F2P-049 | R050 | engine argv / label |
| F2P-050 | R001 | `fieldtheory path` in-process |
| F2P-051 | R043 | `ft index` self-healing refresh |
| F2P-052 | R052 | `make build` artifacts + go.mod |
| F2P-053 | R041 | `ft search --json` HTML escaping disabled |
| F2P-054 | R041 | `.preferences` load/save 0600 |
| F2P-055 | R046 | engine invocation bridge |
| F2P-056 | R045 | `ft classify --regex` categories |
| F2P-057 | R034 | node-less execution path |
| F2P-058 | R049 | `ft commands` traversal rejection |
| F2P-059 | R049 | `ft library` traversal rejection |
| F2P-060 | R046 | engine invocation bridge (LLM module) |
| F2P-061 | R049 | `ft library`/`ft commands` path handling |
| F2P-062 | R034 | node-less execution path (entry) |
| F2P-063 | R044 | timestamp display labels |
| F2P-064 | R051 | `ft bookmarks` alias unknown flags |
| F2P-065 | R041 | `.preferences` 0600 |
| F2P-066 | R048 | env loading without override |
| F2P-067 | R048 | env loading dependency execution |
| F2P-068 | R049 | `ft commands` traversal rejection |
| F2P-069 | R049 | `ft commands` traversal rejection (companion) |
| F2P-070 | R047 | prompt interrupt / close |
| F2P-071 | R044 | date helper labels |
| F2P-072 | R048 | env loading runtime entry |
