# Migrate `@stepperize/core` from TypeScript to Rust

## Background

`@stepperize/core` is the framework-agnostic core package under `packages/core`. Its runtime utilities live in `packages/core/src/utils.ts`. Its public types live in `packages/core/src/types.ts`. Both are re-exported from `packages/core/src/index.ts`. The package has no runtime dependencies and only an optional `typescript` peer dependency. `packages/core/src/tests/utils.test.ts` documents the expected behavior of the utilities.

## Goal

- Migrate the runtime utilities and the public data types of `@stepperize/core` from TypeScript 6.0.3 to Rust 1.83.0.
- The result MUST be a native Rust library crate inside this repository.
- The Rust crate MUST implement the migrated responsibilities itself: step-map construction, initial-index resolution, step-id parsing, initial flow-data copying, schema-driven validation, positional status derivation, and exhaustive step matching.
- The Rust public API MUST represent the package data model. This includes `Step`, `FlowData`, `StepMap`, `ValidationResult`, `StepStatus`, and the Standard Schema v1 validation boundary.
- Represent the exported type contracts of `packages/core/src/types.ts` in Rust where the target language allows. This includes `Get.Id`, `Get.StepById`, `StepMap`, `Stepper`, `StepChangeContext`, and `StandardSchemaV1`.
- TypeScript-only type-level mechanisms may be redesigned for Rust. Their runtime-observable behavior MUST be preserved.
- Out of scope: the React components, primitives, and DOM/ARIA behavior under `packages/react`, and the documentation site, registry, SEO/OG, and generated artifacts under `apps/docs`.

## Public contract to preserve

- The package exports these named operations: `createStepMap`, `getInitialStepIndex`, `parseStep`, `getInitialData`, `validateStep`, `getStepStatus`, `getStepStatuses`, and `matchStep`. The Rust crate MUST expose an equivalent public operation for each.
- A step is identified by a stable string `id`. Every other step field is user data. Step lookup and flow data are keyed by that id.
- `StepMap` exposes the step list, the ids in step order, `get`, `at`, `indexOf`, `has`, `first`, `last`, `next`, `prev`, and `neighbors`.
- `FlowData` maps each step id to that step's data.
- The Standard Schema v1 boundary is keyed by `~standard`. Its `validate(value)` returns either a success `value` or `issues`. It MAY return synchronously or asynchronously.
- `ValidationResult` is either a success carrying the validated value or a failure carrying the issues.
- `StepStatus` is one of `active`, `previous`, or `upcoming`.
- Unknown step ids produce the message `Step "<id>" not found.`. A missing match handler produces `No match handler found for step "<id>".`. Preserve this wording.

## Required Rust public interface

The evaluator compiles its own behaviour tests against the migrated crate, so the library root MUST re-export the following public items with these Rust names and signatures. This fixes the target surface only; internal representation and any additional helpers are free.

