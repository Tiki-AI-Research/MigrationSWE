# webvr-polyfill: Build and Test Toolchain Migration

## Background

This package provides a browser-oriented WebVR polyfill. Its public API and
published artifacts are consumed both by CommonJS applications and by
browser script-tag applications. The existing build and test toolchain is
obsolete and must be replaced without changing the polyfill's observable
behavior.

## Goal

Modernize the build and test toolchain for Node.js 20 and npm 10 using Vite
5.4 and Vitest 2.1, while preserving the package's public behavior,
distribution shape, and compatibility contracts.

Use the target toolchain's native configuration format. The migration is
expected to replace the existing configuration rather than introduce a second,
parallel toolchain or unrelated CI and test infrastructure.

The migration must produce readable and minified UMD/ESM artifacts plus a
genuine CommonJS entry. Preserve the existing license banner as the first
content of both UMD artifacts and of the CommonJS entry, and preserve the
standalone license file. The package must remain usable from CommonJS when a
process supplies only `window`; in that situation the missing `document` and
`navigator` objects must be derived from `window` before initialization, for
the UMD and CommonJS entries alike.

## Required Target Configuration (exact contract)

The target toolchain MUST use its native configuration files and the following
exact package metadata. Equivalent commands, alternative flags, or different
file names do not satisfy the contract.

- Add `vite.config.mjs` and `vitest.config.mjs` (the `.js` variants are also
  accepted), and add the build drivers `scripts/build-vite.mjs` and
  `scripts/watch-vite.mjs`.
- Development dependencies MUST be exactly `jsdom: ^25.0.1`,
  `localStorage: ^1.0.3`, `vite: ^5.4.10`, and `vitest: ^2.1.3`; the runtime
  dependency MUST remain exactly `cardboard-vr-display: ^1.0.19`.
- The package scripts MUST have these exact values:

  ```json
  {
    "build": "npm test && node scripts/build-vite.mjs",
    "build-min": "node scripts/build-vite.mjs --minify",
    "build-all": "npm run build",
    "watch": "node scripts/watch-vite.mjs",
    "test": "vitest run --config vitest.config.mjs"
  }
  ```

- In addition to the scripts above, declare the migrated runtime in package
  metadata: `"engines": { "node": ">=20", "npm": ">=10" }` and
  `"packageManager": "npm@10"`. Add a `"prepublishOnly": "npm run build-all"`
  script so a publish always uses freshly built artifacts. Extra scripts are
  allowed only for this publication guard.
- Remove the obsolete legacy files `.babelrc`, `rollup.config.js`,
  `rollup.config.min.js`, `test/.babelrc`, and the Mocha bootstrap
  `test/init.js`. The migrated Vitest suite must use its configured `jsdom`
  environment instead of the removed bootstrap. No active package script may
  reference Rollup, Babel, or Mocha.
- `package-lock.json` MUST be a complete lockfile v3 consistent with the
  declared dependencies. Every resolved registry package entry MUST contain
  both `resolved` and `integrity`, so `npm ci --offline` can install from the
  lockfile without consulting the registry.
- `vitest.config.mjs` MUST contain the following exact values:
  `environment: 'jsdom'`, `include: ['test/**/*.test.js']`, and
  `fileParallelism: false`.
- `test/utils.js` MUST import the polyfill from `../src/webvr-polyfill.js`, and
  `test/webvr-polyfill.test.js` MUST import its test helpers from `vitest`.
- CI MUST declare Node.js 20: `.travis.yml` with `node_js:` followed by `- "20"`,
  or `.github/workflows/ci.yml` with `node-version: "20"`. The same CI
  configuration MUST run the migrated workflow explicitly: install with
  `npm ci`, then run `npm test`, then run `npm run build-all`, then exercise
  the publication boundary with `npm pack --dry-run --json` (in Travis
  `install`/`script` entries or GitHub Actions `run` steps). It MUST NOT cache
  `node_modules`; cache the npm download cache (`~/.npm`) if caching is used.
