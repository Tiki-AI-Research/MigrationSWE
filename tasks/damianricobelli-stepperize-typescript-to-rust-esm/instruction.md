# Migrate @stepperize/core runtime helpers from TypeScript to Rust

## Background

`@stepperize/core` is the framework-agnostic runtime package for Stepperize. Its pure step helpers are currently implemented in TypeScript 6.0.3 in `packages/core/src/utils.ts` and re-exported from `packages/core/src/index.ts`. `@stepperize/react` imports these helpers synchronously from the workspace package, and non-React adapters use the package directly. The workspace uses pnpm 10.28.0, Node.js >=18, and Turbo.

## Goal

- Migrate the runtime implementation of `packages/core/src/utils.ts` from TypeScript 6.0.3 to Rust using the Rust 1.85.0 toolchain.
- Rust MUST provide the step-map construction, initial-state helpers, step parsing, validation dispatch, positional status derivation, and exhaustive matching behavior currently implemented in that file.
- The built `@stepperize/core` package MUST execute these migrated responsibilities from Rust while preserving its current ESM exports and TypeScript types.
- Keep `packages/react` hooks, providers, primitives, TSX rendering, DOM attributes, keyboard navigation, and React lifecycle behavior out of scope.
- Keep `apps/docs` TanStack Start, Fumadocs, Tailwind UI, MDX content, and shadcn registry JSON output out of scope.
- Do not add server, database, or persistence capabilities, and do not add the docs SSR build to the migration scope.

## Public contract to preserve

- `@stepperize/core` MUST remain an ESM package whose module namespace and exports are available synchronously after import.
- The package root MUST continue to export these functions with their current invocation contracts:
  - `createStepMap(steps)`
  - `getInitialStepIndex(steps, initial?)`
  - `parseStep(steps, value)`
  - `getInitialData(data?)`
  - `validateStep(steps, id, value)`
  - `getStepStatus(steps, currentIndex, id)`
  - `getStepStatuses(steps, currentIndex)`
  - `matchStep(steps, id, handlers)`
- `validateStep` MUST still return a Promise that resolves to the existing `ValidationResult` shape.
- `createStepMap` MUST return an object with `steps`, `ids`, `get`, `at`, `indexOf`, `has`, `first`, `last`, `next`, `prev`, and `neighbors`.
- All TypeScript types currently exported from `packages/core/src/types.ts` through the package root MUST remain importable with unchanged definitions, including `Step`, `StepMap`, `StepStatus`, `ValidationResult`, `StandardSchemaV1`, and `Get`.
- The package MUST remain importable as `@stepperize/core` with working type resolution for its root entry point.

## Behavior requirements

- `createStepMap(steps)` MUST expose the input steps in order and `ids` MUST contain their ids in the same order.
- `get(id)` MUST return the step whose `id` matches, or `undefined`.
- `at(index)` MUST return the step at `index`, or `undefined` when the index is out of range.
- `indexOf(id)` MUST return the matching step index, or `-1` when the id is absent.
- `has(id)` MUST return `true` only when a step with that id exists.
- `first()` and `last()` MUST return the first and last steps respectively, and MUST return `undefined` for an empty sequence.
- `next(id)` MUST return the following step, or `undefined` when `id` is the last step or is unknown.
- `prev(id)` MUST return the preceding step, or `undefined` when `id` is the first step or is unknown.
- `neighbors(id)` MUST return `{ prev, next }` with the adjacent steps, using `undefined` on each side that is at a boundary or when `id` is unknown.
- `getInitialStepIndex(steps, initial?)` MUST return the matching index, or `0` when `initial` is omitted or not found.
- `parseStep(steps, value)` MUST return `value` only when it equals a known step id; it MUST return `undefined` for an unknown string, `null`, `undefined`, or a number.
- `getInitialData(data?)` MUST return a shallow defensive copy; it MUST return an empty object when `data` is omitted, MUST preserve the top-level entries of the supplied object, and MUST NOT return the same object reference.
- `getStepStatus(steps, currentIndex, id)` MUST return `"active"` when the step index equals `currentIndex`, `"previous"` when the step index is non-negative and less than `currentIndex`, and `"upcoming"` otherwise.
- `getStepStatuses(steps, currentIndex)` MUST return a record with one status per step id, using the same positional rules.
- `validateStep(steps, id, value)` MUST throw an error whose message contains `not found` when no step has `id`.
- For a step without a schema, `validateStep` MUST resolve `{ success: true, data: value }` with the value passed through unchanged.
- For a step with a schema, `validateStep` MUST await `schema["~standard"].validate(value)` and resolve `{ success: true, data: result.value }` on success, or `{ success: false, issues: result.issues }` when the validation result provides issues.
- `matchStep(steps, id, handlers)` MUST invoke the handler for `id`, pass it the matching step, and return its result.
- `matchStep` MUST throw an error whose message contains `not found` when no step has `id`, and an error whose message contains `No match handler` when the handler is missing.
- When `matchStep` fails, it MUST NOT invoke any other step handler.

## Constraints

1. The migration MUST NOT remove a required public capability or change a public contract outside the stated goal.
2. The migrated runtime logic MUST be implemented in Rust and built with the Rust 1.85.0 toolchain. Production code in the migration scope MUST NOT implement that logic in TypeScript.
3. Production code MUST NOT retain or invoke the previous TypeScript implementation of the migrated helpers through subprocesses, RPC, dynamic loading, or equivalent indirection.
4. The ESM facade MAY marshal arguments and results and wire the Rust implementation into the package, but it MUST NOT reimplement step lookup, validation dispatch, status derivation, or matching logic.
5. Changes MUST be limited to `packages/core` (including new Rust crate sources, build and binding configuration, and the ESM facade) and workspace-level dependency, lockfile, or build configuration updates required to build the target.
6. `packages/core/src/types.ts` is out of migration scope, and its exported type contracts MUST remain unchanged. TypeScript MAY remain in use for that type layer, the package facade, and out-of-scope packages.
7. `packages/react` hooks, providers, primitives, TSX rendering, DOM attributes, keyboard navigation, and React lifecycle behavior MUST NOT be modified.
8. `apps/docs` content and behavior, including the shadcn registry JSON output, MUST NOT be modified.
9. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
10. You MAY adapt the existing core test bootstrap or import paths only as needed to load the migrated implementation, provided every behavioral assertion remains present and unweakened.
11. The `packages/core` `build` and `test` scripts MUST remain available under those names and MUST work with the migrated package.
12. Work MUST occur in the existing repository checkout at `/workspace/repo`. Do not reset it to `main` or replace the supplied starting commit with another baseline.
13. All required source and build files MUST remain under `/workspace/repo`, including newly added files. The evaluator collects the repository diff from the supplied starting commit, so changes outside the repository are not collected.

## Validation

From `/workspace/repo`, run:

```bash
pnpm --filter @stepperize/core build
pnpm --filter @stepperize/core test
pnpm --filter @stepperize/react test
```

The core build command verifies that the migrated package builds. The core test command verifies public core behavior. The React test command verifies downstream consumption through `@stepperize/react`. These public checks do not by themselves prove that every requirement is satisfied.
