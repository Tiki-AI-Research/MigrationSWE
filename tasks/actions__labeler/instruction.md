# Migrate the labeler action from TypeScript to Go

## Background

`actions/labeler` is a GitHub Action that labels pull requests using a YAML configuration. The implementation is written in TypeScript 6.0.3, compiled with `tsc` and `@vercel/ncc`, and executed on Node.js 24, as declared in `action.yml` (`runs.using: node24`, `runs.main: dist/index.js`). This task migrates the action's implementation to Go 1.24.0.

## Goal

- Migrate the labeler implementation from TypeScript 6.0.3 to Go 1.24.0 across these modules: `src/main.ts`, `src/labeler.ts`, `src/changedFiles.ts`, `src/branch.ts`, `src/utils.ts`, `src/get-inputs/*`, `src/api/*`, plus the action configuration in `action.yml` and `package.json`.
- The Go implementation MUST provide the full migrated behavior: reading action inputs, loading and validating the label configuration, computing matching labels from changed files and branch names, synchronizing labels through the GitHub API, and writing the action outputs.
- The action MUST perform its labeler behavior in the Go implementation. The TypeScript sources under `src/` and the compiled `dist/index.js` bundle MUST no longer be the action entrypoint.
- Documentation, workflows, license files, and other repository files outside the scope above are out of scope. Do not introduce browser, client UI, or frontend artifacts.

## Target Go package layout

The evaluation exercises the migrated behavior by importing the Go packages below, so the implementation MUST use the module path `github.com/actions/labeler` and expose at least the following package paths and exported identifiers (keep the names and signatures observed by the evaluation):

- `internal/config`: `LabelConfigResult`, `GetLabelConfigResultFromObject`
- `internal/core`: `Out`, `Reset`
- `internal/event`: `Payload`, `PullRequest`, `Branch`
- `internal/githubapi`: `Client`, `NewClientForTest`
- `internal/inputs`: `Inputs`, `EscapeNonPrintable`
- `internal/labeler`: `Labeler`
- `internal/match`: `BranchContext`, `CheckAnyBranch`
- `internal/minimatch`: `NewMatcher`
- `cmd/labeler`: the `main` entrypoint

`action.yml` plus the packages above define the surface the evaluation observes; do not rename or relocate these packages or exported identifiers.

## Public contract to preserve