```rust
use std::any::Any;
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;

pub const STANDARD_SCHEMA_V1_VERSION: u8 = 1;

/// A step definition. Implementable by downstream code; only `id` is required.
pub trait Step {
    fn id(&self) -> &str;
    fn schema(&self) -> Option<&dyn StandardSchemaV1> { None }
}

/// Standard Schema v1 boundary. `validate` returns a boxed future so a
/// validator may resolve synchronously or asynchronously.
pub trait StandardSchemaV1 {
    fn version(&self) -> u8 { STANDARD_SCHEMA_V1_VERSION }
    fn vendor(&self) -> &str;
    fn validate(
        &self,
        value: Box<dyn Any>,
    ) -> Pin<Box<dyn Future<Output = Result<Box<dyn Any>, Vec<Issue>>> + '_>>;
}

/// A single validation issue.
pub struct Issue {
    pub message: String,
    pub path: Option<Vec<PathSegment>>,
}
impl Issue {
    pub fn new(message: impl Into<String>) -> Self;
}

/// A path entry inside a validation issue.
pub enum PathSegment { Key(String), Index(usize) }

/// Success carries the validated value; failure carries the issues.
pub enum ValidationResult<T> {
    Success { data: T },
    Failure { issues: Vec<Issue> },
}
impl<T> ValidationResult<T> {
    pub fn success(data: T) -> Self;
    pub fn failure(issues: Vec<Issue>) -> Self;
    pub fn is_success(&self) -> bool;
    pub fn is_failure(&self) -> bool;
    pub fn data(self) -> Option<T>;
    pub fn issues(self) -> Option<Vec<Issue>>;
}

/// Resolve/match error. `Display` MUST render exactly the protected wording;
/// the type MUST be `Debug + Clone + PartialEq + Eq + std::error::Error`.
pub enum StepError {
    NotFound { id: String },
    NoHandler { id: String },
}
impl StepError {
    pub fn not_found(id: impl Into<String>) -> Self;
    pub fn no_handler(id: impl Into<String>) -> Self;
}

/// Positional status. MUST be `Debug + Clone + Copy + PartialEq + Eq`.
pub enum StepStatus { Active, Previous, Upcoming }
// `as_str()` -> "active" | "previous" | "upcoming"; also implements Display.

/// Direction of a step change.
pub enum StepDirection { Next, Prev, Goto, Reset }

/// Both adjacent steps; each side is None at the boundaries.
pub struct Neighbors<'a, S> {
    pub prev: Option<&'a S>,
    pub next: Option<&'a S>,
}

/// Pure step accessors, generic over the concrete step type.
pub struct StepMap<S: Step> { /* private */ }
impl<S: Step> StepMap<S> {
    pub fn new(steps: Vec<S>) -> Self;
    pub fn steps(&self) -> &[S];
    pub fn ids(&self) -> Vec<&str>;
    pub fn get(&self, id: &str) -> Option<&S>;
    pub fn at(&self, index: usize) -> Option<&S>;
    pub fn index_of(&self, id: &str) -> isize;
    pub fn has(&self, id: &str) -> bool;
    pub fn first(&self) -> Option<&S>;
    pub fn last(&self) -> Option<&S>;
    pub fn next(&self, id: &str) -> Option<&S>;
    pub fn prev(&self, id: &str) -> Option<&S>;
    pub fn neighbors(&self, id: &str) -> Neighbors<'_, S>;
}

pub type FlowData<D> = HashMap<String, D>;
pub type GetId = String;
pub type MatchHandlers<S, R> = HashMap<String, Box<dyn Fn(&S) -> R>>;

/// Redesign of the type-level `Get.StepById` lookup.
pub trait GetStepById {
    type Item;
    fn step_by_id(&self, id: &str) -> Option<&Self::Item>;
}
impl<S: Step> GetStepById for [S] { type Item = S; /* ... */ }

pub struct StepChangeContext {
    pub from: String,
    pub to: String,
    pub from_index: isize,
    pub to_index: isize,
    pub direction: StepDirection,
    pub statuses: HashMap<String, StepStatus>,
}

pub struct Stepper<S: Step> { /* private */ }
impl<S: Step> Stepper<S> {
    pub fn new(steps: Vec<S>, current_index: usize) -> Self;
    pub fn id(&self) -> Option<&str>;
    // Additional queries/state helpers are allowed.
}

pub fn create_step_map<S: Step>(steps: Vec<S>) -> StepMap<S>;
pub fn get_initial_step_index<S: Step>(steps: &[S], initial: Option<&str>) -> usize;
pub fn parse_step<'a, S: Step>(steps: &'a [S], value: &dyn Any) -> Option<&'a str>;
pub fn get_initial_data<D: Clone>(data: Option<&FlowData<D>>) -> FlowData<D>;
pub async fn validate_step<S: Step, D: Any>(
    steps: &[S],
    id: &str,
    value: D,
) -> Result<ValidationResult<Box<dyn Any>>, StepError>;
pub fn get_step_status<S: Step>(steps: &[S], current_index: isize, id: &str) -> StepStatus;
pub fn get_step_statuses<S: Step>(
    steps: &[S],
    current_index: isize,
) -> HashMap<String, StepStatus>;
pub fn match_step<S: Step, R>(
    steps: &[S],
    id: &str,
    handlers: &MatchHandlers<S, R>,
) -> Result<R, StepError>;
```

