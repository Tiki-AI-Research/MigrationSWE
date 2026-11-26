# Migrate the Field Theory CLI from Commander.js to oclif

## Background

The Field Theory CLI is a Node.js and TypeScript command-line application. Its `ft` and `fieldtheory` binaries currently use Commander.js 14.0.3 for command registration, option and argument parsing, help and version output, validation errors, and exit behavior. The target CLI framework is oclif (`@oclif/core`) 4.0.0.

## Goal

- Migrate the CLI framework in `src/cli.ts`, `src/companion-cli.ts`, `bin/ft.mjs`, and `package.json` from Commander.js 14.0.3 to oclif (`@oclif/core`) 4.0.0.
- oclif MUST own command registration, argument and flag parsing, help and version generation, validation failures, and exit behavior for the migrated CLI.
- The migration MUST be a real framework replacement. The CLI MUST NOT keep using Commander.js for these responsibilities through imports, a wrapper, a subprocess, RPC, FFI, or dynamic loading.
- Business logic outside the CLI framework is out of scope. The modules that implement bookmark sync, search, classification, media, Library, Commands, adjacent workflows, skill installation, and app installation MUST keep their current behavior and contracts.

## Public contract to preserve

- The `ft` and `fieldtheory` bin names MUST both continue to run the same CLI through `bin/ft.mjs`.
- With no arguments, the CLI MUST show the first-run welcome when no local data exists and the dashboard otherwise.
- Top-level `--help`/`-h` and `--version`/`-V` MUST continue to work. Version output MUST print the package version.
- The complete command tree MUST remain available. This includes `sync`, `search`, `list`, `show`, `stats`, `viz`, `classify`, `classify-domains`, `model`, `categories`, `domains`, `folders`, `index`, `auth`, `status`, `path`, `paths`, `recent`, `ls`, `tree`, `find`, `grep`, `cat`, `head`, `meta`, `open`, `panel`, `codex`, `tab`, `reveal`, `pwd`, `context`, `link`, `links`, `backlinks`, `tags`, `tagged`, `new`, `append`, `note`, `rename`, `cd`, `back`, `current`, `sample`, `fetch-media`, `md`, `wiki`, `ask`, `lint`, `possible` (alias `ideas`), `seeds`, `repos`, `frames`, `skill`, `library`, `commands`, `app`, `install`, and `bookmarks`.
- Nested groups and subcommands MUST remain, including `current update`, `codex panel`, the `library`, `commands`, `app`, `install`, and `skill` groups, the `possible`/`ideas` group with its adjacent workflow commands, the top-level `seeds`, `repos`, and `frames` groups, the hidden `_tick` and `_run-job` worker commands, and the `bookmarks` compatibility group.
- Option names, aliases, defaults, required values, and accepted forms MUST remain unchanged. This includes variadic options such as `sync --cookies` and `seeds create --artifact`, negated options such as `--no-media`, `--no-launch`, `--no-defaults`, `--no-launchd`, and `--no-load`, the `-y`/`--yes` alias, and `--weight` as an alias for `--effort`.
- Command output MUST keep its current content and stream selection. Human-readable text and errors MUST remain on the streams they use today, and `--json` payloads MUST keep the same field names, types, nesting, and values.
- Framework-generated help layout MAY differ, but help MUST still list the same commands, groups, arguments, options, and descriptions.
- Existing exit behavior MUST remain. Successful commands MUST exit with 0. Missing data or index, command failures, and validation failures MUST exit nonzero. Prompt interruption MUST keep its current exit code, including 130 where the CLI currently uses it.
- Command-specific interactive prompts and TTY-dependent behavior that exist today MUST remain.

## Behavior requirements

