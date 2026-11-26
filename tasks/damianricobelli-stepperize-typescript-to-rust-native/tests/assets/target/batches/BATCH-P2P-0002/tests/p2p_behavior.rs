//! Rust behavior tests mirroring `packages/core/src/tests/utils.test.ts`.

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

/// Minimal executor: enough to drive validators that resolve, including
/// validators that yield once before resolving.
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

/// A validator that yields once before producing its result.
async fn yield_once() {
    let mut yielded = false;
    std::future::poll_fn(move |cx| {
        if yielded {
            Poll::Ready(())
        } else {
            yielded = true;
            cx.waker().wake_by_ref();
            Poll::Pending
        }
    })
    .await;
}

struct TestStep {
    id: &'static str,
    label: &'static str,
    schema: Option<Box<dyn StandardSchemaV1>>,
}

impl TestStep {
    fn new(id: &'static str, label: &'static str) -> Self {
        Self {
            id,
            label,
            schema: None,
        }
    }

    fn with_schema(id: &'static str, schema: Box<dyn StandardSchemaV1>) -> Self {
        Self {
            id,
            label: "",
            schema: Some(schema),
        }
    }
}

impl Step for TestStep {
    fn id(&self) -> &str {
        self.id
    }

    fn schema(&self) -> Option<&dyn StandardSchemaV1> {
        self.schema.as_deref()
    }
}

fn sample_steps() -> Vec<TestStep> {
    vec![
        TestStep::new("first", "Step 1"),
        TestStep::new("second", "Step 2"),
        TestStep::new("third", "Step 3"),
    ]
}

struct StringSchema;

impl StandardSchemaV1 for StringSchema {
    fn vendor(&self) -> &str {
        "test"
    }

    fn validate(
        &self,
        value: Box<dyn Any>,
    ) -> Pin<Box<dyn Future<Output = Result<Box<dyn Any>, Vec<Issue>>> + '_>> {
        Box::pin(async move {
            match value.downcast::<&'static str>() {
                Ok(text) if !text.is_empty() => Ok(Box::new(*text) as Box<dyn Any>),
                _ => Err(vec![Issue::new("Required")]),
            }
        })
    }
}

struct AsyncStringSchema;

impl StandardSchemaV1 for AsyncStringSchema {
    fn vendor(&self) -> &str {
        "test-async"
    }

    fn validate(
        &self,
        value: Box<dyn Any>,
    ) -> Pin<Box<dyn Future<Output = Result<Box<dyn Any>, Vec<Issue>>> + '_>> {
        Box::pin(async move {
            yield_once().await;
            match value.downcast::<&'static str>() {
                Ok(text) if !text.is_empty() => Ok(Box::new(*text) as Box<dyn Any>),
                _ => Err(vec![Issue::new("Required")]),
            }
        })
    }
}

/// Step-id keyed handler map used by the `match_step` scenarios.
type Handlers = HashMap<String, Box<dyn Fn(&TestStep) -> &'static str>>;

fn schema_steps() -> Vec<TestStep> {
    vec![
        TestStep::with_schema("name", Box::new(StringSchema)),
        TestStep::new("done", "Done"),
    ]
}

#[test]
fn create_step_map_returns_steps_and_ids() {
    let step_map = create_step_map(sample_steps());

    assert_eq!(step_map.ids(), vec!["first", "second", "third"]);
    assert_eq!(step_map.steps().len(), 3);
    assert_eq!(step_map.steps()[0].label, "Step 1");
}

#[test]
fn create_step_map_gets_steps_by_id_and_index() {
    let step_map = create_step_map(sample_steps());

    assert_eq!(step_map.get("second").unwrap().label, "Step 2");
    assert_eq!(step_map.at(0).unwrap().id, "first");
    assert!(step_map.get("missing").is_none());
    assert!(step_map.at(9).is_none());
}

#[test]
fn create_step_map_returns_positions_and_neighbors() {
    let step_map = create_step_map(sample_steps());

    assert_eq!(step_map.index_of("third"), 2);
    assert_eq!(step_map.index_of("missing"), -1);
    assert!(step_map.has("second"));
    assert!(!step_map.has("missing"));
    assert_eq!(step_map.first().unwrap().id, "first");
    assert_eq!(step_map.last().unwrap().id, "third");
    assert_eq!(step_map.next("first").unwrap().id, "second");
    assert_eq!(step_map.prev("third").unwrap().id, "second");
    assert!(step_map.next("missing").is_none());
    assert!(step_map.prev("missing").is_none());

    let neighbors = step_map.neighbors("second");
    assert_eq!(neighbors.prev.unwrap().id, "first");
    assert_eq!(neighbors.next.unwrap().id, "third");

    let first_neighbors = step_map.neighbors("first");
    assert!(first_neighbors.prev.is_none());
    assert_eq!(first_neighbors.next.unwrap().id, "second");

    let last_neighbors = step_map.neighbors("third");
    assert_eq!(last_neighbors.prev.unwrap().id, "second");
    assert!(last_neighbors.next.is_none());
}

#[test]
fn create_step_map_empty_boundaries() {
    let step_map = create_step_map(Vec::<TestStep>::new());

    assert!(step_map.first().is_none());
    assert!(step_map.last().is_none());
    assert!(step_map.get("first").is_none());
    assert!(step_map.at(0).is_none());
}