## Behavior requirements

- `createStepMap(steps)` MUST expose `ids` in the same order as the input steps.
- `createStepMap(steps).get(id)` MUST return the matching step, or no value when no step matches.
- `createStepMap(steps).at(index)` MUST return the step at that index, or no value when the index is out of range.
- `createStepMap(steps).indexOf(id)` MUST return the matching index, or `-1` when no step matches.
- `createStepMap(steps).has(id)` MUST report whether a step with that id exists.
- `createStepMap(steps).first()` and `.last()` MUST return the first and last steps.
- `createStepMap(steps).next(id)`, `.prev(id)`, and `.neighbors(id)` MUST return the adjacent step, or no value at the first and last positions.
- `getInitialStepIndex(steps, initial)` MUST return the index of `initial`, and MUST return `0` when `initial` is absent or matches no step.
- `parseStep(steps, value)` MUST return `value` only when it equals a known step id. Otherwise it MUST return no value, including for unknown, null, missing, or non-string values.
- `getInitialData(data)` MUST return an independent shallow copy of `data`. Mutating the result MUST NOT affect the input. It MUST return an empty map when `data` is absent.
- `validateStep(steps, id, value)` MUST return success with `value` unchanged when the step has no schema.
- `validateStep(steps, id, value)` MUST return success with the schema's validated value when the schema accepts the value.
- `validateStep(steps, id, value)` MUST return failure with the schema's issues when the schema rejects the value.
- `validateStep(steps, id, value)` MUST fail with `Step "<id>" not found.` when no step matches `id`.
- `validateStep` MUST support schema validators that return their result directly or asynchronously.
- `getStepStatus(steps, currentIndex, id)` MUST return `active` when the step index equals `currentIndex`, `previous` when the step index is below `currentIndex`, and `upcoming` otherwise.
- `getStepStatuses(steps, currentIndex)` MUST return the status of every step id.
- `matchStep(steps, id, handlers)` MUST invoke the handler registered for `id` and return its result.
- `matchStep(steps, id, handlers)` MUST fail with `Step "<id>" not found.` when no step matches `id`.
- `matchStep(steps, id, handlers)` MUST fail with `No match handler found for step "<id>".` when the step exists without a handler.
- The utilities MUST NOT initiate network, filesystem, or persistence operations of their own. They MUST invoke a step's schema validator and the selected `matchStep` handler when required by the contracts above. Side effects performed by caller-supplied validators or match handlers are permitted and are not attributed to the utilities for this restriction; callback invocation, return values, and errors MUST retain the behavior specified above.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production Rust code MUST NOT call the existing TypeScript or Node.js implementation of the migrated responsibilities.
3. The migrated responsibilities MUST NOT be delegated to the original runtime through subprocesses, FFI, RPC, or dynamic loading.
4. Changes MUST be limited to the migration scope and the supporting Rust crate and dependency configuration inside the repository.
5. The out-of-scope packages `packages/react` and `apps/docs` MAY keep their existing TypeScript and React dependencies. Do not change their behavior.
6. Protected public artifacts are the package's named exports, its error-message wording, and the Standard Schema v1 boundary. The behavioral reference `packages/core/src/tests/utils.test.ts` MUST NOT be altered to hide missing behavior.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. Work in `/workspace/repo`. The migration result is collected by diffing the repository working tree against the base commit. Keep all migration files, including the crate manifest, sources, and tests, inside the repository.

The migration baseline of this task is base commit f1b5bfafa5d585de47f6c70eeeb355b76b20ff24.
