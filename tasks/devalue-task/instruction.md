# Problem Background

Users rely on devalue to serialize rich JavaScript values into a compact payload and later restore those values. The current package is based on devalue 4.3.3. A major-version migration is required: existing consumers must keep observing the public entry points and behaviors the current package provides, and they must also receive the capabilities introduced by devalue 6.0.0.

# Goal

Migrate the package from devalue 4.3.3 to devalue 6.0.0. The completed migration must provide the devalue 6.0.0 package metadata and dependency state while preserving every public contract and observable behavior specified below, and adding every behavior listed as required by 6.0.0. The package must no longer retain a dependency on devalue 4.3.3.

# Public contract to preserve

- stringify(value, reducers?) MUST return a flattened JSON string that parse can revive.
- parse(serialized, revivers?) MUST equal unflatten(JSON.parse(serialized), revivers).
- uneval(value, replacer?) MUST return equivalent JavaScript source, including an IIFE for cyclic values.
- Cyclic and repeated references MUST round-trip to the same instance identity after parse.
- undefined, NaN, Infinity, -Infinity, -0, BigInt, Date, RegExp, Map, Set, plain objects and arrays MUST round-trip correctly.
- Sparse arrays MUST round-trip correctly, including arrays with very large declared lengths and few populated entries.
- Serializing a function or a thenable through the synchronous path MUST throw a DevalueError that exposes the failing path.

# Behavior required by 6.0.0

- stringifyAsync(value, reducers?, options?) MUST await thenables and return the same payload format as stringify. Payloads produced by the synchronous and asynchronous paths MUST remain mutually consumable and format-compatible.
- defaultStringifyOperations and defaultParseOperations MUST be exported and frozen.
- DevalueError MUST expose name, path, value and root, and filterArrayIndices MUST be exported.
- ArrayBuffer, TypedArray, DataView, URL, URLSearchParams and Temporal values MUST round-trip correctly, in addition to the preserved value types.
- Serializing a Symbol primitive through the synchronous path MUST throw a DevalueError that exposes the failing path.
- parse and unflatten MUST reject malformed payloads, out-of-range indices, and __proto__ properties. They MUST also reject null-prototype object payloads whose key is not a string.
- uneval(value, replacer) MUST pass `(value, js)` to the replacer, where `js` is a template tag that returns an opaque fragment. A fragment's interpolated values MUST be serialized recursively, including fragments nested inside fragments, and MUST keep shared-reference identity with values serialized elsewhere in the same call. The replacer MUST accept only such fragments, or `undefined`, `null` or `false` to serialize the value normally; any other return value, including the former string form, MUST throw `TypeError`. Identifier-like words written in a fragment's template strings, including in nested fragments, MUST be reserved so generated names cannot shadow them. When a replaced value participates in a cycle that also reaches a Map, it MUST be constructed exactly once and only after the Map entries that precede it already exist: reading that Map's keys during construction MUST observe those preceding entries in order, and the revived Map MUST keep the original insertion order.
- uneval MUST inline every value that occurs exactly once at the position where it occurs, and MUST NOT allocate a generated binding for it; generated bindings are reserved for values that occur more than once or for references that have to be hoisted to break a cycle. This applies inside fragments as well: a fragment MUST be emitted at its interpolation point inside the enclosing fragment's template text, so bindings declared there, such as a class or a function, stay visible to every interpolated value and to nested fragments. Generated source that only evaluates after such a binding is moved out of the enclosing fragment's scope is not equivalent source.
- stringify, stringifyAsync and uneval MUST emit well-formed output for strings containing unpaired UTF-16 surrogates, so the result survives UTF-8 encode/decode transport unchanged; unpaired surrogates MUST be escaped rather than emitted raw, while well-formed surrogate pairs MUST keep their original characters. This MUST hold for strings inside nested arrays and objects, for object property keys, and for Map and Set entries.
- Node.js Buffer values MUST be serialized as Uint8Array values containing only the Buffer's visible bytes (byteOffset through byteOffset + byteLength). Their backing store MUST be copied, so bytes from a shared Buffer allocation pool are never emitted. Repeated references to the same Buffer MUST keep their identity after parse, while distinct Buffers MUST revive with separate backing stores. uneval MUST likewise emit only the visible bytes.
- Ordinary typed arrays and DataView values MUST keep their existing semantics: the entire backing ArrayBuffer is serialized, including bytes outside a subarray or subview. The Buffer rule applies only to actual Node.js Buffer instances; `buffer.buffer`, or a plain typed array or DataView created over a Buffer's backing store, is serialized as an ordinary view.
- When a reviver replaces the serialized ArrayBuffer that a typed array or DataView refers to, revival MUST use only an actual ArrayBuffer or SharedArrayBuffer as that backing store. A reviver result that is a number, an array-like object, or any other value that is not an ArrayBuffer or SharedArrayBuffer MUST make parse and unflatten throw instead of allocating a new backing store from it. Legitimate ArrayBuffer and SharedArrayBuffer backings MUST still revive with their original byteOffset, byteLength and bytes, for full views as well as subarrays and DataViews.
- A settled stringifyAsync call MUST NOT leave a rejected thenable unhandled. When the call fails for any reason, the returned promise MUST reject with that failure and every thenable the call already reached MUST have its rejection handled, so the process reports no unhandled promise rejection. This MUST hold when the failing value is encountered before, after, or nested beside the thenables in arrays and Map entries, and when a thenable rejects while traversal is still running or after the first failure has already been detected. A call that succeeds MUST NOT report an unhandled rejection either, and a single rejected thenable MUST still reject the returned promise with the original rejection reason.
- uneval MUST emit a distinct string value whose length is 128 characters or more only once per call and MUST reference it from every position where it occurs, at any depth, inside arrays and objects, as a Map key or value, and as Set member content. The same rule applies to BigInt values. Shorter repeated strings MAY stay inline. Evaluating the emitted source MUST still produce the original values at every position, and a boxed primitive that is referenced more than once MUST still revive as a single wrapper instance holding the same primitive value.
- uneval MUST reproduce an array's declared length, own elements and holes exactly, including when the array's prototype chain supplies an index that the array itself does not own. A prototype-supplied element MUST NOT be emitted as an own element, MUST NOT appear as a value in the revived array, and MUST NOT shorten the revived array.

