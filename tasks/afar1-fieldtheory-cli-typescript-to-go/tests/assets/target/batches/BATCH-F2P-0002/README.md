# Target-side F2P probes

Probes in this directory run the F2P predicates against the Go reference target
(`FT_REPO=/input/target`, or `/workspace/repo` after migration). Each probe
actually executes the target technology and asserts observable behavior:
process exit codes, SQLite schema/columns, FTS5 BM25 result order, classification
columns, JSON structure, and Go error types.

Each script sets `FT_SIDE=target` before sourcing the shared `lib.sh`. When
`FT_REPO` is not exported the helpers prefer the migrated working tree at
`/workspace/repo` (when it is a Go repository) and otherwise fall back to the
read-only authoring reference at `/input/target`, so the probes still execute
during authoring.

Engine-invocation, engine-argv, and prompt probes have no CLI surface; they copy
the module into a writable temporary tree, inject `gotest/f2p_probe_test.go`,
and run `go test ./.f2pprobe/ -run <TestF2P...>` under an explicit timeout.

These probes were executed against `/input/target` during authoring and all 24
report `PASS`.
