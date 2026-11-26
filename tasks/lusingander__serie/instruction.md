# Migrate the non-UI core of serie from Rust to Go

## Background

serie is a terminal application that currently implements Git repository loading, commit and reference modeling, commit-detail file changes, and commit-graph lane calculation in Rust 2021 (`src/git.rs`, `src/graph/calc.rs`). The non-UI parts of `src/main.rs` parse command-line options, load configuration, load the repository, and calculate the graph before the TUI starts. The project uses Rust edition 2021 and declares Rust 1.88.0 as its minimum supported version.

## Goal

- Migrate the non-UI repository and graph core from Rust 2021 to Go 1.24.0.
- The Go core MUST provide repository loading from Git, commit and reference data, commit-detail file changes, commit-graph lane and edge calculation, and loader orchestration that accepts a repository path, commit ordering, an optional maximum commit count, and a mailmap flag.
- Deliver the core as a Go library or a standalone CLI, or both. The delivered form MUST expose the public behavior described below.
- The following areas are out of scope and MUST remain unchanged: ratatui terminal rendering, layout, event loop, and terminal lifecycle (`src/app.rs`, `src/view/**`, `src/widget/**`); Kitty/iTerm2 image protocols, PNG/base64 encoding, `GraphImageManager`, and pixel geometry (`src/protocol.rs`, `src/graph/image.rs`, `src/graph/geometry.rs`); color themes, UI configuration, key bindings, and status-line interaction (`src/color.rs`, `src/keybind.rs`, and the UI sections of `src/config.rs`); clipboard access, user-command execution, and terminal suspend/resume (`src/external.rs` and the corresponding TUI interactions in `src/main.rs`).
- Do not change existing CLI option names, TUI observable behavior, the configuration file format, `config.schema.json`, or existing public data fields.

## Public contract to preserve

- Commit records MUST expose `commit_hash`, `author_name`, `author_email`, `author_date`, `committer_name`, `committer_email`, `committer_date`, `subject`, `body`, and `parent_commit_hashes`. Dates MUST retain the original fixed offset and MUST be represented as RFC3339 timestamps.
- A commit hash MUST expose the full hash and a short hash formed from its first seven characters.
- References MUST distinguish tags, branches, remote branches, and stashes. A stash reference MUST also carry its message. HEAD MUST distinguish a named branch, a detached commit target, and no HEAD.
- The Go core MUST expose read-only queries equivalent to `commit`, `all_commits`, `parents_hash`, `children_hash`, `refs`, `all_refs`, `head`, and `commit_detail`. `parents_hash` MUST retain Git's parent order (the first parent first) and `children_hash` MUST return a commit's immediate children in repository commit order.
- The repository loader MUST accept a repository path, chronological or topological commit ordering, an optional maximum commit count, and a mailmap flag.
- `commit_detail` MUST return the commit together with its file changes. For a root commit, every file listed by `git ls-tree -r` MUST be reported as an addition. For a non-root commit, the changes MUST be the `git diff --name-status <commit>^ <commit>` entries computed against the commit's first parent, and MUST be reported as add, modify, delete, or rename with source and destination paths; a merge commit therefore reports the changes introduced relative to its first parent. Unknown status codes MUST be ignored.
- `calc_graph` MUST return the loaded commits in order, a commit-position map, one edge list per commit row, and the maximum lane index.
- Each edge MUST expose its edge type, its lane position (`pos_x`), and the lane position it is associated with (`associated_line_pos_x`). Edge types MUST cover vertical, horizontal, up, down, left, right, right-top, right-bottom, left-top, and left-bottom.
- For each row, edges MUST be sorted by `(associated_line_pos_x, pos_x, edge_type)` and exact duplicates MUST be removed. The edge-type order MUST be vertical, horizontal, up, down, left, right, right-top, right-bottom, left-top, left-bottom.
- For each commit, references MUST be ordered by kind: tag, branch, remote branch, stash. Multiple stashes for one commit MUST retain `git stash list` order.
- If the Go core reads `config.toml`, it MUST preserve the existing file format and the existing `core.option.order` and `core.git.mailmap` semantics. UI-only configuration sections are out of scope.

## Behavior requirements

