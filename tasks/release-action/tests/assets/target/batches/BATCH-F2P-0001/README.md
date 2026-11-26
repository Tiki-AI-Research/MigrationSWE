# BATCH-F2P-0001 target probe

`probe.sh` runs the shared fail-to-pass predicates
(`shared/batches/BATCH-F2P-0001/f2p_checks.py`) against the post-migration Go
tree at `/input/target` by default.  The same predicate, byte-for-byte as the
source probe, is applied here.

The reference Go target satisfies every F2P observation: the action uses a
`docker` / `Dockerfile` entry point that builds the Go binary, `go build ./...`
and `go test ./...` succeed, `go.mod` declares only `doublestar`, the CI
workflow installs Go and runs `go test`, and the named behavioural tests pass.

Usage:

```sh
timeout 600 target/batches/BATCH-F2P-0001/probe.sh /input/target /logs/target-result.json
```

This directory is not a harness entry point: `target/runner` is created only by
the INTEGRATION batch.