#[test]
fn get_initial_step_index_defaults_to_zero() {
    let steps = sample_steps();

    assert_eq!(get_initial_step_index(&steps, None), 0);
    assert_eq!(get_initial_step_index(&steps, Some("missing")), 0);
    assert_eq!(get_initial_step_index(&steps, Some("second")), 1);
}

#[test]
fn get_initial_data_returns_a_defensive_copy() {
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
fn validate_step_succeeds_without_a_schema() {
    let steps = schema_steps();

    let result = block_on(validate_step(&steps, "done", true)).unwrap();
    assert!(result.is_success());
    match result {
        ValidationResult::Success { data } => {
            assert!(*data.downcast::<bool>().unwrap());
        }
        ValidationResult::Failure { .. } => panic!("expected success"),
    }
}

#[test]
fn validate_step_returns_success_with_the_validated_value() {
    let steps = schema_steps();

    let result = block_on(validate_step(&steps, "name", "Ada")).unwrap();
    match result {
        ValidationResult::Success { data } => {
            assert_eq!(*data.downcast::<&'static str>().unwrap(), "Ada");
        }
        ValidationResult::Failure { .. } => panic!("expected success"),
    }
}

#[test]
fn validate_step_returns_issues_when_the_schema_fails() {
    let steps = schema_steps();

    let result = block_on(validate_step(&steps, "name", "")).unwrap();
    match result {
        ValidationResult::Failure { issues } => {
            assert_eq!(issues[0].message, "Required");
        }
        ValidationResult::Success { .. } => panic!("expected failure"),
    }
}

#[test]
fn validate_step_fails_for_an_unknown_step_id() {
    let steps = schema_steps();

    let error = block_on(validate_step(&steps, "missing", true))
        .err().expect("unknown step must return an error");
    assert_eq!(error, StepError::not_found("missing"));
    assert_eq!(error.to_string(), "Step \"missing\" not found.");
}

#[test]
fn validate_step_supports_async_validators() {
    let steps = vec![
        TestStep::with_schema("name", Box::new(AsyncStringSchema)),
        TestStep::new("done", "Done"),
    ];

    let result = block_on(validate_step(&steps, "name", "Ada")).unwrap();
    match result {
        ValidationResult::Success { data } => {
            assert_eq!(*data.downcast::<&'static str>().unwrap(), "Ada");
        }
        ValidationResult::Failure { .. } => panic!("expected success"),
    }

    let result = block_on(validate_step(&steps, "name", "")).unwrap();
    match result {
        ValidationResult::Failure { issues } => assert_eq!(issues[0].message, "Required"),
        ValidationResult::Success { .. } => panic!("expected failure"),
    }
}

#[test]
fn step_statuses_use_active_previous_upcoming() {
    let steps = sample_steps();

    assert_eq!(get_step_status(&steps, 1, "first"), StepStatus::Previous);
    assert_eq!(get_step_status(&steps, 1, "second"), StepStatus::Active);
    assert_eq!(get_step_status(&steps, 1, "third"), StepStatus::Upcoming);
    assert_eq!(get_step_status(&steps, 1, "missing"), StepStatus::Upcoming);

    let statuses = get_step_statuses(&steps, 1);
    assert_eq!(statuses.get("first"), Some(&StepStatus::Previous));
    assert_eq!(statuses.get("second"), Some(&StepStatus::Active));
    assert_eq!(statuses.get("third"), Some(&StepStatus::Upcoming));
    assert_eq!(statuses.len(), 3);
}

#[test]
fn parse_step_returns_known_ids_only() {
    let steps = sample_steps();

    assert_eq!(parse_step(&steps, &"second"), Some("second"));
    assert_eq!(parse_step(&steps, &"missing"), None);
    assert_eq!(parse_step(&steps, &42i32), None);
    assert_eq!(parse_step(&steps, &()), None);
}

#[test]
fn match_step_runs_the_handler_for_the_provided_id() {
    let steps = sample_steps();

    let mut handlers: Handlers = HashMap::new();
    handlers.insert("first".to_string(), Box::new(|step| step.label));
    handlers.insert("second".to_string(), Box::new(|step| step.label));
    handlers.insert("third".to_string(), Box::new(|step| step.label));

    assert_eq!(match_step(&steps, "second", &handlers).unwrap(), "Step 2");
}

#[test]
fn match_step_fails_when_no_exhaustive_handler_exists() {
    let steps = sample_steps();

    let mut partial: Handlers = HashMap::new();
    partial.insert("first".to_string(), Box::new(|step| step.label));

    let error = match_step(&steps, "third", &partial).unwrap_err();
    assert_eq!(error, StepError::no_handler("third"));
    assert_eq!(
        error.to_string(),
        "No match handler found for step \"third\"."
    );
}

#[test]
fn match_step_fails_for_an_unknown_step_id() {
    let steps = sample_steps();

    let handlers: Handlers = HashMap::new();
    let error = match_step(&steps, "missing", &handlers).unwrap_err();
    assert_eq!(error, StepError::not_found("missing"));
    assert_eq!(error.to_string(), "Step \"missing\" not found.");
}