- The `action.yml` inputs MUST keep their names, defaults, and meanings: `repo-token` (default `${{ github.token }}`), `configuration-path` (default `.github/labeler.yml`), `sync-labels` (default `false`), `dot` (default `true`), and the multiline `pr-number`.
- The `action.yml` outputs MUST keep their names and meanings: `new-labels` and `all-labels`, each a comma-separated list.
- The label configuration structure MUST stay the same: top-level label names mapped to arrays of match objects using `changed-files`, `head-branch`, and `base-branch`, with optional `any` and `all` groupings, plus the top-level options `changed-files-labels-limit` and `max-files-changed`.
- The action MUST use the standard GitHub Actions runtime interface: read inputs from `INPUT_*` environment variables, read the event payload from `GITHUB_EVENT_PATH`, and append outputs to the file named by `GITHUB_OUTPUT`.
- GitHub API semantics MUST be preserved: REST `issues.addLabels`, paginated REST `pulls.listFiles`, REST `pulls.get`, REST `repos.getContent` with base64 decoding, and the GraphQL `removeLabelsFromLabelable` mutation.
- Changed-file globs MUST keep minimatch semantics, including the `dot` option and `!` negation patterns. Branch patterns MUST be treated as JavaScript regular expressions, so their matching semantics MUST follow ECMAScript, including ECMAScript-only constructs such as negative and positive lookahead. A regular-expression engine that is not ECMAScript-compatible (for example Go's standard `regexp` package, which implements RE2 and rejects lookaround) is not sufficient; branch patterns MUST be compiled with an ECMAScript-compatible engine, and the offline build environment provides an ECMAScript-compatible Go module for this purpose.
- Failure behavior MUST be preserved: an unexpected error is logged and fails the action with the error message; a 403 unauthorized error on a label update surfacing the `issues: write` permission hint; and a `Resource not accessible by integration` error downgraded to a warning plus a failed step.
- The repository `README.md` documents this public behavior and remains the reference for these contracts.

## Behavior requirements

- If a changed file matches a label's `changed-files` rules, the action MUST add that label, supporting all four combinations: `any-glob-to-any-file`, `any-glob-to-all-files`, `all-globs-to-any-file`, and `all-globs-to-all-files`.
- `any` rules MUST match when at least one option matches; `all` rules MUST match when every option matches; every match object for a label MUST match before the label is applied.
- With `sync-labels: true`, the action MUST remove configured labels that no longer match, in a single GraphQL mutation per pull request. With `sync-labels: false`, it MUST NOT remove labels.
- Labels absent from the configuration MUST never be removed, and labels added concurrently by another actor MUST be preserved.
- If the label set exceeds 100 labels, the action MUST apply only the first 100 and log a warning listing the excess labels.
- If `max-files-changed` is set and the changed-file count exceeds it, file-based labeling MUST be skipped, branch-based labels MUST remain active, and existing changed-files labels MUST be left untouched.
- If `changed-files-labels-limit` is set and the count of new changed-files labels exceeds it, all new changed-files labels MUST be skipped for that run. The limit counts only labels not already on the pull request, and a count equal to the limit is still applied. When both options are set, `max-files-changed` is evaluated first.
- A label definition containing any changed-files rule MUST be treated as a changed-files label for both `max-files-changed` and `changed-files-labels-limit`.
- The action MUST read the configuration path locally when the file exists; otherwise it MUST fetch the file through `repos.getContent` and decode the base64 content.
- Configuration keys other than `changed-files`, `head-branch`, `base-branch`, and the two top-level options MUST be ignored with an informational message. Invalid configuration values, such as a negative or non-integer limit or a malformed `changed-files` block, MUST fail with a descriptive error.
- `pr-number` MUST accept newline-separated values: blank entries are ignored, each valid entry is a positive integer after trimming, and invalid entries MUST produce a warning and be skipped. When `pr-number` is empty, the action MUST use the pull request number from the event payload.
- Invalid `pr-number` warnings MUST escape non-printable characters (for example as `\xNN`) in the reported value.
- Pull requests that cannot be fetched, or that have no changed files, MUST produce a warning and be skipped.
- When a label addition fails with a server error, the action MUST verify whether the requested labels were actually applied before failing.
- A failed bulk removal MUST fail with an error identifying the affected configured labels and the pull request.
- If the configuration file cannot be fetched, the action MUST warn about the missing file and access rights, then log the error and fail the step.
- `new-labels` MUST list only labels added by the run; `all-labels` MUST list the resulting label set after the run, capped at the 100-label limit. Both MUST be written to `GITHUB_OUTPUT` as comma-separated values, with existing labels first and newly matched labels appended in configuration order.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code in the migrated scope MUST NOT import or call the TypeScript sources or `@actions/*` Node packages for the migrated responsibilities.
3. Production code in the migrated scope MUST NOT delegate migrated responsibilities to the Node.js or TypeScript runtime through subprocesses, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to the migrated source scope, the target-language sources, dependency and build configuration, and supporting assets needed to build and run the Go action.
5. Repository files outside the migration scope, such as documentation and workflow files, may remain. Dependencies and configuration used only by the migrated TypeScript source may be removed or replaced.
6. The public action interface in `action.yml`, the label configuration format, and the documented output values are protected contracts; do not rename or repurpose them.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs or outputs to fake compliance.
8. As an explicit exception to constraint 4, test and fixture updates needed to validate the Go implementation are permitted, provided the behavioral assertions are preserved; do not delete or weaken coverage.
9. Work in `/workspace/repo`. The evaluation captures the repository changes as a diff against the task baseline, so keep all required source, configuration, and supporting files inside the repository working tree and do not leave them only in ignored or build-only locations.

The migration baseline of this task is base commit 98ce1450c7908643084f7487327dfa4f4bf8a367.