- `ft sync --cookies <ct0> [auth_token]` MUST continue to run browser-session sync without opening a browser.
- `ft search "<query>" --limit 20 --json` MUST emit the same JSON records as today, with no logo, What's New, or other command chrome on stdout.
- `ft bookmarks <subcommand> ...` MUST forward to the corresponding top-level command with the same remaining arguments and options. In particular, `ft bookmarks sync` MUST behave exactly like `ft sync`, and `ft bookmarks enable` MUST behave like `ft sync`.
- `ft possible seed <subcommand> ...` MUST continue to forward to `ft seeds <subcommand> ...`, and `ft ideas` MUST remain an alias of `ft possible`.
- The positive-integer options MUST still accept only integers greater than or equal to 1. These include `--limit` on `recent`, `ls`, `find`, `grep`, and `context`, and `--lines` on `head`. Non-integer or lower values MUST produce a validation error on stderr and a nonzero exit.
- Other numeric options MUST keep their current coercion and default behavior.
- Existing mutual-exclusion checks and required-option checks MUST still fail with a nonzero exit and their current user-facing error messages. These include the `sync` rebuild/continue/gaps combinations, the folder flag combinations, `--effort` with a differing `--weight`, and `--repo` with `--repos`.
- Commands that currently print the logo and What's New MUST continue to do so. Commands run with `--json`, the existing navigation and inspection commands, and the internal `_run-job` worker MUST remain free of that chrome.
- Help output MUST still include the cached update notice when a newer version is present in the local update-check cache.
- The existing post-action update check MUST remain for the invocation paths that currently run it.
- Usage and validation errors MUST still show help after the error.
- All command handlers MUST keep their current side effects, including stdout and stderr writes, JSON output, exit codes, prompt behavior, and local file reads and writes.

## Requirement interpretation

- The Goal and explicit Public contract define the required outcome.
- The Constraints define the permitted scope of changes.
- Existing behavior is authoritative unless this instruction explicitly requires a change.
- If repository artifacts appear inconsistent, follow the explicit Public contract and make the smallest behavior-preserving change.
- These requirements specify observable outcomes, not a required implementation strategy.

## Constraints

1. The migration MUST NOT remove public capabilities or change their contracts outside the stated goal.
2. Production code in the migration scope MUST NOT import or call Commander.js for the migrated responsibilities.
3. The CLI MUST NOT delegate the migrated responsibilities to a Commander.js runtime through a subprocess, RPC, FFI, dynamic loading, or a compatibility wrapper.
4. `package.json` MUST add `@oclif/core` 4.0.0 as the CLI framework dependency and MUST remove `commander` from the runtime dependencies. The dependency lockfile MAY be updated accordingly.
5. Changes MUST be limited to the CLI framework integration in `src/cli.ts`, `src/companion-cli.ts`, and `bin/ft.mjs`, the dependency configuration in `package.json` and the lockfile, CLI tests that use Commander-specific registration internals, and newly added black-box CLI tests that execute the built CLI. You MAY add or split CLI-only source files under `src/`.
6. Do not change the business logic or contracts of bookmark sync, search, classification, media, Library, Commands, adjacent workflows, skill installation, or app installation. Do not alter the `~/.fieldtheory` data tree, JSONL bookmark cache, SQLite FTS5 schema version 6 and stable records, media files and manifest, Library and Commands Markdown, adjacent artifact IDs and provenance, `0o700`/`0o600` permissions, OAuth token owner-only storage, or restart and resume behavior.
7. Do not change external protocols such as X GraphQL/API, GitHub Releases, browser-helper health checks, or panel URLs. Do not introduce browser UI or frontend framework work.
8. Tests that depend on Commander-specific registration internals MAY be adapted to the target framework. Preserve their user-observable behavioral coverage. You MUST NOT delete, skip, weaken, or hardcode assertions merely to make the suite pass. You MAY add framework-neutral black-box CLI tests.
9. The working directory is `/workspace/repo`. The task starts from the provided baseline revision, not `main`; preserve that starting point. Leave the required source, dependency, and test changes in the repository. The evaluation collects those changes as a patch relative to the baseline revision.

## Validation

From `/workspace/repo`, run:

- `npm run build`
- `npm test`

Both commands MUST pass. The implementation MUST also satisfy the Public contract above; the public test suite is not exhaustive.
