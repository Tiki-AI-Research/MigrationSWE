## Background

shiv creates self-contained Python zipapps. Its `shiv` command assembles an application and its dependencies, while `shiv-info` inspects an assembled archive. The project currently depends on Click 6.7 and must move to Click 8.1.7. Users already depend on the command-line interface, archive metadata, startup modes, extraction behavior, and failure behavior, so the migration must preserve those observable contracts.

## Goal

Update the Click-backed command-line and packaging behavior to use Click 8.1.7 exactly. Keep the existing commands, accepted invocation forms, dependency-installation semantics, archive contents, metadata meaning, runtime behavior, and failure statuses. The resulting behavior MUST follow the explicit contracts below, including any clarified precedence or error behavior; do not preserve an older implementation quirk when it conflicts with those contracts.

## Public contract to preserve

- `shiv` MUST continue to support `-h`, `--help`, `--halp`, `--version`, `-e`/`--entry-point`, `-c`/`--console-script`, `-o`/`--output-file`, `-p`/`--python`, `--site-packages`, `--build-id`, `--compressed`, `--uncompressed`, `--compile-pyc`, `-E`/`--extend-pythonpath`, `--reproducible`, `--no-modify`, `--preamble`, and `--root`.
- `shiv-info <pyz>` MUST continue to support `-h`, `--help`, `--halp`, and `-j`/`--json`.
- The three help aliases MUST remain equivalent and MUST be presented together as the same help option. The command usage shown for `shiv` and `shiv-info` MUST retain their established command-specific forms. Every preserved option and alias MUST remain part of the actual Click command interface as well as its rendered help; accepting or printing an option through an unrelated compatibility shortcut is not sufficient.
- `shiv --version` MUST succeed without building anything and MUST retain the established `shiv, version ...` output prefix.
- Help and version requests MUST be immediate successful requests. They MUST not install dependencies, validate unrelated paths, or create, replace, or modify an archive. Extra arguments must not turn a help or version request into an unrelated build.
- A request that lacks both dependency-installation arguments and `--site-packages` MUST fail with status 1 and report `You must supply PIP ARGS or --site-packages!`.
- A request that supplies input without `-o`/`--output-file` MUST fail with status 1 and report `You must provide an output file option! (--output-file/-o)`.
- A failed dependency installation MUST fail with status 1 and report `Pip install failed!`.
- An invalid entry point, missing console script, path-like console-script name, invalid path, or other user error MUST fail clearly with status 1 and MUST NOT leave a misleading successful result. A path validation error MUST identify the affected option and supplied path; nonexistent paths use status 2, and `--site-packages` accepts directories while `--preamble` accepts files. Valid paths containing spaces MUST work.

## Dependency and argument behavior

- `--site-packages` MUST accept multiple directories. A build using only those directories MUST work without invoking dependency installation merely because no pip argument was supplied.
- Supplied directories MUST be combined in command-line order. All non-conflicting files and package metadata from every valid source MUST remain available. If the same relative path occurs more than once, the later source MUST provide the effective bytes and metadata for that path, while the archive retains only one effective member for the path; when multiple contributing copies are retained, their order MUST follow the declared source order.
- Dependency-installed files and explicitly supplied files MUST be combined without silently dropping either source.
- The final archive MUST contain one effective member for each archive path, use `/` as the member separator, and resolve duplicate paths the same way regardless of whether a source came from dependency installation or `--site-packages`.
- Arguments not consumed by shiv MUST reach dependency installation in their original order and spelling. This includes repeated options, positional arguments, values that begin with a dash, and equals-form options. Options that shiv explicitly disallows MUST still be rejected rather than forwarded.
- The disallowed options `-t`, `--target`, `--editable`, `-d`, `--download`, `--user`, and `--prefix`, including applicable equals forms, MUST fail with status 1 and identify the supplied argument and its reason.
- When both an explicit module entry point and a console script are supplied, the explicit module entry point MUST determine startup.
- A console script MUST work from a declared package metadata entry or, when the requested name has no such entry, from a valid executable launcher supplied by a package. A metadata entry for the requested name MUST take precedence over a launcher with the same name. When more than one supplied package source declares the requested name, lookup MUST retain the established source-order behavior; changing dependency versions must not silently select a different declaration. The requested name MUST be matched exactly: names containing path separators, launchers for an unsupported platform, and requests for an undeclared or missing name MUST be rejected rather than redirected to another entry.
- A console-script archive MUST pass arguments after the archive path unchanged and MUST return the launcher's exit status. The launcher's standard output, standard error, argument order, and non-zero status MUST remain visible to the caller.
- Entry-point syntax errors supplied by the user MUST be reported during the build and MUST prevent a successful archive from being produced. A syntactically valid entry named by package metadata may retain the established runtime failure behavior if its target cannot be resolved; these two cases MUST NOT be conflated.
- With no explicit callable entry point, the archive MUST retain the established interactive/stdin startup behavior.

## Behavior that must stay unchanged

