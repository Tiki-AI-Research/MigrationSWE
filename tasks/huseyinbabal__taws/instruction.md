# Migrate taws from Rust to Go

## Background

`taws` is a terminal tool for browsing and managing AWS resources. The current implementation is a single Rust binary crate named `taws` (version `1.3.0-rc.8`, edition `2021`). It talks to AWS over direct HTTP using self-implemented SigV4 signing instead of the AWS SDK, and it loads its AWS resource catalog from embedded JSON definitions under `src/resources/`.

## Goal

- Migrate the non-interactive parts of `taws` from Rust (edition `2021`) to Go 1.23.0.
- The Go implementation MUST provide the migrated responsibilities: CLI parsing, configuration persistence, shell completion generation, AWS SigV4 HTTP access, the four AWS API protocols, the credential chain, and the JSON-driven resource registry with list, describe, and action dispatch. These responsibilities MUST also be reachable non-interactively, as described under "Non-interactive resource access".
- The migration covers the CLI entrypoint and completion (`src/main.rs`, `src/completion.rs`), configuration (`src/config.rs`), the AWS layer (`src/aws/`), the resource layer (`src/resource/`), and `Cargo.toml`.
- The interactive TUI is out of scope. The ratatui/crossterm layout rendering, keyboard event handling, and terminal lifecycle (raw mode, alternate screen, cursor) used by `src/ui/`, `src/app.rs`, and `src/event.rs` do not need to be preserved or ported.

## Public contract to preserve

### Command-line interface

- The executable MUST be named `taws` and MUST keep the same options:
  - `--profile` / `-p`: AWS profile name.
  - `--region` / `-r`: AWS region.
  - `--log-level`: one of `off`, `error`, `warn`, `info`, `debug`, `trace`; default `off`. A value outside this set MUST be rejected: `taws` MUST exit with status code `2` and MUST report the allowed values.
  - `--readonly`: run in read-only mode.
  - `--endpoint-url`: custom AWS endpoint URL; when omitted, `AWS_ENDPOINT_URL` is used.
  - `--help` / `-h` and `--version` / `-V`.
- The subcommands MUST remain `completion <shell>` (`bash`, `zsh`, `fish`, `powershell`, with other values falling back to the parser default), plus the hidden `list-profiles` and `list-regions` subcommands that print one entry per line.
- The generated completion script text does not need to match character-for-character; the subcommand and argument parsing contract does.

### Configuration

- Preferences are stored as YAML. The path is the XDG config directory `taws/config.yaml`, falling back to `~/.taws/config.yaml`, then `.taws/config.yaml`.
- The YAML keys `profile`, `region`, `last_resource`, and `recently_used_regions` MUST keep their names and types. `recently_used_regions` is most-recent-first with a maximum of 6 entries, and missing fields default to unset or empty.
- Loading MUST NOT panic. A read or parse failure MUST log a warning and fall back to defaults.
- Effective profile resolution is `AWS_PROFILE`, then the stored `profile`, then `default`.
- Effective region resolution is `AWS_REGION`, then `AWS_DEFAULT_REGION`, then the stored `region`, then `us-east-1`.
- Adding a recently used region MUST de-duplicate, place the region first, and truncate the list to 6.

### Resource catalog

- The catalog is data-driven. The Go implementation MUST load and interpret the existing `src/resources/*.json` files with the current schema, including `resources`, `color_maps`, and the fields `display_name`, `service`, `sdk_method`, `sdk_method_params`, `response_path`, `id_field`, `name_field`, `is_global`, `columns`, `sub_resources`, `actions`, `detail_sdk_method`, `detail_sdk_method_params`, `api_config`, `field_mappings`, `action_configs`, `describe_config`, `filters_config`, `requires_parent`, `sort_field`, `sort_order`, and `preserve_order`.
- Resource keys MUST be preserved, and resources marked `requires_parent` MUST be excluded from the directly addressable resource keys.
- The schema and the content of `src/resources/*.json` MUST NOT be changed.

### AWS protocols

- The four protocols MUST be supported and driven by `api_config`, `action_configs`, and `describe_config`: `query` (action and version query parameters with XML responses), `json` (JSON body with the `X-Amz-Target` header), `rest-json`, and `rest-xml`.
- Request paths, HTTP methods, templates, static parameters, and body templates MUST be interpreted as configured.
- The pagination contract (`input_token`, `output_token`, `max_results_param`, `max_results`) MUST load subsequent pages from the returned output token and reset or advance correctly.

