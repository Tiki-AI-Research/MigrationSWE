# Migrate configuration validation from Superstruct to Zod

## Background

`mushroom-cards` is a TypeScript plugin that runs as a Home Assistant Lovelace frontend resource in the browser. Card, chip, and badge configuration schemas are defined and validated with Superstruct `2.0.2`. The plugin also derives the editor's localized configuration messages from Superstruct failure details in `src/ha/common/structs/handle-errors.ts`. The repository has no test files and no `test` script; the plugin bundle is produced by `npm run build`.

## Goal

- Migrate the `mushroom-cards` schema definitions in these modules from Superstruct `2.0.2` to Zod `3.23.8`:
  - `src/shared/config/entity-config.ts`
  - `src/shared/config/appearance-config.ts`
  - `src/shared/config/actions-config.ts`
  - `src/shared/config/lovelace-card-config.ts`
  - `src/shared/config/lovelace-badge-config.ts`
  - `src/utils/layout.ts`
  - `src/ha/common/structs/handle-errors.ts`
  - `src/ha/panels/lovelace/editor/structs/action-struct.ts`
- Zod MUST perform the configuration validation and the localized validation-error reporting that these modules provide today.
- The exported schemas MUST keep their current accept/reject semantics for the same configuration input.
- Modules that compose or call these exported schemas (card, chip, and badge config and editor code) MUST be adapted only as needed to keep validating the same configuration input with Zod.
- Out of scope: Lit templates, CSS, Web Component rendering, `mushroom-*` element tag names, and card, chip, and badge YAML option names. Superstruct may remain installed and used outside the migrated schema surface.

## Public contract to preserve

- `entitySharedConfigStruct`, `entityNameStruct`, `appearanceSharedConfigStruct`, `layoutStruct`, and `actionConfigStruct` MUST keep equivalent accept/reject semantics for the same configuration input.
- The card and badge config structs that compose these schemas MUST keep equivalent accept/reject semantics for the same configuration input.
- The exported TypeScript config types derived from `Infer` / `z.infer` (`EntitySharedConfig`, `AppearanceSharedConfig`, and the other config types) MUST keep their current public shape.
- `handleStructError(hass, err)` MUST keep returning `{ warnings: string[]; errors?: string[] }`.
- Validation messages MUST keep using the `ui.errors.config.*` localization keys and the offending dotted key paths.

## Behavior that must stay unchanged

- If a configuration object satisfies its schema, validation MUST succeed.
- If a configuration object is missing a key that its schema requires, validation MUST produce an error entry localized with `ui.errors.config.key_missing` for that key's dotted path.
- If a configuration object contains a key that its schema does not expect, validation MUST produce a warning localized with `ui.errors.config.key_not_expected` for that key's dotted path.
- If a configuration object contains a value of the wrong type, validation MUST produce a warning localized with `ui.errors.config.key_wrong_type` that includes the offending value.
- When a value fails several union alternatives, the union branches MUST continue to be skipped; the messages MUST NOT add one entry per alternative.
- If `handleStructError` receives an error that is not a validation error from the schema framework, it MUST return `{ warnings: [err.message], errors: undefined }`.

## constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code in the migrated schema modules MUST NOT import or call Superstruct for the migrated validation responsibilities.
3. The migrated responsibilities MUST NOT be delegated back to the Superstruct runtime through subprocesses, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to the migrated schema modules, the modules that compose or call them, and the dependency configuration needed to add Zod.
5. Superstruct may remain installed and used by modules outside the migrated schema surface.
6. The exported schema names, exported config type names, and `ui.errors.config.*` localization keys MUST remain unchanged.
7. You MUST NOT change `mushroom-*` custom element tag names or card, chip, and badge YAML option names.
8. You MUST NOT change Lit templates, CSS, or Web Component rendering code, and you MUST NOT alter the default values applied to optional config fields.
9. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
10. Work in the repository at `/workspace/repo` and keep all changes in that working tree, because the submission is collected as a repository diff against the starting checkout.
11. From `/workspace/repo`, run `npm run build`. This verifies that the Home Assistant frontend bundle builds with the migrated schemas; it does not by itself prove the full configuration and error-message contract.

IMPORTANT: Create a new branch from the task's base commit d8a90ffff1b217fbbf9cb8210ee672be99596231 and commit all task changes, including new files, when you are done.
