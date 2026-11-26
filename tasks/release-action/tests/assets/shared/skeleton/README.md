# Shared skeleton

This directory holds harness assets shared by both the `source/` and `target/`
probes of the TypeScript 6.0.3 -> Go 1.23.0 migration of `ncipollo/release-action`.

The SKELETON batch only establishes the directory layout and conventions. It
declares no observations, rules, gates, or public-surface evidence.

Conventions for later batches:

- `shared/` contains assets used by both runtimes (for example the shared mock
  GitHub API corpus and reporting helpers).
- `source/` contains assets that probe the pre-migration TypeScript behaviour in
  `/input/source`.
- `target/` contains assets that probe the post-migration Go behaviour in
  `/input/target`.
- `fixtures/` contains declarative input/expected fixtures consumed by probes.

Harness entry points (`source/runner`, `target/runner`) are created only by the
INTEGRATION batch and must not be created here.

Every probe that can block or run long must be wrapped in an explicit `timeout`
and must never wait indefinitely. Assertions compare observable behaviour
(status/code/type, structure, ordering, existence, and normalized
numeric/boolean values); human-readable messages are never the contract unless
the instruction names the exact string as public.
