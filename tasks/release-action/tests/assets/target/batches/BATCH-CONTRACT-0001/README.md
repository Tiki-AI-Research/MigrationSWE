# BATCH-CONTRACT-0001 target probe

`contract_probe_test.go` is a Go contract probe for the post-migration Go 1.23.0
implementation of `ncipollo/release-action`. It is authored in `package main` so
the INTEGRATION runner can drop it next to the migrated sources and execute it
against `/input/target`.

Every check calls the migrated Go code directly (no network, no Node runtime,
no privileged operations) and asserts the observable contract value for one
CONTRACT observation. The checks do not read or execute the TypeScript
implementation and no assertion is skipped or weakened.

How the batch was calibrated (authoring):

```
cp -R /input/target <workdir>
cp contract_probe_test.go <workdir>/
cd <workdir>
GOMODCACHE=/opt/migration/dependencies/gomodcache GOPROXY=off \
  timeout 300 go test -run TestContractPublicSurface ./...
```

All 31 subtests (`CONTRACT-001` .. `CONTRACT-031`) pass against the reference
target. The runner MUST wrap the `go test` invocation in an explicit `timeout`;
the probe performs no blocking or privileged work.