- Commit enumeration MUST match `git log --date-order` for chronological ordering and `git log --topo-order` for topological ordering. The enumerated set MUST cover all local branches, remote branches, tags, HEAD when present, and each stash's first parent. When `max_count` is set, it MUST be applied as `git log --max-count` before stashes are merged.
- Commit records MUST be parsed from NUL-separated records. Each record MUST be split on `0x1f` into exactly ten fields in this order: commit hash, author name, author email, author date, committer name, committer email, committer date, subject, body, and parent hashes. Parent hashes MUST be split on spaces. Dates MUST be parsed from RFC3339 values and MUST retain their original fixed offset.
- When mailmap is enabled, author and committer identities MUST be resolved through the `%aN`, `%aE`, `%cN`, and `%cE` placeholders. When mailmap is disabled, the raw `%an`, `%ae`, `%cn`, and `%ce` values MUST be used. Enabling mailmap in a repository without a `.mailmap` file MUST NOT change identities.
- Each stash MUST be inserted immediately before its first-parent commit in the commit sequence. A stash whose first parent is absent from the loaded commit set MUST be ignored in the commit sequence and MUST still be exposed as a stash reference.
- References MUST be loaded with `git show-ref --head --dereference`. Branches, remote branches, lightweight tags, and annotated tags MUST be distinguished. An annotated tag MUST target the commit named by its `^{}` dereference. Reference loading MUST be independent of `max_count`.
- A bare repository MUST be accepted. A path that is neither inside a Git work tree nor a bare repository MUST fail with `not a git repository (or any of the parent directories)`. A repository with no commits MUST fail with `no commits in the repository`. A failed load MUST NOT return partial repository data.
- Graph lane assignment MUST reproduce the existing lane state machine. A commit with no filtered children MUST take the first vacant lane. Otherwise it MUST reuse the lowest lane occupied by one of its filtered children. A filtered child is a child whose first parent is the commit. Filtered children MUST be considered in repository commit order.
- Graph edges MUST reproduce the existing commit, branch, merge, and overlap-detour cases. A merge edge MUST use a detour when its direct lane path would overlap a lane or vertical edge occupied by another commit.
- When `max_count` truncates the commit set and a commit still has a first parent outside the set, the graph MUST emit a down edge on that commit's row and a vertical edge on every later row at the same lane.
- The maximum lane index MUST include every lane used by ordinary edges, detours, and truncated-parent continuations.
- Commit enumeration, lane assignment, edge order, and reference order MUST be deterministic. Iteration over Go maps MUST NOT influence any result.
- If the Go core provides a CLI, `-n/--max-count` and `-o/--order` MUST preserve the existing option names and values. A CLI value MUST take precedence over the corresponding configuration value. UI-only options are out of scope.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production Go code within scope MUST NOT import, call, or delegate the migrated repository-loading or graph-calculation responsibilities to the Rust implementation or the Rust runtime.
3. The Go core MUST NOT use a Rust binary, CGO, FFI, RPC, or dynamic loading to perform migrated responsibilities.
4. External `git` commands that provide repository data MUST remain the data source, and the Go core MUST execute them itself.
5. Changes MUST be limited to the new Go core, its Go module and build files, and supporting Go tests or fixtures.
6. The Rust TUI, image protocol, color, key-binding, clipboard, and external-command modules are outside scope and MUST remain functionally unchanged.
7. `config.schema.json`, the configuration file format, existing CLI option names, and existing public data fields MUST NOT be altered.
8. Existing Rust tests under `src/tests/**`, the PNG graph snapshots under `tests/graph/**`, and existing public test fixtures are protected assets. You MUST NOT delete assertions, weaken comparisons, skip checks, or hardcode evaluation inputs to fake compliance.
9. You MAY add Go tests and fixtures and adapt build or test wiring needed to run them. Adaptations MUST preserve the original behavioral assertions.
10. The agent environment is configured without network hosts. The Go core MUST build and run without fetching third-party modules during the task.
11. Work in `/workspace/repo`. Keep all source, module, and test changes inside that repository. The evaluator collects the repository diff against the starting commit, so changes outside the repository will not be collected.

## Validation

From `/workspace/repo`, the existing public Rust checks are:

```text
cargo fmt --all -- --check
cargo build --verbose
cargo clippy --all-targets --all-features -- -D warnings
cargo test --verbose
```

These commands validate the existing Rust baseline and protected Rust behavior. They do not by themselves verify the Go core. The starting repository has no public Go test command. Add focused Go tests for the migrated behavior and run them with `go test ./...` from the directory containing your Go module.