# Public type surface

The migrated package MUST ship TypeScript declarations for its public API. A consumer that imports the package by its published name MUST resolve those declarations through the package's own declared type entry, and the declarations MUST keep exporting every type name the package already exports.

The declarations MUST additionally export these three names:

- `JavaScriptFragment` — the opaque value produced by the `js` template tag and accepted from `uneval` replacers. Consumers can pass a fragment around as a value, but cannot obtain one from a string, a plain object or any other forged literal.
- `JavaScriptTag` — the type of the `js` argument that `uneval` passes to a replacer. It is callable only as a tagged template, its string parts are emitted verbatim, and its interpolated values accept arbitrary values.
- `UnevalReplacer` — the replacer signature `(value, js) => ...` whose result is a `JavaScriptFragment`, or `false`, `null`, `undefined` or no value at all so the value is serialized normally.

`UnevalReplacer` MUST reject at the type level every replacer result the runtime rejects: strings (including the former string form), numbers, booleans other than `false`, plain objects and functions. A strict TypeScript consumer that imports these names from the package and uses them as described MUST type-check without errors.

# Migration toolchain and dependency contract

The migration includes the package toolchain, not only the runtime source. The
exact target contract is available at `/opt/migration/target/`:

- `/opt/migration/target/package.json` lists the required `devDependencies`
- `/opt/migration/target/pnpm-lock.yaml` is the frozen target lockfile
- `/opt/migration/target/README.md` repeats the commands and requirements below

The migrated package version MUST be `6.0.0`, and its `devDependencies` MUST be:

- `@changesets/cli` `^2.29.6`
- `@js-temporal/polyfill` `^0.5.1`
- `@types/node` `^24.12.0`
- `dts-buddy` `^0.8.3`
- `prettier` `^3.2.4`
- `publint` `^0.3.12`
- `typescript` `^6.0.3`
- `vite` `8.3.0`
- `vitest` `5.0.1`

The migrated package MUST also declare `engines.node` with a lower bound of
Node.js `22.17` or newer. The target manifest uses `>=22.17`.

`uvu` MUST be removed from both the manifest and the lockfile. Replace the
workspace lockfile with the frozen target lockfile from
`/opt/migration/target/pnpm-lock.yaml` (or reproduce it exactly from that
dependency set). The evaluator deletes `node_modules` and runs:

```sh
pnpm install --offline --frozen-lockfile
```

so the manifest and lockfile must resolve the target graph without network
access.

The package scripts MUST support:

```sh
pnpm build
pnpm test
pnpm run test:types
```

The target scripts are `dts-buddy` for `build`, `vitest run --project unit` for
`test` and `test:unit`, and `tsc -p . --noEmit` for `test:types`. Add the
`vitest.config.js` (`unit` project covering the package's source and test
suites) and `tsconfig.json` needed by those commands.

## Test runner migration

The existing suite is written for `uvu`, which this migration removes. The
migrated package MUST run that suite through the `vitest` `unit` project and
`pnpm test` MUST pass on the result:

- Registration only: keep every suite name, test name and scenario value
  exactly as they are, with the same grouping and the same cases.
- Assertion semantics MUST NOT change. The equality helper is deep structural
  equality, not identity; identity, truthiness, type, instance, unreachable and
  throws keep the semantics the current suite relies on, and every helper MUST
  fail when its condition does not hold. `node:assert`'s equality helpers are
  not drop-in replacements.
- Serve those helpers from a single local helper module and import them; do not
  redefine `assert` or `expect` inline.
- The global `Temporal` setup statement stays at module scope in the suites
  that declare it; do not move it into a setup file, hook or helper module.
- Any expectation that encodes a 4.3.3 behaviour replaced by the sections above
  MUST be updated to the new contract; every other expectation stays frozen.

# Constraints / Prohibitions

1. The package version and dependency graph MUST target devalue 6.0.0 and MUST NOT retain a dependency on devalue 4.3.3.
2. Every public contract in the "Public contract to preserve" section MUST remain importable and callable with unchanged signatures and semantics.
3. Every behavior in the "Behavior required by 6.0.0" section MUST be observable after the migration.
4. The migration MUST NOT remove, rename, or narrow any exported public entry point listed above.
5. Serialization and deserialization MUST NOT introduce regressions for any value type, reference pattern, or rejection case listed above.
6. The synchronous and asynchronous paths MUST agree on the payload format they produce and consume.
7. Do not introduce unrelated public API changes or change behavior outside the migration scope.

# Fixed Footer

Implement the requested change, run the relevant checks, and summarize the result.
