# Shared skeleton

Language-agnostic scaffolding for the oauth2 Rust-to-Go migration test assets.

The shared corpus (see `corpus.json`) is the single registry of observable
scenarios that both the source-side and target-side probes consume so that the
Rust reference behavior and the migrated Go behavior are compared on identical
inputs. The SKELETON batch only establishes this container; concrete scenarios
are appended by later batches.

The runner entrypoints `source/runner` and `target/runner` are owned exclusively
by the INTEGRATION batch and are intentionally absent here.
