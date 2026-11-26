# Migrate eslint-plugin-jest from ESLint 9 to ESLint 10

## Background

`eslint-plugin-jest` currently runs on ESLint 9 (resolved to 9.39.5). It exports six shared configurations, Jest globals, runtime settings, and the Jest rule table. The `all`, `recommended`, and `style` configurations still use the eslintrc-only `env` and `plugins: ['jest']` shape. The target environment uses ESLint 10.5.0, which supports flat configuration only.

## Goal

- Upgrade the plugin and repository from ESLint 9.39.5 to ESLint 10.5.0.
- The installed ESLint dependency and `yarn.lock` MUST resolve to ESLint 10.5.0.
- ESLint 10.5.0 MUST load the plugin and execute its configurations and rules through flat config.
- Keep the existing Jest rule inventory, rule behavior, and non-ESLint features unchanged.
- Out of scope: changes to rule semantics, rule IDs, message IDs, schemas, fixer or suggestion output, and unrelated frontend, documentation-site, or dependency refactors.

## Public contract to preserve

- `require('eslint-plugin-jest')` MUST still return a plugin object with `meta.name`, `meta.version`, `configs`, `environments.globals`, and `rules`.
- `configs` MUST continue to expose these six names: `all`, `recommended`, `style`, `flat/all`, `flat/recommended`, and `flat/style`.
- Every exported config MUST be a flat config object with `plugins.jest`, `languageOptions.globals`, and `rules`.
- The `all`, `recommended`, and `style` entries MUST NOT keep the eslintrc-only `env` or `plugins: ['jest']` shape.
- Each same-named configuration MUST keep the rule set it has in the source version.
- `all` MUST continue to omit deprecated rules.
- `recommended` and `style` MUST keep the source version's fixed rule tables.
- `plugin.configs.recommended` and `plugin.configs['flat/recommended']` MUST enable the same rules and produce the same diagnostics.
- `settings.jest.version`, `settings.jest.globalAliases`, and `settings.jest.globalPackage` MUST keep their user-facing keys and semantics.
- `index.d.ts` MUST keep exporting the plugin object, rule table, and six configurations.
- Configuration types MUST use ESLint 10 flat config types and MUST NOT reference removed legacy config types.
- `prefer-importing-jest-globals` MUST keep its message, import/require choice, and automatic fix results.

## Behavior requirements

- When loaded with ESLint 10.5.0 from a flat config array, the plugin MUST not produce legacy-config or `env` errors.
- Flat configs MUST inject Jest globals so test files need not redeclare `describe`, `it`, `expect`, or the other provided globals.
- `all` and `flat/all` MUST contain exactly the non-deprecated rules.
- `prefer-importing-jest-globals` MUST produce an `import` fix when `languageOptions.sourceType` is `module`.
- It MUST produce a `require` fix when `languageOptions.sourceType` is `script`.
- Existing merging of `@jest/globals` imports or requires and `'use strict'` handling MUST keep the same fix output.
- `settings.jest.version` and automatic detection MUST still control `no-deprecated-functions` deprecation mapping and fixes.
- Automatic detection MUST still start from the nearest `node_modules` to the linted file and cache the result.
- Type-aware rules MUST still declare `meta.docs.requiresTypeChecking` where applicable.
- `no-unnecessary-assertion` MUST still report `noStrictNullCheck` when `strictNullChecks` is disabled.
- The repository's declared Node.js engine range (`engines.node` in `package.json`) MUST be a subset of the engine range declared by the installed ESLint 10.5.0 package. The repository MUST NOT advertise Node.js versions that the installed ESLint 10.5.0 does not support.
- The removed ESLint 9 bundled type-declaration package (`@types/eslint`) MUST NOT remain a declared dependency; the plugin's configuration types MUST come from the installed ESLint 10.5.0 typings.

## Constraints

1. The migration MUST NOT change any rule ID, rule semantics, `messageId`, schema, fixer output, or suggestion output.
2. The migration MUST NOT add or remove Jest rules or change the `recommended`, `style`, or `all` rule sets.
3. The migration MUST NOT change `settings.jest.version`, `globalAliases`, or `globalPackage` parsing or Jest auto-detection behavior.
4. Production code in scope MUST NOT use ESLint 9-only APIs or types that ESLint 10 removed, including legacy config shapes and `Linter.LegacyConfig`.
5. Production code in scope MUST NOT load or delegate configuration building or rule execution to an ESLint 9 runtime or another framework.
6. `eslint.config.js` MUST be a valid ESLint 10 flat config and MUST NOT reference removed ESLint 9-only types or APIs.
7. The ambient `eslint-plugin-eslint-plugin` declaration in `types.d.ts` MUST remain compatible with ESLint 10 types.
8. Changes MUST be limited to `package.json`, `yarn.lock`, `eslint.config.js`, `index.d.ts`, `types.d.ts`, `src/index.ts`, `src/rules/prefer-importing-jest-globals.ts`, `src/rules/__tests__/prefer-importing-jest-globals.test.ts`, `src/rules/__tests__/test-utils.ts`, `src/__tests__/rules.test.ts`, `src/__tests__/__snapshots__/rules.test.ts.snap`, `README.md`, and `.github/workflows/nodejs.yml`.
9. Rule implementations, rule documentation under `docs/rules/`, and rule tests are protected unless they are listed in Constraint 8. Do not modify them.
10. You MAY adapt the in-scope tests, snapshots, and test utilities to flat-config-only behavior. You MUST NOT remove behavioral assertions, weaken comparisons, skip checks, or hardcode evaluation inputs.
11. README usage for the exported configurations MUST describe ESLint 10 flat config only. It MUST NOT present `env` or `plugins: ['jest']` as supported for this plugin.
12. The ESLint test matrix in `.github/workflows/nodejs.yml` MUST include ESLint 10.5.0 as the upgrade target.
13. Keep the `yarn@4.18.0` package manager, the existing Babel build, and the separate `yarn typecheck` flow. Package and engine changes MUST stay within ESLint 10.5.0 compatibility: the declared `engines.node` range MUST be narrowed to a subset of the installed ESLint 10.5.0 engine range, and the removed ESLint 9 `@types/eslint` dependency MUST be dropped.
14. Do not lower the existing 100% coverage thresholds.
15. Work in `/workspace/repo` from the provided starting point. Do not substitute `main` or another branch.
16. Your changes are collected as a patch relative to the provided starting commit. Keep all required changes in the repository.

## Validation

From `/workspace/repo`, run:

```bash
yarn test
yarn typecheck
yarn lint
yarn prettier:check
```

`yarn test` runs the Jest test and lint projects. `yarn typecheck` checks the TypeScript sources and declarations against the installed ESLint 10 types. `yarn lint` checks the repository with its own ESLint configuration. `yarn prettier:check` checks formatting.

All four public commands MUST pass after the migration. These checks cover the visible test, type, lint, and formatting expectations, but they do not by themselves prove every requirement in this instruction.