- The full `npm run build` / `npm run build-all` output MUST leave a standalone
  `build/licenses.txt` copied from the root `licenses.txt`, alongside
  `build/webvr-polyfill.js`, `build/webvr-polyfill.min.js`,
  `build/webvr-polyfill.mjs`, `build/webvr-polyfill.min.mjs`,
  `build/webvr-polyfill.cjs`, and the source maps described below. Each
  source map MUST be valid JSON. Its `sources` MUST describe the migrated
  module graph rooted at the `src/webvr-polyfill.js` entry module, and its
  `sourcesContent` MUST contain the exact UTF-8 text of every listed source
  file. The legacy CommonJS bootstrap `src/index.js` MUST NOT appear in
  `sources` or `sourcesContent`; that bridge belongs in the build
  configuration (the generated `intro`), not in the mapped source graph. Every
  entry in `sources` MUST be package-relative (for example `src/config.js` or
  `node_modules/cardboard-vr-display/...`), never an absolute path and never
  prefixed with `..`. Its `mappings` MUST account for the prepended license
  banner so that generated lines still map to the correct source lines, and
  MUST remain correct when the same token occurs in multiple modules. These
  requirements apply to the CommonJS map as well.
- A full `npm run build` / `npm run build-all` MUST publish a clean-slate
  generation: artifacts from earlier builds MUST NOT survive into it. This
  clean-slate requirement applies only to the full build; `build-min` and
  `watch` remain incremental and MUST NOT discard the readable generation just
  to rebuild the files they publish, as described below. "Clean slate" is
  about the published result, not about clearing `build/` first: see the
  atomic publication requirement below.
- `npm run build-min` MUST be a self-contained minified build. From a tree
  with no `build/` directory it MUST succeed and produce exactly the minified
  distribution: `build/webvr-polyfill.min.js`,
  `build/webvr-polyfill.min.mjs`, their two source maps, and
  `build/licenses.txt`. It MUST NOT require a previous full build, and it MUST
  NOT emit the readable artifacts or the CommonJS entry from that clean tree.
  When readable artifacts from the full build are present, it MUST leave
  `build/webvr-polyfill.js`, `build/webvr-polyfill.mjs`,
  `build/webvr-polyfill.cjs`, and `build/licenses.txt` byte-for-byte
  unchanged, and MUST only add or replace the minified artifacts and their
  source maps. It MUST NOT clear `build/` in that case.
- A build result is published atomically as one generation, and that atomicity
  is observable: while a build runs, anything that lists `build/` MUST only
  ever see one complete published generation — either the previous one,
  byte-for-byte unchanged, or the new one, byte-for-byte complete. A reader
  MUST NOT be able to observe a cleared, partially written, partially updated,
  or mixed-generation `build/` at any point during the build. A brief moment
  during the final directory swap in which `build/` itself does not exist is
  acceptable; an ordinary two-rename swap is enough, there is no need for an
  atomic directory exchange primitive. Rebuilding the same source twice MUST
  produce byte-for-byte identical artifacts. A build invocation that fails
  MUST exit nonzero and MUST leave the previously published generation
  byte-for-byte intact: no partially written artifact, no half-updated source
  map, and no readable/minified mix from different revisions. A build MUST also
  publish from a single input snapshot: the complete generation MUST be derived
  from one state of `src/`, `licenses.txt`, and the package metadata the build
  reads. If one of those inputs changes while the build is running, the build
  MUST either publish a complete generation from that same state or abort
  nonzero and leave the previous generation byte-for-byte intact; it MUST NOT
  publish a set that mixes files read before and after the change. Staging or
  temporary directories used while building MUST be removed; after a
  successful build, `build/` MUST contain exactly the published files and the
  repository root MUST NOT retain `build.*`, `.build-*`, or similar staging
  directories. A build or watch process killed mid-run (including by
  `SIGKILL`) MUST leave the previously published generation intact, and the
  next build or watch rebuild MUST remove the staging directories the killed
  process left behind before it publishes again.
- Build logic MUST stay inside the Node toolchain. The build and watch drivers
  MUST NOT spawn an external interpreter or shell to perform any build step —
  for example `python3 -c ...`, `sh -c ...`, or `perl -e ...` — and MUST NOT
  shell out to a retained or alternative build implementation. The only
  permitted subprocess use is the declared driver invoking the installed Vite
  CLI, or the watch driver invoking the local build driver.
