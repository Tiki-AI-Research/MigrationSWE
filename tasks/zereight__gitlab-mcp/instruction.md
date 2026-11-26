# Migrate the OMG MCP server from TypeScript to Python

## Background

`mcp-server/` is a standalone Model Context Protocol (MCP) server named `omg-mcp-server`. It reports itself to clients as `omg-workflow` version `1.0.0` and runs over stdio. It provides tools for workflow state, PRDs, autopilot phases, project memory, and model recommendation, and it persists data under `.omc/`. The sub-project is compiled and tested independently from the repository root.

The root `@zereight/mcp-gitlab` GitLab server is a separate component and is not part of this migration.

## Goal

- Migrate the `mcp-server` sub-project from TypeScript 5.9.3 to Python 3.13.0.
- Migrated sources: `mcp-server/src/index.ts`, `mcp-server/src/state-tools.ts`, `mcp-server/src/prd-tools.ts`, `mcp-server/src/workflow-tools.ts`, `mcp-server/src/memory-tools.ts`, `mcp-server/src/model-router.ts`, `mcp-server/src/utils.ts`, and `mcp-server/package.json`.
- The Python implementation MUST be a real replacement that runs as a stdio MCP server and MUST fulfill every migrated responsibility on its own.
- The migrated server MUST be an importable Python package rooted at `mcp-server/omg_mcp/` (import name `omg_mcp`) and MUST run as `python -m omg_mcp` with `mcp-server` importable. The public test suite MUST be ported into `mcp-server/tests/` as a `unittest`-discoverable package so that `python -m unittest discover -s mcp-server/tests -t mcp-server` runs it.
- Out of scope and MUST remain unchanged: the root `@zereight/mcp-gitlab` TypeScript server (`index.ts`, `tools/`, `schemas.ts`, `stateless/`, and the rest of the root project), the MCP wire protocol, the `.omc` file locations and JSON field shapes, and the feature set.
- The migration MUST NOT add new features or concurrency control. The current unlocked read-modify-write semantics MUST be preserved.

## Public contract to preserve

- The server MUST register the same MCP tools with identical input schemas: `omg_read_state`, `omg_write_state`, `omg_clear_state`, `omg_list_active`, `omg_read_prd`, `omg_create_prd`, `omg_update_story`, `omg_verify_story`, `omg_next_phase`, `omg_check_completion`, `omg_get_phase_info`, `omg_read_memory`, `omg_write_memory`, `omg_delete_memory`, `omg_select_model`.
- Tool results MUST keep the MCP envelope `{content:[{type:"text",text:<JSON string>}]}`. Tool-layer failures MUST keep `{content:[{type:"text",text:<JSON string of {"success":false,"error":<message>}}]}`.
- The server MUST identify itself as `omg-workflow` version `1.0.0` over stdio. On startup failure it MUST print `OMG MCP Server failed to start: <error>` and exit with code 1.
- Persistent layout MUST stay `.omc/state/<mode>-state.json`, `.omc/prd.json`, and `.omc/project-memory.json`. The workspace root MUST resolve from the `WORKSPACE_ROOT` environment variable when set, otherwise the current working directory.
- The persisted JSON field shapes MUST be preserved: state objects keep the caller-supplied keys plus `updated_at`, `current_phase`, `phase_name`, `phase_started_at`, `active`, `completed`, `completed_at`, and `skip_reason` where applicable; PRD stories keep `id`, `title`, `description`, `acceptance_criteria`, `passes`, optional `verification_evidence`, and `priority`; memory entries keep `key`, `value`, `category`, `created_at`, and `updated_at`.

## Behavior that must stay unchanged

State tools:

