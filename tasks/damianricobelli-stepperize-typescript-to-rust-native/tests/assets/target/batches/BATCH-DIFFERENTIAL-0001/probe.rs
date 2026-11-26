//! Differential target probe for BATCH-DIFFERENTIAL-0001.
//!
//! Consumes the shared case corpus (`shared/batches/BATCH-DIFFERENTIAL-0001/cases.json`)
//! against the native Rust `stepperize_core` crate and asserts the normalized
//! results. This file is executed against `/input/target` during authoring; the
//! observed results back the `target_expectation = PASS` declarations.

use std::any::Any;
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll, Wake, Waker};

use stepperize_core::{
    create_step_map, get_initial_data, get_initial_step_index, get_step_status, get_step_statuses,
    match_step, parse_step, validate_step, FlowData, Issue, StandardSchemaV1, Step, StepError,
    StepStatus, ValidationResult,
};

fn block_on<F: Future>(future: F) -> F::Output {
    struct NoopWaker;
    impl Wake for NoopWaker {
        fn wake(self: Arc<Self>) {}
    }
    let waker = Waker::from(Arc::new(NoopWaker));
    let mut context = Context::from_waker(&waker);
    let mut future = Box::pin(future);
    loop {
        match future.as_mut().poll(&mut context) {
            Poll::Ready(value) => return value,
            Poll::Pending => std::thread::yield_now(),
        }
    }
}

struct FixtureStep {
    id: &'static str,
    label: &'static str,
    schema: Option<Box<dyn StandardSchemaV1>>,
}

impl FixtureStep {
    fn new(id: &'static str, label: &'static str) -> Self {
        Self { id, label, schema: None }
    }
    fn with_schema(id: &'static str, schema: Box<dyn StandardSchemaV1>) -> Self {
        Self { id, label: "", schema: Some(schema) }
    }
}

impl Step for FixtureStep {
    fn id(&self) -> &str {
        self.id
    }
    fn schema(&self) -> Option<&dyn StandardSchemaV1> {
        self.schema.as_deref()
    }
}

fn steps() -> Vec<FixtureStep> {
    vec![
        FixtureStep::new("first", "Step 1"),
        FixtureStep::new("second", "Step 2"),
        FixtureStep::new("third", "Step 3"),
    ]
}

fn schema_steps() -> Vec<FixtureStep> {
    vec![
        FixtureStep::with_schema("name", Box::new(NonEmptyString)),
        FixtureStep::new("done", "Done"),
    ]
}

struct NonEmptyString;

impl StandardSchemaV1 for NonEmptyString {
    fn vendor(&self) -> &str {
        "test"
    }
    fn validate(
        &self,
        value: Box<dyn Any>,
    ) -> Pin<Box<dyn Future<Output = Result<Box<dyn Any>, Vec<Issue>>> + '_>> {
        Box::pin(async move {
            match value.downcast::<&'static str>() {
                Ok(text) if !text.is_empty() => Ok(text as Box<dyn Any>),
                _ => Err(vec![Issue::new("Required")]),
            }
        })
    }
}

#[test]
fn diff_002_step_map_ids_order() {
    assert_eq!(create_step_map(steps()).ids(), vec!["first", "second", "third"]);
}

#[test]
fn diff_003_step_map_get() {
    let map = create_step_map(steps());
    assert_eq!(map.get("second").unwrap().label, "Step 2");
    assert!(map.get("missing").is_none());
}

#[test]
fn diff_004_step_map_at() {
    let map = create_step_map(steps());
    assert_eq!(map.at(0).unwrap().id, "first");
    assert!(map.at(9).is_none());
}

#[test]
fn diff_005_step_map_index_of() {
    let map = create_step_map(steps());
    assert_eq!(map.index_of("third"), 2);
    assert_eq!(map.index_of("missing"), -1);
}

#[test]
fn diff_006_step_map_has() {
    let map = create_step_map(steps());
    assert!(map.has("second"));
    assert!(!map.has("missing"));
}

#[test]
fn diff_007_step_map_first_last() {
    let map = create_step_map(steps());
    assert_eq!(map.first().unwrap().id, "first");
    assert_eq!(map.last().unwrap().id, "third");
    let empty = create_step_map(Vec::<FixtureStep>::new());
    assert!(empty.first().is_none());
    assert!(empty.last().is_none());
}

#[test]
fn diff_008_step_map_next_prev_neighbors() {
    let map = create_step_map(steps());
    assert_eq!(map.next("first").unwrap().id, "second");
    assert_eq!(map.prev("third").unwrap().id, "second");
    assert!(map.next("missing").is_none());
    assert!(map.prev("missing").is_none());

    let middle = map.neighbors("second");
    assert_eq!(middle.prev.unwrap().id, "first");
    assert_eq!(middle.next.unwrap().id, "third");

    let head = map.neighbors("first");
    assert!(head.prev.is_none());
    assert_eq!(head.next.unwrap().id, "second");
}

