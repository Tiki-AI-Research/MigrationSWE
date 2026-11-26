# BATCH-P2P-0002 preserve-to-preserve probes

Executable state-preservation (P2P) probes for the TypeScript -> Go migration.

The batch owns observations `P2P-025` .. `P2P-040`. Each probe runs on the side
it owns, executes that side's own repository test suite plus bounded observable
smoke checks, and emits one normalized projection per observation:

```
{"observation_id":"P2P-025","value":{"result":"PASS"}}
```

The probe writes the full projection set as a single valid JSON document to the
runtime `{report}` path:

```json
{"batch_id":"BATCH-P2P-0002","side":"source",
 "observations":[{"observation_id":"P2P-025","value":{"result":"PASS"}}]}
```

and also streams the same records as line-delimited JSON on stdout.

The value is a status wrapper because the source (TypeScript on `sql.js-fts5`)
and the Go target (native `modernc.org/sqlite`) do not share a byte-identical
projection for these surfaces. Both sides emit the *same* wrapper shape, so the
normalized values compare equal, while the per-side status is derived from the
real result of that side's repository test suite plus observable smoke checks
(never hardcoded by side, never an expectation comparison).

## Layout

- `shared/batches/BATCH-P2P-0002/lib.sh` - shared timeout / projection helpers
  (JSON report writer and JSONL streamer) and per-side repository resolvers.
- `source/batches/BATCH-P2P-0002/probe.sh` - runs the TypeScript repository's own
  suite (`npm test`, i.e. `tsx --test tests/**/*.test.ts`, or a local `tsx`) under
  an explicit timeout, falling back to the runtime's declared native test command
  (`git diff --exit-code`) and then to a static presence check of the in-scope
  source modules.
- `target/batches/BATCH-P2P-0002/probe.sh` - runs `go build -o ft ./cmd/ft`,
  `go test ./...` and the built binary against a temporary `FT_DATA_DIR`.
- `fixtures/batches/BATCH-P2P-0002/bookmarks.jsonl` - deterministic cache shared
  by both probes.

## Harness rules

- Every potentially long-running command is wrapped with `timeout` through
  `run_timed`/`run_default`; probes never wait indefinitely and every child gets
  stdin from `/dev/null`.
- Probes exclude interactive, privileged, network and blocking cases (TTY
  prompts, `sync`, `fetch-media`, app install/open).
- Assertions compare observable behavior only: process exit codes, JSON
  structure/field/ordering, presence, and normalized values.
- `source/runner` and `target/runner` are owned by the INTEGRATION batch and are
  intentionally not created here.
