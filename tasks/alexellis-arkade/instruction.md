# Migrate the arkade CLI from Cobra to Kong

## Background

arkade is a Go command-line tool that downloads developer tools and installs Kubernetes applications. It currently uses Cobra 1.10.2 to define its command tree, parse arguments and flags, render help and generate shell completion, so the public command surface is built and dispatched through that framework. The command tree, flag handling, help output, completion output and error behavior already form a stable interface that downstream users rely on.

## Goal

- Completely migrate arkade from Cobra 1.10.2 to Kong 1.8.1 for the root command and every in-scope command group.
- Kong MUST own command tree construction, argument and flag parsing, flag scoping, help rendering, shell-completion generation and command-level error propagation for the migrated commands.
- Every command group that remains out of scope MUST stay reachable through the Kong root command with its current behavior, aliases and flags.
- The migration MUST preserve the public CLI contract described below. It MUST NOT change tool download, checksum verification, archive extraction, Helm execution or Kubernetes resource side effects.

## Public contract to preserve

The following top-level commands and aliases MUST remain accepted:

| Command | Aliases | Notes |
|---|---|---|
| `arkade` | none | Root command. |
| `install` | `i` | Exposes all current Kubernetes app subcommands and `info`. |
| `version` | `v` | Prints version information. |
| `info` | `f` | Available at the root and under `install`. |
| `update` | `u` | Updates the arkade binary. |
| `get` | `g`, `d`, `download` | Downloads tools. |
| `uninstall` | `delete` | Prints uninstall guidance. |
| `completion` | none | Emits shell completion. |
| `release` | `rel`, `r` | Hidden from root help. |
| `chart` | `c` | Contains `verify`, `upgrade` and `bump`. |
| `docker` | `d` | Contains `upgrade` and `gen`. |
| `fstail` | `ft` | Tails files with optional matching. |
| `gha` | none | Contains `bump` with alias `u`. |
| `system` | `s` | Contains `install` with alias `i` and all current system subcommands. |
| `oci` | `o` | Contains `install` with aliases `i` and `extract`, `publish` with aliases `push` and `export`, and `login`. |
| `search` | none | Searches the tool catalog. |

- `docker` also declares the alias `d`. The top-level token `d` MUST continue to resolve to `get`.
- All current app subcommands under `install` MUST remain available. The tool catalog and app catalog MUST NOT change.

Every flag currently declared on a command or subcommand MUST keep its name, shorthand, value type, default value, repeatability, persistence and command scope.

- `get` MUST keep these flags and defaults:
  - `--progress` boolean, default `true`
  - `--format` / `-o` string, default empty
  - `--path` string, default empty
  - `--version` / `-v` string, default empty
  - `--arch` string, default the detected client architecture
  - `--os` string, default the detected client OS
  - `--quiet` boolean, default `false`
  - `--verify` boolean, default `true`
  - `--parallel` / `-p` integer, default `4`
- `install` MUST keep its persistent `--kubeconfig` and `--wait` flags and its local `--print-table` flag. These persistent flags MUST apply to nested install app subcommands and MUST NOT appear on unrelated top-level commands.
- `uninstall` MUST keep its own persistent `--kubeconfig` flag with default `kubeconfig` and its `--wait` flag.
- `version` MUST keep `--json` / `-j` with default `false`.
- `search` MUST keep `--format` with default `table`.
- `release` MUST keep `--dry-run`, `--major`, `--minor`, `--patch`, `--prerelease`, `--latest` and `--token`. The mutual exclusion of `--major`, `--minor` and `--patch` MUST remain.
- `system`, `oci`, `chart`, `docker`, `gha` and `fstail` MUST keep every existing subcommand flag, including shorthand letters, defaults and accepted values.
- The deprecated hidden `--path` flag on `oci install` MUST remain accepted and hidden.

The following observable output, error and exit behavior MUST be preserved:

