# Verifier regressions

Run the evaluator's focused and complete self-checks:

```sh
python3 tests/verifier/test_security_selfcheck.py --quick
python3 tests/verifier/test_security_selfcheck.py --full
```

Run these commands from the task directory. The verifier image supplies the
trusted Peggy and TypeScript packages under `/opt/verifier-tools`. On a host,
provide an equivalent trusted TypeScript installation through `NODE_PATH`.

The false-negative controls cover:

- Documented `node npm-scripts.mjs grammar` builds without an npm shortcut.
- Static, transitive build-helper imports, including helpers in the repo root
  and literal `import('./helper.mjs')` calls. Computed module specifiers remain
  forbidden.
- Runtime imports of build/test helpers, which must still trigger security checks.
- Cross-realm and module-loading tests that are not production fallbacks.
- Named and anonymous CommonJS wrappers, including ASI-terminated output, and
  both Peggy initializer forms.
- The explicitly specified Peggy `SyntaxError` declaration surface, including
  `buildMessage`, `format`, and its four-argument constructor, without imposing
  zero- or one-argument overloads and with `location.source` in the fixture.
  Negative declaration checks also require `buildMessage`'s expectation array
  and Peggy's exact two-element tuple shape for character-class ranges.
- Peggy's five discriminated `SyntaxError.expected` variants, rejecting `any`,
  `unknown`, and broad type-tag declarations that omit variant-specific fields.
- Post-export adapters that preserve Peggy's original export and delegate to
  its parser, including parser-object reassignment wrappers, grammar-generated
  parse factories, inline forwarding adapters, and reproducible build-generated
  module wrappers.
- Two clean, byte-identical rebuilds, source-mutation rejection of cached
  artifacts, and cache invalidation when a build helper changes.

The false-positive controls validate the exact published npm SRI values for
Peggy 4.2.0 and its migration-specific dependency closure. A fabricated string
with a `sha512-` prefix is not accepted as a usable lockfile integrity value.

Export layout alone never establishes cheating. The compatibility exception
still requires generation evidence, reproducibility, and unchanged runtime
contracts. Existing dependency, test-integrity, fallback, and TypeScript checks
remain enabled. Historical job results are not rewritten; use the updated
verifier image to re-evaluate existing candidate patches.
