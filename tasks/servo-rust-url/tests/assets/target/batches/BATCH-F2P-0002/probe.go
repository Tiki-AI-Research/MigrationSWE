// Package f2pprobe holds the F2P target-side probes for batch BATCH-F2P-0002.
//
// Each probe exercises exactly one atomic claim from the batch observation set
// against the migrated Go module github.com/servo/rust-url. The probe package
// is copied into the candidate target module by the INTEGRATION runner and run
// with `go test`; it must never be added to the repository under test directly.
package f2pprobe