- The published tree MUST stay readable to other user accounts on the same
  machine. Every file and directory under `build/` MUST be readable by all
  users; a staging directory created with owner-only permissions (for example
  `fs.mkdtempSync`, which creates `0700`) MUST NOT be swapped into place as
  `build/`, because the published package then cannot be read by an evaluator
  or CI user that is not the build owner.
- The minified artifacts MUST be reproducible across entry points: for the
  same source tree and installed dependencies, the four minified outputs
  produced by `npm run build-min` alone MUST be byte-for-byte identical to the
  ones produced by the full `npm run build` / `npm run build-all`. Both entry
  points MUST share one build configuration; a duplicated or divergent minify
  pipeline does not satisfy the contract.
- The full build MUST be reproducible across checkout paths: building the same
  source tree and installed dependencies from two different absolute
  directories MUST produce byte-for-byte identical artifacts. No generated
  artifact may embed the checkout path, a timestamp, or host-specific data.
- Build the complete debug/minified × UMD/ESM matrix, not just the common
  subset: `build/webvr-polyfill.js`, `build/webvr-polyfill.min.js`,
  `build/webvr-polyfill.mjs`, and `build/webvr-polyfill.min.mjs`, with a source
  map beside each. The ES modules' default export MUST be the same
  constructor. The full build MUST additionally emit the genuine CommonJS
  entry `build/webvr-polyfill.cjs` with `build/webvr-polyfill.cjs.map` beside
  it. The existing `package.json` `"main"` field MUST stay exactly
  `"build/webvr-polyfill.js"`; the new CommonJS artifact MUST NOT be published
  as `"main"`, and the package MUST NOT become ESM-only. Add exactly
  `"module": "build/webvr-polyfill.mjs"` and the exact metadata
  `"exports": { ".": { "import": "./build/webvr-polyfill.mjs", "require": "./build/webvr-polyfill.cjs" } }`
  to `package.json`.
- The `.cjs` artifact MUST be real CommonJS, not a copy or rename of the UMD
  wrapper: it MUST expose the constructor through `module.exports`, MUST NOT
  contain a UMD `define` / `define.amd` branch, and MUST NOT be byte-identical
  to `build/webvr-polyfill.js`. It MUST load through `require()` in a Node
  process that provides only `window`, applying the same
  `document`/`navigator` bootstrap, and MUST expose the same constructor,
  `.version`, `VRDisplay`, and `VRFrameData` as the UMD entry.
- Both `.min` artifacts MUST be genuinely minified, not merely readable source
  renamed with a `.min` suffix. After the exact leading license banner, if one
  is present, ignore blank lines and comment-only lines; every remaining code
  line MUST have no leading spaces or tabs. Multiline string contents are not
  code lines. This applies to every build mode, including watch output. In
  particular, the minified ESM MUST NOT retain a formatted multi-line
  `export { ... }` block or indented function bodies.
- All five published entries -- readable UMD, minified UMD, readable ESM,
  minified ESM, and CommonJS -- MUST expose identical observable behavior for
  the same user agent, native-API availability, and option set. At minimum
  `enabled`, `hasNative`, `.version`, and the count and names returned by
  `getVRDisplays()` MUST match across every entry.
