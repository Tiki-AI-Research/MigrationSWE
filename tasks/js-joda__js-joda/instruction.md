# Upgrade @js-joda/core from 6.1.0 to 6.3.2

## Background

`@js-joda/core` is an immutable date and time library for JavaScript, ported from the ThreeTen-Backport reference implementation of Java's `java.time` (JSR-310). This repository is the js-joda monorepo. It uses Lerna with independent package versions and contains `packages/core`, `packages/timezone`, `packages/locale`, `packages/extra`, `packages/examples`, and generated prebuilt locale packages under `packages/locale/packages/`.

`packages/core/package.json` currently declares version `6.1.0`. The sibling packages and the generated locale packages depend on core through peer and development dependency ranges. The target release is `@js-joda/core` `6.3.2`. The working tree already contains the `6.2`/`6.3` source changes; only the version declarations and package metadata lag the target release, so this task aligns metadata and does not port source.

## Goal

- Upgrade `@js-joda/core` from `6.1.0` to `6.3.2` inside this monorepo.
- `packages/core/package.json` MUST declare version `6.3.2`.
- Every in-repo declaration that pins or constrains the core version MUST be aligned so the workspace resolves and installs `@js-joda/core` `6.3.2`.
- The target-release behavior for the fixes documented in the repository root `CHANGELOG.md` for the `6.2`/`6.3` line MUST be preserved: `Instant.parse` accepting an offset, `Duration.toString` for negative durations on a whole-minute boundary, truncation of pre-epoch `Instant` values toward negative infinity, zone-id parsing for ids that share a prefix with a fixed id, and initialization of parsed excess days to `Period.ZERO`. Those changes are already merged into the working-tree source, so the migration MUST NOT add, remove, or modify package source modules (`packages/*/src/**`).
- Out of scope: cross-language or cross-framework replacement, public API redesign, and changes to the packaged IANA time zone or CLDR data.

## Public contract to preserve

- `@js-joda/core` MUST keep `main=dist/js-joda.js`, `module=dist/js-joda.esm.js`, and `typings=typings/js-joda.d.ts`.
- `@js-joda/core` MUST keep exporting `ZonedDateTime`, `LocalDate`, `LocalDateTime`, `Duration`, `ChronoUnit`, `ZoneOffset`, and the other existing public exports.
- `@js-joda/timezone`, `@js-joda/locale`, and `@js-joda/extra` MUST keep the `main`, `module`, and `typings` entries declared in their `package.json` files.
- `@js-joda/locale` MUST keep exporting `Locale`, `WeekFields`, and `registerLocaleData`.
- The prebuilt locale packages MUST keep declaring `@js-joda/core`, `@js-joda/locale`, and the optional `@js-joda/timezone` as `peerDependencies`.
- After the upgrade, the version fields and peer dependency ranges across the package manifests MUST stay mutually consistent. `@js-joda/core` `6.3.2` MUST satisfy the existing ranges, including `>=6.0.0` and `>=5.7.0`, so the dependency graph remains resolvable and installable.
- Importing `@js-joda/timezone` MUST keep registering the IANA time zone rules as a side effect.
- All public date-time types MUST stay immutable. Their methods MUST NOT mutate the receiver. Methods returning date-time values MAY reuse an existing immutable instance representing the correct result, including the receiver when the result is unchanged; allocating a fresh instance on every call is not required.

## Behavior requirements

- After importing `@js-joda/timezone`, `ZonedDateTime.now().toString()` MUST return a parseable string that includes a time zone offset.
- For the same inputs, `Interval` from `@js-joda/extra` MUST return the same `toString()` and `contains()` results as before the upgrade.
- `@js-joda/locale` formatting MUST return the expected localized text for each supported locale, including `en-US` and `de-DE`.
- `packages/core` Node tests (Mocha), browser tests (Karma), TypeScript definition tests, and lint MUST run and pass after the upgrade.
- `build-dist` and `build-locale-dist` MUST keep producing the ESM, CJS, UMD, and IIFE artifacts and the prebuilt locale packages.
- The documented target-release behaviors listed in the Goal MUST hold, and the upgrade MUST NOT regress the other existing public parse, format, and arithmetic results.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. The migration MUST NOT rename, delete, or alter any public export or package entry point (`main`, `module`, `typings`).
3. The migration MUST NOT change the parse, format, or arithmetic result semantics of public types beyond the documented target-release behavior. Where a pre-upgrade expectation conflicts with a documented target-release fix, the target-release behavior is authoritative.
4. Production code within the migration scope MUST NOT delegate the upgraded behavior to an external js-joda runtime through subprocesses, RPC, FFI, or dynamic loading.
5. The migration MUST be a real version upgrade. Renaming or relabeling without resolving `@js-joda/core` `6.3.2` and without meeting the behavior requirements in this document is not sufficient.
6. Changes MUST be limited to the monorepo package manifests and lockfiles, package entry files, typings, locale and prebuilt locale package metadata, and the Lerna configuration needed to complete the version upgrade.
7. The migration MUST NOT modify the packaged IANA time zone data files under `packages/timezone/data/packed/**`.
8. The migration MUST NOT change the semantics of the CLDR or locale data or the prebuilt language package data.
9. The migration MUST NOT introduce frontend or browser UI capability, and MUST NOT change the library's non-UI positioning.
10. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode inputs to fake compliance.
11. All work MUST happen inside the repository at `/workspace/repo`. The evaluation collects the changes from the repository working tree.
12. The environment has no external network access. Dependencies, including `@js-joda/core` `6.3.2`, MUST be resolved from the preloaded local npm cache rather than the public registry.

## Validation

From `/workspace/repo`, the following public commands are available. Run them after completing the upgrade.

- `npx lerna run --stream build-dist` and `npx lerna run --stream build-locale-dist` rebuild the distribution artifacts and prebuilt locale packages.
- In `packages/core`, run `npm test`, `npm run test-ts-definitions`, `npm run test-browser`, `npm run lint`, and `npm run test-ci`.
- In `packages/examples`, run `npm test`, which exercises the CommonJS, ES module, and TypeScript samples.

These checks cover the public entry points, package resolution, and the observable behaviors described above. They do not prove every requirement in this document.

The migration baseline of this task is base commit ce4665b0930da4d7e0bf0b18b97799206c960c9c.
