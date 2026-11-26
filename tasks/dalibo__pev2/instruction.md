# Migrate pev2 plan parsing and metric computation from TypeScript to Rust

## Background

pev2 is a Vue 3 library that renders a graphical view of PostgreSQL `EXPLAIN` plans. The plan parsing, source cleaning, node construction, and derived-metric logic currently lives in TypeScript in `src/services/plan-service.ts`, `src/services/help-service.ts`, `src/enums.ts`, and `src/interfaces.ts`. The rendering layer consumes the structures these modules produce.

## Goal

- Reimplement in Rust (`1.83.0`) the responsibilities currently provided by `src/services/plan-service.ts`, `src/services/help-service.ts`, `src/enums.ts`, and `src/interfaces.ts` (TypeScript `5.9.3`).
- The Rust implementation MUST provide plan parsing for text and JSON input, source cleanup and line splitting, node and node-type construction, and the plan-level fields and derived metrics, matching the existing TypeScript implementation.
- The Rust implementation MUST expose the public entry points listed under "Public contract to preserve".
- The following are out of scope and need not be migrated: the Vue single-file components and example SPA (`src/components/`, `example/`), browser persistence (the `example/src/idb.ts` IndexedDB store and `localStorage`), the DOM scroll helpers `scrollChildIntoParentView` and `smoothScroll`, the d3/d3-flextree layout and the color/theme rendering, the `NODE_DESCRIPTIONS` and `HELP_MESSAGES` display wording, and the Playwright visual/E2E behavior.

## Public contract to preserve

- `PlanService.fromSource(source: string): IPlanContent` MUST remain a public entry point. It MUST accept `EXPLAIN` output as text or as JSON and return parsed plan content. It MUST fail with an error when the input cannot be parsed.
- `PlanService.createPlan(planName: string, planContent: IPlanContent, planQuery: string): IPlan` MUST remain a public entry point. It MUST normalize whitespace in `planQuery` and MUST compute the plan-level fields and derived metrics below.
- `PlanService.fromText(text)` MUST build the plan content and node tree from text input.
- `PlanService.fromJson(source)` MUST build the plan content from JSON embedded in text.
- `PlanService.cleanupSource(source)` and `PlanService.splitIntoLines(text)` MUST preserve their text-cleaning and line-splitting contracts.
- The `Property` string values MUST remain the output property keys for parsed values. Examples include `"Actual Loops"`, `"Actual Rows"`, `"Total Cost"`, and `"Plan Rows"`.
- The `Node` constructor MUST map node-type strings to a `Node Type` value and the related structured fields. Examples of node-type strings are `"Index Scan using ... on ..."`, `"Nested Loop"`, and `"HashAggregate"`.
- `splitBalanced(input, split)` MUST split `input` on `split` while keeping bracketed and quoted regions intact.
- The exported helpers `findNodeById`, `findNodeBySubplanName`, and `shouldShowProp` are within the migration scope and MUST be preserved.
- The parse result MUST be a JSON-serializable structure with the same nested shape and the same key strings as the TypeScript structures. This shape includes the nested `Plan`/`Plans` node objects, `Workers`, and the global fields `JIT`, `Planning`, `Serialization`, `Triggers`, and `Query Text`.

## Rust target contract

The TypeScript entry points above are delivered as a native Cargo library that the evaluation links against. The repository MUST be a Cargo workspace whose root manifest is `Cargo.toml`, and it MUST contain a library package named `pev2-core` located at `crates/pev2-core`, importable from Rust as `pev2_core`. The crate MUST build with `rustc` 1.83.0 for the default (non-`wasm32`) target.

The crate MUST expose at least the following public items with these names and signatures:

```rust
pub type JsonMap = serde_json::Map<String, serde_json::Value>;
pub type PlanContent = JsonMap;
pub type Plan = JsonMap;
pub type Node = JsonMap;

pub fn from_source(source: &str) -> Result<PlanContent, String>;
pub fn from_text(text: &str) -> Result<PlanContent, String>;
pub fn from_json(source: &str) -> Result<PlanContent, String>;
pub fn get_plan_content(value: &serde_json::Value) -> Result<PlanContent, String>;
pub fn cleanup_source(source: &str) -> String;
pub fn split_into_lines(text: &str) -> Vec<String>;
pub fn split_balanced(input: &str, split: &str) -> Vec<String>;
pub fn create_plan(plan_name: &str, plan_content: PlanContent, plan_query: &str) -> Result<Plan, String>;
pub fn parse_node_type(node_type: &str) -> Node;
pub fn should_show_prop(key: &str, value: &serde_json::Value) -> bool;
pub fn find_node_by_id(plan: &Plan, id: i64) -> Option<Node>;
pub fn find_node_by_subplan_name(plan: &Plan, subplan_name: &str) -> Option<Node>;
```

