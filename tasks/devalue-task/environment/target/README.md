# Target dependency and toolchain contract

This directory is the authoritative dependency contract for the devalue 6.0.0
migration. It is available at `/opt/migration/target/` inside the agent
workspace image. The image's pnpm store is pre-populated with the exact target
graph, so `pnpm install --offline --frozen-lockfile` works without network
access.

The `package.json` and `pnpm-lock.yaml` here must stay byte-identical to
`tests/environment/dependencies/`; that directory is the verifier's copy of the
same contract.

## Required `devDependencies`

`package.json` in this directory contains the exact target set:

- `@changesets/cli` `^2.29.6`
- `@js-temporal/polyfill` `^0.5.1`
- `@types/node` `^24.12.0`
- `dts-buddy` `^0.8.3`
- `prettier` `^3.2.4`
- `publint` `^0.3.12`
- `typescript` `^6.0.3`
- `vite` `8.3.0`
- `vitest` `5.0.1`

The migrated package must not retain `uvu` in its dependency graph. The
workspace starts with a mixed legacy/target dependency install so that the
baseline remains runnable; the migration itself is complete only when the
package manifest and lockfile describe the target set above.

## Required lockfile

`pnpm-lock.yaml` in this directory is the frozen target lockfile. Replace the
workspace lockfile with this file (or reproduce it byte-for-byte from the same
dependency set). The evaluator removes `node_modules` and performs an offline,
frozen install against this exact graph.

## Required package scripts

Keep the package's existing public scripts and ensure these commands work:

```sh
pnpm build
pnpm test
pnpm run test:types
```

The target package scripts are:

```json
{
  "build": "dts-buddy",
  "test": "vitest run --project unit",
  "test:unit": "vitest run --project unit",
  "test:watch": "vitest --project unit",
  "test:types": "tsc -p . --noEmit"
}
```

Add the migration configuration those commands need: a `vitest.config.js` with
a `unit` project covering `src/**/*.test.js` and `test/**/*.js`, and a
`tsconfig.json` usable by `tsc -p . --noEmit`. The evaluator also runs its own
Vitest project against the migrated sources.

## Acceptance commands

Run these from the workspace root before finishing:

```sh
pnpm install --offline --frozen-lockfile
pnpm build
pnpm test
pnpm run test:types
```

The pinned `pnpm` store already contains the target packages. Do not reach the
network; fix the manifest/lockfile instead if the frozen install cannot
resolve.