- `omg_read_state` MUST return the stored JSON text for a valid mode with an existing state file. When no file exists it MUST return `{"active":false,"mode":<mode>,"message":"No state found"}`. An invalid mode MUST return the error envelope.
- `omg_write_state` MUST merge the incoming JSON object over any existing state, set `updated_at` to the current ISO-8601 time, persist the merged object, and return `{"success":true,"mode":<mode>,"state":<merged>}`. Unparsable existing state MUST be treated as empty and overwritten. Input that is invalid JSON or not a JSON object MUST return the error envelope.
- `omg_clear_state` MUST return `{"success":true,"message":"No state directory"}` when no state directory exists. With a mode it MUST delete `<mode>-state.json` and return `{"success":true,"cleared":<mode>}`. With no mode it MUST delete every `*-state.json` file and return `{"success":true,"cleared":"all","files":<count>}`.
- `omg_list_active` MUST return `{"active_modes":[...]}` with one entry per state file whose persisted `active` is truthy, each carrying `mode` and optional `phase` (the string form of `current_phase`, else `phase`) and `updated_at`. A missing directory MUST yield an empty list.

PRD tools:

- `omg_read_prd` MUST return `{"exists":false,"message":"No PRD found. Create one with ralph or omg-autopilot."}` when no PRD exists. Otherwise it MUST return a summary with `title`, `total_stories`, `passing`, `failing`, and per-story `id`, `title`, `passes`, `priority`, and `criteria_count`.
- `omg_create_prd` MUST accept `title`, optional `description` (default empty string), and optional `stories`. Each story MUST default `id` to `story-<8-char id>` when omitted, `description` to empty string, `acceptance_criteria` to an empty list, `passes` to false, and `priority` to 0. It MUST persist and return `{"success":true,"title":<title>,"stories_created":<count>}`.
- `omg_update_story` MUST require `story_id` and `passes`, accepting optional `evidence`. With no PRD it MUST return `{"success":false,"error":"No PRD found"}`. For an unknown id it MUST return `{"success":false,"error":"Story <story_id> not found"}`. On success it MUST set `passes`, store `verification_evidence` when evidence is provided, and return `{"success":true,"story_id":<id>,"passes":<bool>,"total_passing":<n>,"total_stories":<n>}`.
- `omg_verify_story` MUST return `{"exists":false,"message":"No PRD found"}` when no PRD exists. It MUST select the highest-priority story whose `passes` is false. When none are pending it MUST return `{"all_passing":true,"message":"All stories pass. PRD is complete.","total_stories":<n>}`. Otherwise it MUST return `{"story":{"id","title","description","acceptance_criteria","priority"},"remaining":<n>,"total":<n>}`.

Workflow tools:

- The six autopilot phases MUST match the source: 0 `expansion`, 1 `planning`, 2 `execution`, 3 `qa`, 4 `validation`, 5 `cleanup`, each with its existing description.
- `omg_next_phase` MUST require `current_phase` and accept optional `skip_reason`. It MUST return `{"success":false,"error":"No omg-autopilot state found. Start with /omg-autopilot."}` when no `omg-autopilot` state exists, and a corrupted-state error when the state cannot be parsed. It MUST enforce that `current_phase` equals the persisted `current_phase` (default 0 when absent); a mismatch MUST return an error naming both phases. It MUST advance the persisted workflow to the next phase in sequence. When the workflow is already at its final phase, it MUST instead set `active` false, `completed` true, `completed_at`, and `current_phase` 5, persist, and return `{"success":true,"completed":true,"message":"OMG Autopilot workflow complete."}`. Otherwise it MUST set `current_phase`, `phase_name`, `phase_started_at`, `updated_at`, and `skip_reason` when provided, persist, and return `{"success":true,"phase":<n>,"name":<name>,"description":<description>}`.
- `omg_check_completion` MUST return `{"allow_stop":true,"remaining":[],"message":"No active state"}` when no state directory exists. For each active state it MUST append: `omg-autopilot: phase <n>/5 (<name>)` while phase < 5; `ralph: persistence loop active`; `team: <current_phase or "active">`; `ultraqa: cycle <current_cycle or "?">/5`; and `<mode>: active` for other modes. It MUST return `allow_stop` true when nothing remains and the message `All tasks complete. Safe to stop.`, otherwise the message `<count> active workflow(s). Continue working.`.
- `omg_get_phase_info` MUST return `{"error":"Invalid phase: <n>. Valid: 0-5."}` for an invalid phase, the single phase object for a valid phase, and `{"phases":[...]}` when no phase is provided.