#[test]
fn diff_009_initial_step_index() {
    let steps = steps();
    assert_eq!(get_initial_step_index(&steps, Some("second")), 1);
    assert_eq!(get_initial_step_index(&steps, None), 0);
    assert_eq!(get_initial_step_index(&steps, Some("missing")), 0);
}

#[test]
fn diff_010_parse_step() {
    let steps = steps();
    assert_eq!(parse_step(&steps, &"second"), Some("second"));
    assert_eq!(parse_step(&steps, &"missing"), None);
    assert_eq!(parse_step(&steps, &42i32), None);
    assert_eq!(parse_step(&steps, &()), None);
}

#[test]
fn diff_011_initial_data_copy() {
    let mut data: FlowData<bool> = HashMap::new();
    data.insert("first".to_string(), true);

    let copy = get_initial_data(Some(&data));
    assert_eq!(copy, data);

    let mut mutated = get_initial_data(Some(&data));
    mutated.insert("second".to_string(), false);
    assert!(!data.contains_key("second"));

    let empty: FlowData<bool> = get_initial_data(None);
    assert!(empty.is_empty());
}

#[test]
fn diff_012_validate_without_schema() {
    let steps = schema_steps();
    let result = block_on(validate_step(&steps, "done", true)).unwrap();
    match result {
        ValidationResult::Success { data } => assert!(*data.downcast::<bool>().unwrap()),
        ValidationResult::Failure { .. } => panic!("expected success"),
    }
}

#[test]
fn diff_013_validate_schema_accepts() {
    let steps = schema_steps();
    let result = block_on(validate_step(&steps, "name", "Ada")).unwrap();
    match result {
        ValidationResult::Success { data } => {
            assert_eq!(*data.downcast::<&'static str>().unwrap(), "Ada")
        }
        ValidationResult::Failure { .. } => panic!("expected success"),
    }
}

#[test]
fn diff_014_validate_schema_rejects() {
    let steps = schema_steps();
    let result = block_on(validate_step(&steps, "name", "")).unwrap();
    match result {
        ValidationResult::Failure { issues } => assert_eq!(issues[0].message, "Required"),
        ValidationResult::Success { .. } => panic!("expected failure"),
    }
}

#[test]
fn diff_015_validate_unknown_id() {
    let steps = schema_steps();
    let error = block_on(validate_step(&steps, "missing", true))
        .err().expect("unknown step must return an error");
    assert_eq!(error, StepError::not_found("missing"));
    assert_eq!(error.to_string(), "Step \"missing\" not found.");
}

#[test]
fn diff_017_get_step_status() {
    let steps = steps();
    assert_eq!(get_step_status(&steps, 1, "first"), StepStatus::Previous);
    assert_eq!(get_step_status(&steps, 1, "second"), StepStatus::Active);
    assert_eq!(get_step_status(&steps, 1, "third"), StepStatus::Upcoming);
}

#[test]
fn diff_018_get_step_statuses() {
    let steps = steps();
    let statuses = get_step_statuses(&steps, 1);
    assert_eq!(statuses.len(), 3);
    assert_eq!(statuses.get("first"), Some(&StepStatus::Previous));
    assert_eq!(statuses.get("second"), Some(&StepStatus::Active));
    assert_eq!(statuses.get("third"), Some(&StepStatus::Upcoming));
}

#[test]
fn diff_019_match_step_dispatch() {
    let steps = steps();
    let mut handlers: HashMap<String, Box<dyn Fn(&FixtureStep) -> &'static str>> = HashMap::new();
    handlers.insert("first".to_string(), Box::new(|step| step.label));
    handlers.insert("second".to_string(), Box::new(|step| step.label));
    handlers.insert("third".to_string(), Box::new(|step| step.label));
    assert_eq!(match_step(&steps, "second", &handlers).unwrap(), "Step 2");
}

#[test]
fn diff_020_match_step_unknown_id() {
    let steps = steps();
    let handlers: HashMap<String, Box<dyn Fn(&FixtureStep) -> &'static str>> = HashMap::new();
    let error = match_step(&steps, "missing", &handlers).unwrap_err();
    assert_eq!(error, StepError::not_found("missing"));
    assert_eq!(error.to_string(), "Step \"missing\" not found.");
}

#[test]
fn diff_021_match_step_missing_handler() {
    let steps = steps();
    let mut partial: HashMap<String, Box<dyn Fn(&FixtureStep) -> &'static str>> = HashMap::new();
    partial.insert("first".to_string(), Box::new(|step| step.label));
    let error = match_step(&steps, "third", &partial).unwrap_err();
    assert_eq!(error, StepError::no_handler("third"));
    assert_eq!(error.to_string(), "No match handler found for step \"third\".");
}