- Running `arkade` with no subcommand MUST print the existing ASCII art, version and build information, and root help, then exit zero.
- `arkade get` with no positional arguments MUST keep its tool-list behavior for the default table and for `--format table`, `--format markdown` and `--format list`.
- `arkade install` with no app name MUST print the existing app guidance. An unknown app MUST return the existing `no such app` error, including the tool hint when the name matches a known tool. A known app MUST launch its installer.
- `arkade install --print-table` MUST keep its markdown table and application count line.
- `arkade info` with no app MUST print its guidance. Exactly one known app MUST print `Info for app: <name>` and that app's message. An unknown app or more than one argument MUST return an error.
- `arkade version` MUST keep its text output and its `--json` object with the keys `version`, `commit` and `build_date`, using `n/a` when the build has no value.
- `arkade search` MUST keep the query-required error, relevance ranking, alias expansion and fuzzy matching, and the `table`, `list` and `markdown` output formats. No match MUST print the existing no-tools message and exit zero.
- `arkade completion SHELL` MUST emit completion for `bash`, `zsh`, `fish` and `powershell` from the migrated root command tree, including aliases and subcommands. An unsupported shell MUST return an error. No argument MUST print the existing usage guidance.
- `arkade release` MUST keep its zero, one and two positional argument behavior, semver detection, version bump behavior, repository-visibility defaults for `--prerelease` and `--latest`, `--dry-run` output and `gh` and `git` interactions.
- Unknown commands and unknown flags MUST produce a non-zero exit. Errors returned by commands MUST propagate to a non-zero exit. Successful commands MUST exit zero.
- Errors and usage for invalid invocations MUST continue to go to stderr, while normal listings and command output MUST continue to go to stdout.

## Behavior that must stay unchanged

- Every accepted command, alias and subcommand MUST remain executable and produce its existing observable result, including all app subcommands under `install` and all system subcommands under `system install`.
- Help MUST remain available for the root command and every command. Byte-for-byte help and usage text is not required.
- `release` MUST remain hidden from root help while `release`, `rel` and `r` remain accepted.
- `get` MUST accept both `tool@version` and `--version`, MUST reject using both for the same tool, MUST download into `$HOME/.arkade/bin` unless `--path` is supplied, and MUST honor `--arch`, `--os`, `--verify`, `--parallel`, `--progress` and `--quiet`.
- `get` MUST return the existing error when `--parallel` is less than one, when an architecture is unsupported, when an operating system is unsupported, or when a tool name is unknown.
- `get` MUST preserve the existing observable download behavior: network access, destination paths, checksum verification where configured, archive extraction and post-install guidance.
- `install` MUST preserve the existing `--kubeconfig` and `--wait` behavior for nested app installers and MUST preserve their Kubernetes and Helm side effects.
- `oci install` MUST preserve image resolution, shortcut image names, architecture and OS handling, extraction behavior and the positional path override.
- `oci login` MUST preserve its credential storage behavior in the Docker configuration file and its password-stdin handling.
- `system install` and its subcommands MUST preserve their existing installation paths, flags and external command side effects.
- Completion output MUST describe the migrated command tree, not the previous Cobra tree.
- Interrupt or SIGTERM handling during downloads MUST preserve the existing terminal cleanup and non-zero exit behavior.

## constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Code that defines or dispatches migrated commands MUST NOT import or call Cobra.
3. Migrated responsibilities MUST NOT be delegated to the Cobra runtime through subprocesses, remote procedure calls, foreign-function calls, dynamic loading or a wrapper command tree.
4. Out-of-scope commands MAY remain on Cobra: `chart verify`, `chart upgrade`, `chart bump`, `docker upgrade`, `docker gen`, `fstail`, `gha bump`, `oci publish`, `oci login`, each individual `system install` tool command, and every `install` app installer except `argocd`. They MUST be reachable from the Kong root command and MUST keep their current behavior, aliases and flags. App installers are reached through the Kong `install` command via the existing app catalog, so `cmd/apps_catalog.go` is part of this exception too. `cmd/apps/argocd_app.go` is the only app installer in scope and MUST be migrated to Kong; every other `cmd/apps/*` file is out of scope. This exception does not permit a migrated command to be a Cobra wrapper.
5. Changes MUST be limited to command wiring, dependency declarations, supporting assets and the minimum test adaptation needed to compile.
6. The migration MUST NOT change the tool catalog, the app catalog or the supported platform set.
7. The migration MUST NOT change tool download, checksum verification, archive extraction, Helm execution or Kubernetes resource side effects.
8. Byte-for-byte help, usage and generated shell-completion script contents are not required. Command names, aliases, flag names, flag scoping, accepted values and exit semantics are required.
9. You MUST NOT delete, skip, weaken or hardcode checks. Existing tests that must change in order to compile MAY be adapted, but their behavioral assertions MUST remain.
10. Existing public tests, examples, tool definitions and the `cmd/apps/*` installers other than `cmd/apps/argocd_app.go` are protected artifacts. Change them only where required to compile.
11. Work on the repository in its provided starting state and do not substitute another branch or commit. Keep the code buildable with the Go toolchain and language version the repository declares.
12. The evaluation collects all repository changes made from the starting state, so every required change must be committed inside the repository.

IMPORTANT: Create a new branch from the task's base commit 0a0a800fd7554d4eddb1856f9ef8a21214e95bab and commit all task changes, including new files, when you are done.
