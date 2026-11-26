# Migrate the release action implementation from TypeScript to Go

## Background

The `release-action` GitHub Action (repository `ncipollo/release-action`) implements its release creation, release update, artifact handling, and output reporting logic in TypeScript 6.0.3 under `src/`. The action is currently packaged for the Node.js runtime, and `action.yml` declares its public inputs, outputs, and branding.

## Goal

- Migrate the implementation in `src/` from TypeScript 6.0.3 to Go 1.23.0.
- The Go implementation MUST provide the release creation and update, artifact globbing and upload, release-asset deletion, output reporting, and GitHub error formatting that `src/` currently provides.
- The action MUST keep reading the `action.yml` inputs and reporting the `action.yml` outputs with unchanged names and semantics.
- The action entry point (`action.yml` `runs`) MUST launch the Go implementation. The `runs` section MAY be adapted for this purpose.
- Out of scope: the `action.yml` input, output, and branding definitions, and the GitHub REST endpoint selection and request parameter mapping. These keep their current content.

## Public contract to preserve

- `action.yml` MUST keep the current names, requiredness, defaults, and descriptions for these inputs: `allowUpdates`, `artifactErrorsFailBuild`, `artifact`, `artifacts`, `artifactContentType`, `body`, `bodyFile`, `commit`, `discussionCategory`, `draft`, `generateReleaseNotes`, `generateReleaseNotesPreviousTag`, `immutableCreate`, `makeLatest`, `name`, `omitBody`, `omitBodyDuringUpdate`, `omitDraftDuringUpdate`, `omitName`, `omitNameDuringUpdate`, `omitPrereleaseDuringUpdate`, `owner`, `prerelease`, `removeArtifacts`, `replacesArtifacts`, `repo`, `skipIfReleaseExists`, `tag`, `token`, and `updateOnlyUnreleased`.
- `action.yml` MUST keep the current output names and semantics: `id`, `html_url`, `upload_url`, `tarball_url`, `zipball_url`, and `assets`.
- The action MUST keep the current GitHub REST operations and their parameter mapping: create release, update release, get release by tag, generate release notes, list releases, list release assets, delete release asset, and upload release asset.
- The `tag` input MUST fall back to the git ref when that ref starts with `refs/tags/`. When neither source provides a tag, the action MUST fail with `No tag found in ref or input!`.
- The `token` input MUST be read as a required value.
- The `artifacts` input MUST fall back to `artifact` when empty, and `artifactContentType` MUST default to `raw`.
- `owner` and `repo` MUST default to the workflow repository context when their inputs are empty.
- `makeLatest` MUST accept only `legacy`, `true`, or `false`; any other value MUST be treated as absent.
- String inputs MUST be trimmed of surrounding whitespace. Boolean inputs MUST be true only for the exact string `true`, except `skipIfReleaseExists`, which MUST accept the workflow boolean literals and fail otherwise.

## Behavior requirements