- The `watch` script MUST run the dedicated watcher
  `scripts/watch-vite.mjs` and MUST be a real, incremental watch build, not a
  one-shot build. From a tree with no `build/` directory, its initial build
  MUST produce the complete matrix: `build/webvr-polyfill.js`,
  `build/webvr-polyfill.min.js`, `build/webvr-polyfill.mjs`,
  `build/webvr-polyfill.min.mjs`, the four source maps beside them, and
  `build/licenses.txt`. After a source file changes, the watcher MUST rebuild
  all four JavaScript artifacts and all four source maps from the current
  source without being restarted. The watch build MUST be incremental: it MUST
  NOT remove `build/` or unrelated files already present there, and it MUST
  NOT substitute stale artifacts left by an earlier full build. The minified
  watch artifacts MUST satisfy the same minification and source-map contracts
  as the full build. Changes to files outside the build graph (for example an
  unrelated `README.md`) MUST NOT rewrite any build artifact. In particular,
  `src/` is not itself the build graph: a new file under `src/` that no mapped
  source imports MUST NOT trigger a rebuild and MUST NOT change the content or
  the modification time of any published artifact. Rapid successive source
  edits MUST converge on the last edit; no
  artifact may remain at an intermediate source state. When the watcher driver
  receives `SIGTERM`, it MUST stop within 20 seconds and MUST NOT leave any
  descendant process running. The watcher MUST follow the module graph rather
  than a fixed file list: adding a module and importing it from a mapped source
  MUST include that module in the next rebuild of the artifacts and source
  maps, and removing the import MUST drop it again. A source file with a syntax
  error MUST NOT terminate the watcher or leave truncated or partially written
  artifacts; after the file is corrected, the next rebuild MUST use the
  corrected source without restarting the watcher. Source map `sourcesContent`
  MUST reflect the current source text after a watch rebuild. The watcher owns
  the whole published set: if `build/` is deleted while the watcher is running,
  the next source change MUST republish every published file, including the
  standalone `build/licenses.txt` and the CommonJS entry, and the republished
  set MUST satisfy the same banner, source-map, and content contracts as a
  fresh full build. The watcher MUST keep publishing after such a repair.
  Watch rebuilds MUST publish under the same atomic-generation rule as a full
  build: while a rebuild runs, anything that lists `build/` MUST only see the
  previous complete generation or the new complete generation, never a
  partially written, partially updated, or mixed-generation set. The watcher
  MUST also watch the license source `licenses.txt`: changing it MUST trigger a
  rebuild that republishes the whole published set from the current sources,
  including all four JavaScript artifacts, all source maps, the CommonJS entry,
  and `build/licenses.txt`.
- Watcher startup and recovery are part of the contract. Starting `npm run
  watch` while a mapped source is already invalid MUST NOT terminate the
  watcher or disturb the last complete generation; fixing the source MUST
  publish a complete new generation without restarting the watcher. The same
  applies when an imported module or `licenses.txt` is temporarily missing:
  the watcher MUST keep watching the expected path and MUST republish the whole
  published set once the module is created or the file is restored. A module
  may be imported before it exists; both the import-first and create-first
  orders MUST recover. Source files may be replaced atomically
  (write-temp-then-rename); the watcher MUST continue to follow the path after
  replacement.
- Watcher directory recovery is part of the contract. Deleting an entire
  subdirectory that holds a mapped module MUST NOT terminate the watcher or
  disturb the last complete generation; recreating the subdirectory together
  with the module MUST republish a complete generation without restarting the
  watcher. The same applies to the entry module itself: when
  `src/webvr-polyfill.js` is already missing while the watcher starts, the
  watcher MUST stay alive and keep the last complete generation, and restoring
  the entry MUST resume publication without a restart.
- The full build and the watcher share one output directory and MUST be safe to
  run at the same time. A full build (`build-all`, which runs
  `scripts/build-vite.mjs`) MUST be safe to run while `npm run watch` is active
  and rebuilding, and two concurrent full builds MUST be safe to run together.
  Because both paths publish through `scripts/build-vite.mjs`, that driver MUST
  stage each generation in a directory unique to its process and exchange it
  into `build/` under a short-lived lock, so no two writers share, overwrite, or
  delete each other's staging area. While any such build runs, a reader that
  lists `build/` MUST still only ever observe a complete previous generation or
  a complete new generation; it MUST NOT observe a `build/` directory that
  exists but is missing part of the published matrix. When the builds finish,
  no staging or temporary directory (any `.build-*` or `build.*` entry) may
  remain, and the watcher MUST keep publishing.
- Watcher quiescence is part of the contract. A change that is not a build
  input MUST NOT republish anything and MUST NOT create a staging directory: a
  file under `src/` that no mapped source imports, a module whose import was
  removed from the graph (including later edits to or deletion of that orphan
  module), and a file inside `build/` that the watcher does not own MUST each
  leave both the content and the modification time of every published artifact
  unchanged. A genuine in-graph edit or a change to a required file MUST still
  publish.