Memory tools:

- `omg_read_memory` MUST accept optional `category` and `key` filters and return `{"total":<n>,"entries":[{"key","value","category","updated_at"}]}`.
- `omg_write_memory` MUST require `key` and `value` and accept `category` defaulting to `"project"`. When `value` exceeds 10000 characters it MUST return the error envelope stating the maximum length. A new key MUST be rejected with the error envelope when the store already holds 500 entries. Writing an existing key MUST update it and return `{"success":true,"key":<key>,"action":"updated"}`; a new key MUST return `action` `"created"`.
- `omg_delete_memory` MUST remove the entry for `key` and return `{"success":true,"deleted":<bool>,"key":<key>}`.

Model router:

- `omg_select_model` MUST require `task` and accept optional `file_count` and `agent_type`, and MUST return `{"recommended_model","tier","reason","alternatives","complexity":{"tier","score","factors"}}`.
- Complexity scoring MUST reproduce the source implementation's assessment exactly: for a given `task`, `file_count`, and `agent_type`, the returned `score`, `tier`, and `factors` MUST equal what the source computes. The `tier` MUST be one of `high`, `medium`, or `low`.
- Agent-type overrides MUST be honored after the complexity assessment: the source's documented `agent_type` values override the recommended tier, the override reason replaces the default reason, and `complexity` still reflects the computed assessment.
- Recommendations MUST be: high `claude-sonnet-4.6` (tier `HIGH`, alternatives `["gpt-4.1"]`); medium `gpt-4.1` (tier `MEDIUM`, alternatives `["claude-sonnet-4.6"]`); low `gpt-4.1-mini` (tier `LOW`, alternatives `["gpt-4.1"]`).

Shared safety and path helpers:

- Mode names MUST be accepted only when they match `[a-zA-Z0-9_-]+`; any other value MUST raise `Invalid mode name: "<mode>". Only alphanumeric, hyphens, and underscores allowed.` (`__proto__` is accepted because it matches the pattern).
- Reading a symlink or a file larger than 5 MiB (5 * 1024 * 1024 bytes) MUST yield no content, as MUST a missing or unreadable file.
- Writing to an existing symlink MUST fail with `Refusing to write to symlink`. Creating a new file MUST succeed.
- JSON parsing MUST accept only JSON objects; arrays, primitives, and `null` MUST be rejected. Accepted objects MUST have the keys `__proto__`, `constructor`, and `prototype` removed.
- Generated identifiers MUST be 8 characters derived from a random UUID.

## constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code within `mcp-server/` MUST NOT import, call, or execute the TypeScript sources or the Node.js runtime to serve any migrated responsibility.
3. Production code within `mcp-server/` MUST NOT delegate migrated responsibilities to the original TypeScript server through subprocesses, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to the `mcp-server/` sub-project: Python sources, dependency and packaging configuration, and supporting assets. The root project, its build, and its tests MUST NOT be modified.
5. Source-framework dependencies outside the migration scope, including the root project's TypeScript and npm dependencies, MAY remain.
6. The MCP tool names, input schemas, the text-JSON result envelope, the `.omc` layout and field shapes, and the stdio server identity MUST be treated as protected public artifacts.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. The project's existing test suite MUST be ported to `mcp-server/tests/` as a `unittest`-discoverable package, and its behavioral assertions (mode validation, JSON safety, id generation, file-safety behavior, and `.omc` state paths) MUST be preserved. Ported tests MUST isolate their temporary files in a unique temporary directory and remove it afterward. The migrated package MUST NOT depend on the original TypeScript test entry point at runtime.
9. The working directory is `/workspace/repo`; the migrated sub-project lives at `/workspace/repo/mcp-server`.
10. Leave all migration changes as files in the repository working tree under `mcp-server/` so the full change set can be captured as a diff against the starting commit.

IMPORTANT: Create a new branch from the task's base commit ee18073a11e04534911bd2900e9879ababed3367 and commit all task changes, including new files, when you are done.