- When `allowUpdates` is true, the action MUST look up the release by tag and update it. When that lookup fails with a 404, the action MUST look for an existing draft release with the same tag and update it, or create a new release when no matching draft exists. When `allowUpdates` is false, the action MUST create a new release.
- When a release lookup fails with a status other than 404, the action MUST propagate the error.
- When `skipIfReleaseExists` is true and a non-draft release exists for the tag, the action MUST skip the remaining work and report a notice. When that lookup fails, the action MUST continue instead of failing.
- When `updateOnlyUnreleased` is true and the release being updated is neither a draft nor a prerelease, the action MUST fail.
- An artifact input string MUST be split on commas and newlines. Each entry MUST be left-trimmed, have path separators normalized to `/`, have a leading `~` expanded to the home directory, and be expanded with glob matching.
- Artifact patterns MUST support the same glob syntax as the current implementation, including POSIX named character classes. For example, `[[:digit:]].zip` MUST match `1.zip`, and `[[:alpha:]].zip` MUST match both `a.zip` and the Unicode-letter filename `é.zip`.
- A glob that matches no files, or a matched path that is a directory, MUST fail the action when `artifactErrorsFailBuild` is true and MUST only warn otherwise. Directory paths MUST NOT be uploaded.
- Each artifact's name MUST be the base file name of its path, and its content length MUST be the file size.
- When `replacesArtifacts` is true, the action MUST delete existing release assets whose names match the artifacts being uploaded before uploading. When `removeArtifacts` is true, the action MUST delete all existing release assets before uploading.
- A failed upload with a 5xx status MUST be retried at most three times. A failure that is not retried MUST fail the action when `artifactErrorsFailBuild` is true, and MUST otherwise warn and skip that artifact.
- An artifact whose upload was skipped MUST NOT appear in the `assets` output.
- The action MUST write `id`, `html_url`, and `upload_url` from the release response, write `tarball_url` and `zipball_url` as empty strings when the response value is null, and write `assets` as a JSON string mapping asset names to download URLs.
- The release name MUST default to the tag when `name` is not provided. The release body MUST come from `body` when set, otherwise from the contents of `bodyFile`, otherwise be empty.
- When `generateReleaseNotes` is true, generated notes MUST be appended to the body separated by a newline, or used as the body when the body is empty. On updates this MUST only happen when `omitBodyDuringUpdate` is false. `generateReleaseNotesPreviousTag` and `commit` MUST be passed to the generate-notes call when present.
- During updates, the `omitBodyDuringUpdate`, `omitNameDuringUpdate`, `omitDraftDuringUpdate`, and `omitPrereleaseDuringUpdate` flags MUST omit the corresponding field so the existing value is preserved.
- When `immutableCreate` is true and `draft` is false, the action MUST first create a draft release, upload artifacts, then publish it with `draft` false while omitting the body and commit, and MUST report outputs from the publish response.
- GitHub error formatting MUST keep the `Error <status>: <message>` prefix, append `Errors:` followed by `- <detail>` lines when details exist, and append the 404 line `Make sure your github token has access to the repo and has permission to author releases`. Details MUST be formatted as `<resource> does not exist.`, `The <field> field on <resource> is missing.`, `The <field> field on <resource> is an invalid format.`, or `<resource> already exists.` for the `missing`, `missing_field`, `invalid`, and `already_exists` codes respectively; other codes MUST use the detail message with `Please see <documentation_url>.` appended when a documentation URL exists.

## Constraints

1. Production code in the migration scope MUST be implemented in Go.
2. Production code in the migration scope MUST NOT import, execute, or delegate the migrated responsibilities to the TypeScript implementation or the Node.js runtime, including through subprocesses, RPC, FFI, or dynamic loading.
3. The migration MUST NOT remove public capabilities or change their contracts outside the stated goal.
4. The `action.yml` input, output, and branding definitions and the GitHub REST endpoint selection and parameter mapping MUST NOT be changed, except for adapting the `runs` section to launch the Go implementation.
5. Changes MUST be limited to Go source and module files, the `action.yml` entry point, build and test configuration, and the supporting assets needed to build and run the action. The existing TypeScript test suite MAY be replaced by Go tests that preserve the same behavioral assertions.
6. Node.js and TypeScript dependencies that are not needed by the Go implementation MAY remain in the repository, but they MUST NOT be used to perform the migrated responsibilities.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. The `action.yml` public interface and the GitHub REST operation mapping are protected artifacts and MUST remain consistent with the migration.
9. The working directory is `/workspace/repo`. The submitted work is collected as the repository difference against the provided starting commit, so keep all required sources, module files, and supporting assets in the repository working tree.

## Validation

From `/workspace/repo`, run `go build ./...` to compile the Go implementation and `go test ./...` to run its tests. The repository's existing `__tests__` suite documents the pre-migration behavior and can guide equivalent Go tests; running the TypeScript suite after the migration does not by itself confirm the Go behavior.

The migration baseline of this task is base commit 389305c50bc7406ce7e55077eb8b863e0f0a3083.
