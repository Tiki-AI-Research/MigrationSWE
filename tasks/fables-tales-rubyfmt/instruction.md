# Migrate rubyfmt from Rust to Go

## Background

rubyfmt is an opinionated Ruby source formatter. Its current Rust workspace contains a `rubyfmt-main` command-line binary and a `rubyfmt` library that parses Ruby with `ruby-prism` and renders formatted source. The CLI formats files, directories, `@` path lists, or stdin, and supports check and in-place modes.

## Goal

- Migrate the formatter and CLI implementation from Rust (edition 2024; CI toolchain 1.95.0) to Go 1.24.0.
- The Go implementation MUST parse and format Ruby source itself and MUST provide the formatting and CLI responsibilities currently implemented in the Rust workspace.
- Keep the documented formatting style and all fixture expectations unchanged.
- `editor_plugins/` and the browser/client UI are out of scope and MUST NOT be migrated.

## Public contract to preserve

- The executable MUST be invocable as `rubyfmt` with positional `include-paths` accepting file paths, directories, and `@file` path lists. stdout is the default output sink.
- `--check`/`-c` MUST emit a unified diff to stdout and exit 5 when differences are found, 1 on syntax errors, and 0 when clean. The diff MUST be produced through the vendored `github.com/pmezard/go-difflib/difflib` package, so `go list -deps ./cmd/rubyfmt` MUST list that package. Each changed input contributes its own diff, and when no input changes the command MUST exit 0 with empty stdout; every input mode (named files, directory traversal, `@file` lists, and stdin) MUST support check mode. The diff header MUST name the input: a named file uses its path as given, a file discovered under a directory or `@file` list uses the path at which it was found, and stdin uses `stdin` (or the `--stdin-filepath` value).
- `-i`/`--in-place` MUST rewrite only files whose formatted output differs and MUST leave unchanged files' modification times untouched.
- stdin MUST be read when no include paths are supplied; a terminal stdin MUST print help. `--stdin-filepath` applies ignore rules and MUST conflict with include paths and `--in-place`: each such combination MUST exit non-zero and write a stderr message naming `--stdin-filepath` and stating it cannot be used with the other option.
- `--header-opt-in` MUST format only files whose leading content contains `# rubyfmt: true`. `--header-opt-out` MUST skip files whose leading content contains `# rubyfmt: false`.
- `.rubyfmtignore` and `.gitignore` MUST control directory traversal. `--include-gitignored` stops `.gitignore` rules from excluding files (`.rubyfmtignore` still applies), and explicitly named files bypass ignore rules. Ignore rules MUST be gathered from the working directory and every ancestor directory up to the repository root (the closest ancestor containing `.git`, or the filesystem root when none exists), so an ancestor ignore file still applies when the command runs from a subdirectory. A pattern that matches a directory (for example a trailing-slash directory pattern) MUST exclude the whole directory and everything beneath it: traversal MUST NOT descend into it, and a negated pattern inside a nested ignore file MUST NOT re-include a file under an already-excluded directory.
- Unknown command-line options MUST exit non-zero and MUST write a diagnostic naming `rubyfmt` to stderr.
- The library formatting entry point MUST be `rubyfmt.FormatBuffer([]byte) ([]byte, error)` and MUST return formatted bytes or a syntax/IO error whose value exposes `ExitCode() int` that maps to the documented exit codes.
- `--version`/`-V` and help output MUST identify the program as `rubyfmt`. Exact help wording and terminal color output need not be byte-identical.
- The migrated project MUST be a Go module whose path is `rubyfmt`, with the public library importable as `rubyfmt` from the module root. Evaluation builds the CLI from the `./cmd/rubyfmt` entry point with the fixed command `go build -mod=vendor -buildvcs=false -o rubyfmt ./cmd/rubyfmt`; that command MUST succeed offline with vendored dependencies.

## Behavior requirements

- Formatting the input `a 1,2,3` from stdin or from a file MUST produce `a(1, 2, 3)\n` and exit 0.
- For every paired `_actual.rb` and `_expected.rb` fixture under `fixtures/small` and `fixtures/large`, formatting the actual input MUST emit the expected bytes, and formatting the expected bytes MUST be idempotent.
- Formatting MUST preserve Ruby program behavior: output produced by running each `ci/` stress script before formatting MUST equal the output produced after formatting.
- A syntax error on stdin MUST exit 1 and report a syntax error on stderr. Outside check mode and without `--fail-fast`, syntax and IO errors on ordinary named inputs MUST be warnings that keep processing and exit 0; each warning MUST be written to stderr, and IO failures MUST use `IO error` wording. A missing `@file` path list is not an ordinary named input: it MUST exit 3 even without `--fail-fast` and under `--check`.
- `--fail-fast` MUST exit 1 on a syntax error and 3 on an IO error, including a missing `@file` list.
- In check mode, each changed input MUST produce the documented unified diff with its file path. With `--stdin-filepath`, the diff MUST use the supplied path in the header.
- An ignored `--stdin-filepath` MUST pass input through unchanged. Under `--check`, that same ignored input MUST produce no output and exit 0.
- Explicit file paths MUST be formatted regardless of extension. Directory traversal MUST format only files ending in `.rb`.

## Constraints

1. The migration MUST NOT remove public capabilities or change their contracts outside the stated goal.
2. Production code within scope MUST NOT import, link, invoke, or dynamically load the previous Rust implementation for formatting.
3. Production code within scope MUST NOT delegate formatting to the Rust implementation through subprocesses, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to the formatter and CLI source, build and dependency configuration, and supporting assets needed for the Go port.
5. CLI flag names, exit-code meanings, stdin/stdout/in-place modes, the documented formatting style, and all fixture expectations MUST NOT change.
6. `fixtures/` contents, including every `_actual.rb` and `_expected.rb` pair, MUST remain unchanged. `editor_plugins/` MUST NOT be modified.
7. Public test harnesses MAY be adapted to invoke the Go implementation, but their behavioral assertions MUST be preserved.
8. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
9. The CLI and formatter implementation MUST live in the `rubyfmt/internal/cli` and `rubyfmt/internal/formatter` packages (including `internal/formatter/formatter.go`). The Ruby parser MUST be a separate package `rubyfmt/internal/prism` exposing a cgo bridge (`internal/prism/prism.go`) and compiling the bundled Prism C parser (`internal/prism/prism.c` plus the vendored `third_party/prism` sources) with `CGO_ENABLED=1`; the built binary MUST NOT require `ruby`, `cargo`, or `rustc` on `PATH`. `go list -deps ./cmd/rubyfmt` MUST list `rubyfmt/internal/cli`, `rubyfmt/internal/formatter`, `rubyfmt/internal/prism`, and `runtime/cgo`, and MUST NOT list any package of the previous Rust implementation or a subprocess / FFI / RPC / dynamic-loading wrapper.
10. The migrated tests MUST be Go tests and `go test ./...` MUST pass offline. Migrate the public Rust test harnesses (`tests/*.rs`, `benches/`) to equivalent Go tests at the conventional locations (`rubyfmt_test.go`, `internal/cli/cli_test.go`, `internal/formatter/fixtures_test.go`, `internal/formatter/binary_test.go`, `internal/formatter/bench_test.go`, `internal/formatter/stress_test.go`), preserving their behavioral assertions. The `ci/*_stress_test.rb` harness scripts MUST remain unchanged.
11. All work MUST be done in the repository working tree at `/workspace/repo`. Evaluation collects the repository diff against the starting commit, so leave all source, configuration, and supporting files in that working tree.