- The published package MUST contain only `LICENSE`, `package.json`,
  `build/webvr-polyfill.js`, `build/webvr-polyfill.js.map`,
  `build/webvr-polyfill.min.js`, `build/webvr-polyfill.min.js.map`,
  `build/webvr-polyfill.mjs`, `build/webvr-polyfill.mjs.map`,
  `build/webvr-polyfill.min.mjs`, `build/webvr-polyfill.min.mjs.map`,
  `build/webvr-polyfill.cjs`, `build/webvr-polyfill.cjs.map`, and
  `build/licenses.txt`, plus `README.md` which npm always includes and which
  may be the only additional published file. Declare this boundary as an
  explicit `"files"` allowlist in `package.json` that names the published build
  outputs and `build/licenses.txt`; a bare `build` directory entry or a
  `.npmignore` exclusion list does not satisfy the contract. The published set
  MUST remain exact when the working tree is dirty: stray files at the
  repository root (notes, logs, caches, coverage output) and stale files inside
  `build/` MUST NOT be published.
- The generated `build/` outputs are verification inputs, not deliverables:
  never stage or commit them.
- The published tarball MUST be self-sufficient. After `npm pack` and
  extracting the tarball as an installed dependency, `require('webvr-polyfill')`
  and `import('webvr-polyfill')` MUST both resolve inside that installed tree,
  load the polyfill in a process that supplies only `window`, and expose the
  same constructor, `.version`, `VRDisplay`, and `VRFrameData` as the built
  artifacts. Resolution MUST use the declared `exports` targets; a tarball that
  only works from the repository root does not satisfy the contract.

## Public Contract

- Preserve the package name, version, publication metadata, and runtime dependency.
- Preserve the `WebVRPolyfill` constructor, `.version`, `VRDisplay`, and `VRFrameData`.
- Preserve global population, enable/disable behavior, native-display precedence, and mobile fallback policy.
- Preserve desktop, iOS, and Android user-agent behavior, including the mobile display option.
- Preserve the promise behavior, ordering, count, and names returned by `getVRDisplays()`.
- Preserve configuration defaults and the behavior of `race`, `isIOS`, `isMobile`, `copyArray`, and `extend`.
- Preserve CommonJS loading with a window-only Node environment, through both
  the UMD main entry and the dedicated `.cjs` entry.
- Make both UMD artifacts work as browser globals and CommonJS modules without requiring a browser-global `require`.
- Publish only the license, package metadata, the UMD/ESM matrix, the
  CommonJS entry and its source map, and the standalone license file.

## Behavioral Coverage

Existing behavior must remain equivalent for all native and fallback display
scenarios on desktop, iOS, and Android. This includes cases with no native
API, a native API with no displays, a native API with displays, and the mobile
opt-out configuration.

The migration-specific acceptance checks must also confirm that both UMD
artifacts are generated, the minified artifact exposes the same public API as
the readable artifact, the CommonJS entry loads through `require()` with a
window-only environment, the minified distribution can be built independently
from a clean tree, the full build is reproducible across checkout paths, an
extracted tarball is self-sufficient through both `require` and `import`, all
five published entries behave identically for the same scenario, the watcher
ignores unrelated files, converges on rapid edits, tracks modules added to and
removed from the build graph, survives and recovers from a broken source edit,
recovers when a mapped subdirectory or the entry module is deleted, stays
quiescent for changes that are not build inputs, publishes safely while a full
build runs concurrently, and stops cleanly on `SIGTERM`, the source suite runs
in a DOM-like environment, and the published package excludes development-only
material even from a dirty working tree.

Passing only by process exit code is insufficient. Observable values and
public API behavior must remain correct.

## Constraints