- A module entry point for the hello package MUST still print `hello world` when the archive is run. Module-entry and console-script modes MUST remain distinct.
- `shiv-info` text output MUST identify the archive path and its metadata. JSON output MUST be valid JSON. Both views MUST describe the same archive state, including build identifier, timestamp, interpreter, entry-point mode, compression, extraction settings, reproducibility, and recorded source hashes.
- `--compressed` and `--uncompressed` MUST affect compression only. `--build-id` MUST remain in metadata and MUST affect the documented extraction identity.
- `--root` and an environment-provided root override MUST select the documented extraction location. Nested package paths MUST remain nested below that location, and the selected location MUST be created when needed.
- `--extend-pythonpath` MUST expose archive contents to child processes while retaining existing path entries in their original order and without duplicates.
- `--compile-pyc` MUST produce compiled bytecode in the extracted tree. Without it, extraction MUST not create bytecode solely as a side effect of extraction.
- `--no-modify` MUST raise `RuntimeError` at runtime when a checked extracted Python source file has been changed or an additional Python source file has been introduced. Preserve the existing scope of that check: it examines Python source files that are present in the extracted tree, so the absence of a previously recorded source file alone does not trigger this integrity error. If application startup later requires that missing file, the resulting normal import or execution failure MUST remain visible.
- A successful archive MUST be a readable, runnable zipapp with the expected bootstrap and metadata, one effective entry for each archive path, and preserved source permissions where supported. The output MUST be executable on platforms that support executable zipapps. For a newly created archive, its final POSIX mode MUST follow the established umask behavior; overwriting an archive MUST retain its existing read/write permissions while ensuring executable bits remain available.
- `shiv-info` MUST validate the input before presenting a successful result. It MUST fail with a non-zero status for a missing file, a regular non-archive file, or a corrupt archive in both text and JSON modes. It MUST NOT emit a success header, fabricated metadata, partial metadata, or a success-shaped JSON document for any of these inputs. Invalid-input handling MUST retain the established failure status, output-channel placement, and a diagnostic that identifies the underlying cause; do not replace an existing failure with newly synthesized metadata or an unrelated success-shaped response.

## Failure, replacement, and recovery guarantees

- From a user's perspective, extraction and archive replacement MUST be all-or-nothing. A partially completed operation MUST never appear as a valid usable result.
- If the first extraction or build fails, no partial target or archive may remain that could be mistaken for a successful result.
- If replacing an existing valid extraction or archive fails at any point, the complete previous result MUST remain readable and runnable, with its contents and bytes unchanged where byte identity is part of the existing behavior.
- If a complete prior extraction and an incomplete replacement are both present, the next invocation MUST discard the incomplete state and preserve or restore the complete prior result. The original replacement or compilation error MUST remain observable to the caller when the operation fails.
- If an operation is interrupted, a later invocation MUST be able to recover and complete normally without manual cleanup. Obsolete intermediate state MUST not appear in the usable result.
- Forced extraction MUST replace changed content and remove stale content. Without forced extraction, an existing valid extraction MUST remain usable as-is. Repeated archive entries for one path MUST produce the content of the last entry.
- A later successful build or extraction MUST not be prevented by names or remnants left by an earlier failed operation; failed operations MUST remove unusable temporary or backup state that could be mistaken for the current result.
- These guarantees apply to normal errors, dependency-installation errors, invalid inputs, compilation errors, and failures occurring after output has begun to be written.

## Locking and concurrency

- A file lock MUST provide mutual exclusion across threads and processes using the same path. An already-held lock MUST block another acquisition until release.
- A finite timeout and polling interval MUST control waiting. A timed-out acquisition MUST raise `TimeoutError` identifying the lock path, MUST not enter the protected operation, and MUST not prevent a later acquisition.
- A lock path that merely exists on disk MUST still be acquirable when it is not actively held.
- Locks MUST be released after normal exit and after exceptions. Both protected-context use and explicit acquire/release use MUST work.
- Concurrent builds targeting one output MUST finish with a complete runnable archive from one of the completed builds. They MUST not corrupt the archive, lose a completed result to a partial file, or leave unusable residue.

## Reproducibility and metadata

- With `--reproducible` and the same `SOURCE_DATE_EPOCH`, equivalent builds MUST be byte-for-byte identical, including archive ordering and reproducible metadata. Builds without reproducibility retain their normal time-dependent behavior.
- Environment metadata MUST round-trip through its JSON representation without losing optional or default values. Environment-variable overrides and build identifiers MUST retain their documented precedence.
- A Python interpreter value longer than 128 characters for the zipapp shebang MUST fail with status 1 and report `Shebang is too long, it would exceed BINPRM_BUF_SIZE! Consider /usr/bin/env`.
- A preamble MUST run before its selected entry point. Supported Python and executable-shell preambles MUST work on their supported platforms, and completion of a shell preamble MUST not prevent application startup.

## Constraints

1. Use Click 8.1.7 exactly; do not retain a dependency on Click 6.7.
2. Do not remove, rename, weaken, or reinterpret existing public commands, options, supported packaging capabilities, runtime modes, error statuses, or documented diagnostics.
3. Preserve the observable archive, extraction, locking, argument-forwarding, metadata, and execution behavior described above on supported platforms.
4. Keep changes limited to this Click migration and compatibility work required for it. Do not introduce unrelated features, protocol changes, or dependency changes.
5. A clean invocation with documented inputs MUST be sufficient; do not rely on network access or caller-specific local state.

IMPORTANT: Create a new branch from the task's base commit `ff542cbe75ea832df3a989d07c7fdf5214f727fa` and commit all task changes, including new files, when you are done.