### Credentials

- The credential chain MUST resolve sources in this order: environment variables `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN`, AWS SSO (IAM Identity Center), console login (`login_session`), role assumption (`role_arn` with `source_profile` or `credential_source`), the shared credentials file, the config file, and IMDSv2.
- The overrides `AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE`, `AWS_LOGIN_CACHE_DIRECTORY`, `AWS_CA_BUNDLE`, and `SSL_CERT_FILE` MUST keep their current meanings.
- `list-profiles` MUST combine profiles from the credentials and config files, and `list-regions` MUST return the configured region list.

### Resource operations

- The list, describe, and action operations MUST keep their observable behavior: listing returns a page of items plus an optional next token; describing returns the full details of one resource; actions execute the configured write operation; the read actions `get_secret_value` and `get_parameter` return the retrieved value.

### Non-interactive resource access

- The resource operations MUST also be reachable non-interactively, through two hidden subcommands that a script can drive:
  - `taws fetch <resource-key> [--page-token TOKEN] [--filter NAME=VALUE]...` performs the list operation configured for `<resource-key>` and prints a single JSON object to stdout: `{"items": [...], "next_token": "<token>"}`. `items` is the requested page of results and `next_token` is the pagination output token for that page, or the empty string when the operation returns none. The command exits `0` on success and non-zero on failure.
  - `taws act <resource-key> <action-id> <resource-id>` executes the named action configured for `<resource-key>` and prints `{"ok": true}` on success. `<action-id>` is the identifier the resource's `action_configs` are keyed by (not the interactive shortcut). When `--readonly` is set the action MUST NOT be sent to AWS: the command MUST report an error and exit non-zero.
- Both subcommands use the same profile and region resolution, credential chain, endpoint override (`--endpoint-url`, then `AWS_ENDPOINT_URL`), SigV4 request signing, and the four AWS API protocols as the rest of the migration.
- Both subcommands are hidden from `--help`.

## Behavior requirements

- If the configuration file is missing, unreadable, or invalid, `taws` MUST log a warning and continue with default preferences.
- If `--readonly` is set, write operations MUST NOT be sent to AWS.
- If `--endpoint-url` is set or `AWS_ENDPOINT_URL` is present, service requests MUST target that endpoint, and `AWS_ENDPOINT_URL` MUST also override the STS AssumeRole endpoint.
- When a paginated list returns an output token, the next page MUST be loadable with that token, and `max_results` MUST apply when configured.
- Credential resolution MUST follow the documented order, and a profile configured for SSO or console login MUST report that login requirement instead of silently using unrelated static credentials.
- When an `aws ssm start-session` subprocess exits non-zero, `taws` MUST report it with the message text `SSM session exited with code: <code>` followed by the numeric exit code, and when the code is `254` it MUST additionally indicate that the instance is not connected to the SSM Agent.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code within the migration scope MUST NOT call into the Rust implementation to perform the migrated responsibilities.
3. Production code within the migration scope MUST NOT delegate the migrated responsibilities to the Rust runtime through subprocesses, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to the in-scope modules, the build and dependency configuration (for example, replacing `Cargo.toml` with a Go module), and supporting assets.
5. The `src/resources/*.json` files MUST NOT be modified in schema or content.
6. Retained Rust TUI code MUST NOT be used to satisfy any migrated responsibility.
7. External commands the original behavior already invokes (`aws ssm start-session`, `aws login`, and `credential_process`) MAY remain external, but MUST NOT be replaced by calls into the Rust implementation.
8. Ported or adapted public tests MAY change language, but their behavioral assertions MUST be preserved; checks, assertions, and comparisons MUST NOT be removed or weakened, and evaluation inputs MUST NOT be hardcoded to fake compliance.
9. Hidden evaluation assets MUST NOT be modified.
10. Work from the repository working directory `/workspace/repo`.
11. All migration changes MUST remain in the repository working tree; the result is collected as a patch of the repository against the task's baseline commit.

## Validation

From `/workspace/repo`, build the Go module with the Go 1.23.0 toolchain using `go build ./...`, and run its tests with `go test ./...`. These checks confirm that the migrated code compiles and that exposed tests pass; they do not by themselves prove every behavior requirement. Driving the non-interactive subcommands against a local mock endpoint confirms the protocol and signing behavior.

The migration baseline of this task is base commit d1cbf6e450eced9a7947cf810764ec79599c7793.