1. Change only the build, test, packaging, and CI toolchain needed for this migration.
2. Do not change production WebVR behavior or rewrite frozen domain modules.
3. Do not weaken, delete, skip, rename, or replace existing behavior tests.
4. Do not remove the CommonJS entry or turn the package into an ESM-only package.
5. Do not use a browser-global `require`, a test-only fallback, a second implementation for the minified artifact, or a source-runtime bridge.
6. Do not retain active legacy build or test tooling in the migrated execution path.
7. Keep dependency declarations and the lockfile consistent with the required target toolchain.
8. Do not add unrelated dependencies, services, browser automation, or public API changes.
9. Do not modify generated build output or dependency directories as delivery changes.
10. Keep all changes inside the task's declared migration boundary.
11. Keep packaging and CI changes narrowly scoped; replacing an existing mechanism is acceptable when the replacement preserves the same public purpose.
12. Additional test setup or CI files are acceptable only when they are directly required by the migration and do not weaken behavior coverage.
13. File renames or replacements must remain within the build, test, packaging, and CI migration boundary.
14. Before finishing, ensure the committed diff contains no generated artifacts, unexpected files, trailing whitespace, or other `git diff --check` errors.

## Completion Criteria

- The target toolchain installs from the lockfile in the offline evaluation environment.
- The complete source behavior suite passes without skipped or weakened cases.
- All UMD/ESM artifacts, the CommonJS entry, and the standalone license file
  are freshly generated by the full build.
- `npm run build-min` produces a complete, reproducible minified distribution
  from a clean tree without a prior full build.
- Builds publish atomically: a concurrent reader of `build/` only ever sees a
  complete generation, a failed or killed build leaves the previous generation
  intact, no staging directory survives a later build, and watch rebuilds obey
  the same rule.
- A build observes one input snapshot: if `src/`, `licenses.txt`, or build-read
  package metadata changes mid-build, the result is either one complete
  generation from a single state or a nonzero failure that leaves the previous
  generation byte-for-byte intact.
- Changing `licenses.txt` while the watcher runs republishes the whole
  published set with the new banner.
- Build and watch drivers stay inside the Node/Vite toolchain (no external
  interpreter or shell subprocess), and the published tree is readable by
  users other than the build owner.
- The full build produces byte-identical artifacts when repeated from a
  different absolute checkout path.
- Browser-global, CommonJS window-only, and normal/minified parity checks pass,
  including loading the `.cjs` entry through `require()`.
- The publication contents match the package contract even when the working
  tree contains stray files, and the extracted tarball loads through both
  `require('webvr-polyfill')` and `import('webvr-polyfill')`.
- All five published entries produce the same observable behavior for the same
  scenario.
- The watcher tracks modules added to and removed from the build graph,
  ignores new files under `src/` that the graph does not import, survives a
  broken source edit without corrupting artifacts, recovers on the
  next valid edit, converges on the last of several rapid edits, publishes
  every rebuild atomically, republishes the whole published set after `build/`
  is deleted or `licenses.txt` changes while it runs, and shuts down within 20
  seconds on `SIGTERM` without leaving child processes.
- No frozen production behavior or protected evaluation asset is modified.
- The committed change set contains only migration inputs, not files produced by a build or package command.

## Change Boundary

- The allowed change set is limited to `.npmignore`, `.travis.yml`,
  `.gitignore`, `.babelrc`, `package.json`, `package-lock.json`,
  `rollup.config.js`, `rollup.config.min.js`, `vite.config.mjs`,
  `vite.config.js`, `vitest.config.mjs`, `vitest.config.js`,
  `scripts/build-vite.mjs`, `scripts/watch-vite.mjs`, `test/.babelrc`,
  `test/init.js`, `test/setup.js`, `test/setup.mjs`, `test/utils.js`, and
  `test/webvr-polyfill.test.js`.
- CI workflow files may be added or modified only as
  `.github/workflows/*.yml` or `.github/workflows/*.yaml`.
- Do not add, delete, rename, or modify any path outside this set, including
  generated files, dependency directories, committed build artifacts, source
  modules (`src/**`), README content, license text, or fixture files.
- The package script values and package metadata are evaluated as exact
  contract values, not merely by equivalent behavior.
- Before completion, verify that the final diff contains only allowed paths and
  passes `git diff --check`.

IMPORTANT: Please work on this in a new branch from main and commit everything when you are done.
