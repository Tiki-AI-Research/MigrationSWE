# Target probe — BATCH-DIFFERENTIAL-0001

`probe_test.go` is the Go-side half of the differential corpus. It is authored in
`package oauth2` so it can be dropped into the migrated package and executed
against the reference target at `/input/target`:

```
go test -run TestDIFF ./oauth2/
```

Each `TestDIFFnnn` corresponds to observation `DIFF-nnn` and asserts the same
normalized observable projection that the source probe observes, so the two
sides can be compared with the relation `equal_after_normalization`. The probe
has been executed against `/input/target` and all 22 tests pass.

The runner entrypoint `target/runner` belongs exclusively to the INTEGRATION
batch and is intentionally absent here.