- `PlanContent` serializes to a JSON object whose root plan node is the `"Plan"` member; the global fields (`"JIT"`, `"Planning"`, `"Serialization"`, `"Triggers"`, `"Query Text"`) sit beside it.
- `Plan` serializes to a JSON object with the plan content under `"content"` and the plan-level fields (`"isAnalyze"`, `"isVerbose"`, `"ctes"`, `"query"`) beside it.
- `Node` values are JSON objects keyed by the property strings above (for example `"Node Type"` and `"Join Type"`).
- `from_text` returns `Err("Unable to parse plan")` when no root `Plan` can be derived, and `get_plan_content` returns `Err("Invalid plan")` when the payload has no `Plan` member.

The workspace MUST be buildable fully offline against the crate registry already provided by the evaluation image. Depend only on crates available there (`serde` and `serde_json` are provided and are required by the contract types above) and commit a `Cargo.lock` that resolves entirely within that registry.

## Behavior requirements

- The observable behavior of every migrated entry point MUST match the existing TypeScript implementation (`src/services/plan-service.ts`, `src/services/help-service.ts`, `src/enums.ts`, `src/interfaces.ts`) exactly. Reproduce its results; do not approximate or invent behavior.
- `fromSource` MUST accept `EXPLAIN` output in both text and JSON form and MUST return the same plan content as the TypeScript implementation, or the same error when the input cannot be parsed.
- `fromText`, `fromJson`, `cleanupSource`, and `splitIntoLines` MUST produce the same output as their TypeScript counterparts for every input the TypeScript implementation accepts.
- The node tree, the node-type parsing, and the per-node properties (including `Parent Relationship`, never-executed nodes, `InitPlan`/`SubPlan` and `CTE` handling, `Worker`, `JIT`, `Planning`, `Serialization`, `Buffers`, `I/O Timings`, sort/group/hash keys, memory, and cache information) MUST match the TypeScript `Node` constructor.
- `createPlan` MUST set `isAnalyze`, `isVerbose`, `ctes`, and `query`, assign node identifiers, and compute the plan-level metrics `maxRows`, `maxCost`, `maxTotalCost`, `maxDuration`, `maxBlocks`, `maxIo`, and `maxEstimateFactor` with the same values the TypeScript implementation produces.
- Every derived per-node field MUST match the TypeScript implementation exactly, including the exclusive duration and cost, the loop- and worker-adjusted actual values, the planner row estimate factor and its direction, the exclusive block and I/O aggregates, and the `CTE`/`InitPlan` usage corrections.
- The output property key strings MUST equal the TypeScript `Property` values.
- Where the TypeScript implementation raises an error, the Rust implementation MUST return an `Err` with the same message (in particular the two messages stated above).

## Constraints

1. The migration MUST NOT remove the required public capabilities or change their contracts outside the stated goal.
2. Production Rust code within the migration scope MUST implement the parsing and metric responsibilities natively.
3. Production Rust code within the migration scope MUST NOT delegate those responsibilities to a Node.js or TypeScript runtime through subprocesses, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to the Rust implementation of the in-scope responsibilities and its build and dependency configuration.
5. TypeScript, Vue, and third-party dependencies outside the migration scope MAY remain in the repository.
6. The existing public tests and fixtures MUST NOT be modified, deleted, or weakened. These protected categories include the Vitest specs under `src/services/__tests__/` and `src/__tests__/`, the `from-text` plan/expect fixtures, and the Playwright snapshots under `tests/e2e/`.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. All work MUST be performed inside the `/workspace/repo` working directory, and the deliverable is the resulting repository content. Preserve the task starting point; do not substitute another branch or commit for it.

The migration baseline of this task is base commit 6d14a4f65b7400b9a877d6ec583f07f364f2267d.
