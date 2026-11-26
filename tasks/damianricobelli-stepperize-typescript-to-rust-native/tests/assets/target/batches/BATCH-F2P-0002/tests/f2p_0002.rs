//! Target-side probes for BATCH-F2P-0002 (stepperize-core, TypeScript -> Rust).
//!
//! Each `f2p_*` test exercises one fail-to-pass observation from the batch and
//! asserts the Rust-only target postcondition. The same probes are expected to
//! FAIL on the TypeScript implementation because the target conditions are
//! expressed in Rust terms (signed `index_of`, `Result`-returning
//! `validate_step`, `StepError` variants, boxed-dynamic `parse_step`, unordered
//! status map, ordered ids, and the public data-model types).

use std::any::Any;
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll, Wake, Waker};

use stepperize_core::{
    create_step_map, get_initial_data, get_initial_step_index, get_step_status, get_step_statuses,
    match_step, parse_step, validate_step, FlowData, StandardSchemaV1, Step, StepError, StepMap,
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
        Self { id, label, schema: None }
    }
    fn with_schema(id: &'static str, schema: Box<dyn StandardSchemaV1>) -> Self {
        Self { id, label: "", schema: Some(schema) }
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
    ) -> Pin<Box<dyn Future<Output = Result<Box<dyn Any>, Vec<stepperize_core::Issue>>> + '_>> {
        Box::pin(async move {
            match value.downcast::<&'static str>() {
                Ok(text) if !text.is_empty() => Ok(Box::new(*text) as Box<dyn Any>),
                _ => Err(vec![stepperize_core::Issue::new("Required")]),
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
    ) -> Pin<Box<dyn Future<Output = Result<Box<dyn Any>, Vec<stepperize_core::Issue>>> + '_>> {
        Box::pin(async move {
            yield_once().await;
            match value.downcast::<&'static str>() {
                Ok(text) if !text.is_empty() => Ok(Box::new(*text) as Box<dyn Any>),
                _ => Err(vec![stepperize_core::Issue::new("Required")]),
            }
        })
    }
}

type Handlers = HashMap<String, Box<dyn Fn(&TestStep) -> &'static str>>;

fn schema_steps() -> Vec<TestStep> {
    vec![
        TestStep::with_schema("name", Box::new(StringSchema)),
        TestStep::new("done", "Done"),
    ]
}

#[test]
fn f2p_025_get_initial_data_is_an_independent_clone() {
    let mut data: FlowData<bool> = HashMap::new();
    data.insert("first".to_string(), true);
    let mut copy = get_initial_data(Some(&data));
    copy.insert("second".to_string(), false);
    assert!(!data.contains_key("second"));
    let empty: FlowData<bool> = get_initial_data(None);
    assert!(empty.is_empty());
}

#[test]
fn f2p_026_statuses_are_derived_self_contained() {
    let steps = sample_steps();
    let statuses = get_step_statuses(&steps, 1);
    assert_eq!(statuses.get("first"), Some(&StepStatus::Previous));
    assert_eq!(statuses.get("second"), Some(&StepStatus::Active));
    assert_eq!(statuses.get("third"), Some(&StepStatus::Upcoming));
}

#[test]
fn f2p_027_validate_step_awaits_async_validators() {
    let steps = vec![
        TestStep::with_schema("name", Box::new(AsyncStringSchema)),
        TestStep::new("done", "Done"),
    ];
    let result = block_on(validate_step(&steps, "name", "Ada")).unwrap();
    assert!(result.is_success());
}

#[test]
fn f2p_028_validate_step_unknown_id_is_not_found() {
    let steps = schema_steps();
    // ValidationResult is not required to implement Debug by the public API.
    let error = block_on(validate_step(&steps, "missing", true))
        .err().expect("unknown step must return an error");
    assert_eq!(error, StepError::not_found("missing"));
}

#[test]
fn f2p_029_get_step_status_derives_self_contained() {
    let steps = sample_steps();
    assert_eq!(get_step_status(&steps, 1, "first"), StepStatus::Previous);
    assert_eq!(get_step_status(&steps, 1, "second"), StepStatus::Active);
    assert_eq!(get_step_status(&steps, 1, "third"), StepStatus::Upcoming);
    assert_eq!(get_step_status(&steps, -1, "first"), StepStatus::Upcoming);
}

#[test]
fn f2p_030_match_step_dispatch_uses_target_runtime() {
    let steps = sample_steps();
    let mut handlers: Handlers = HashMap::new();
    handlers.insert("second".to_string(), Box::new(|step| step.label));
    assert_eq!(match_step(&steps, "second", &handlers).unwrap(), "Step 2");
}

#[test]
fn f2p_031_neighbor_queries_are_self_contained() {
    let steps = sample_steps();
    let map = create_step_map(steps);
    assert_eq!(map.next("first").unwrap().id, "second");
    assert_eq!(map.prev("third").unwrap().id, "second");
    let n = map.neighbors("second");
    assert_eq!(n.prev.unwrap().id, "first");
    assert_eq!(n.next.unwrap().id, "third");
}

#[test]
fn f2p_032_match_step_error_variants() {
    let steps = sample_steps();
    let mut partial: Handlers = HashMap::new();
    partial.insert("first".to_string(), Box::new(|step| step.label));
    let error = match_step(&steps, "third", &partial).unwrap_err();
    assert_eq!(error, StepError::no_handler("third"));
    let unknown =
        match_step::<TestStep, &'static str>(&steps, "missing", &Handlers::new()).unwrap_err();
    assert_eq!(unknown, StepError::not_found("missing"));
}

#[test]
fn f2p_033_create_step_map_preserves_input_order() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.ids(), vec!["first", "second", "third"]);
    assert!(map.has("second"));
    assert!(!map.has("missing"));
}

#[test]
fn f2p_034_first_and_last_are_self_contained() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.first().unwrap().id, "first");
    assert_eq!(map.last().unwrap().id, "third");
    let empty = create_step_map(Vec::<TestStep>::new());
    assert!(empty.first().is_none());
    assert!(empty.last().is_none());
}

#[test]
fn f2p_035_all_eight_operations_are_exposed() {
    let steps = sample_steps();
    let map = create_step_map(sample_steps());
    assert_eq!(map.ids().len(), 3);
    let _ = get_initial_step_index(&steps, Some("second"));
    let _ = parse_step(&steps, &"second");
    let _ = get_initial_data::<bool>(None);
    let _ = block_on(validate_step(&steps, "second", true));
    let _ = get_step_status(&steps, 1, "second");
    let _ = get_step_statuses(&steps, 1);
    let _ = match_step(&steps, "second", &Handlers::new());
}

#[test]
fn f2p_036_index_of_targets() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.index_of("third"), 2);
    assert_eq!(map.index_of("missing"), -1);
}

#[test]
fn f2p_037_public_data_model_types_compile() {
    // Compile-time proof that the public data-model items are exported.
    fn assert_step<S: Step>() {}
    fn assert_schema<S: StandardSchemaV1>() {}
    assert_step::<TestStep>();
    assert_schema::<StringSchema>();
    let _: Option<StepMap<TestStep>> = Some(create_step_map(sample_steps()));
    let _: Option<ValidationResult<bool>> = Some(ValidationResult::success(true));
    let _: FlowData<bool> = HashMap::new();
    let _: StepStatus = StepStatus::Active;
}

#[test]
fn f2p_038_index_of_returns_signed_integer() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.index_of("third"), 2);
    assert_eq!(map.index_of("missing"), -1);
}

#[test]
fn f2p_039_validate_step_supports_direct_and_async_validators() {
    let direct = schema_steps();
    let result = block_on(validate_step(&direct, "name", "Ada")).unwrap();
    assert!(result.is_success());
    let steps = vec![
        TestStep::with_schema("name", Box::new(AsyncStringSchema)),
        TestStep::new("done", "Done"),
    ];
    let result = block_on(validate_step(&steps, "name", "Ada")).unwrap();
    assert!(result.is_success());
    let data = get_initial_data::<bool>(None);
    assert!(data.is_empty());
}

#[test]
fn f2p_040_get_initial_data_is_self_contained() {
    let mut data: FlowData<bool> = HashMap::new();
    data.insert("first".to_string(), true);
    let copy = get_initial_data(Some(&data));
    assert_eq!(copy, data);
}

#[test]
fn f2p_041_membership_queries_are_self_contained() {
    let map = create_step_map(sample_steps());
    assert!(map.has("second"));
    assert!(!map.has("missing"));
}

#[test]
fn f2p_042_parse_step_downcasts_dynamic_values() {
    let steps = sample_steps();
    assert_eq!(parse_step(&steps, &"second"), Some("second"));
    assert_eq!(parse_step(&steps, &42i32), None);
    assert_eq!(parse_step(&steps, &()), None);
}

#[test]
fn f2p_043_validate_step_unknown_id_display() {
    let steps = schema_steps();
    let error = block_on(validate_step(&steps, "missing", true))
        .err().expect("unknown step must return an error");
    assert_eq!(error.to_string(), "Step \"missing\" not found.");
}

#[test]
fn f2p_044_match_step_dispatch_is_self_contained() {
    let steps = sample_steps();
    let mut handlers: Handlers = HashMap::new();
    handlers.insert("second".to_string(), Box::new(|step| step.label));
    assert_eq!(match_step(&steps, "second", &handlers).unwrap(), "Step 2");
}

#[test]
fn f2p_045_get_by_id_preserves_ordering() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.get("second").unwrap().label, "Step 2");
    assert!(map.get("missing").is_none());
    assert_eq!(map.ids(), vec!["first", "second", "third"]);
}

#[test]
fn f2p_046_statuses_are_keyed_by_step_id() {
    let steps = sample_steps();
    let statuses: HashMap<String, StepStatus> = get_step_statuses(&steps, 1);
    assert_eq!(statuses.get("first"), Some(&StepStatus::Previous));
    assert_eq!(statuses.get("second"), Some(&StepStatus::Active));
    assert_eq!(statuses.get("third"), Some(&StepStatus::Upcoming));
    // Unordered container: iteration order is unspecified but lookups by key work.
    assert_eq!(statuses.len(), 3);
}

#[test]
fn f2p_047_parse_step_handles_non_string_values() {
    let steps = sample_steps();
    assert_eq!(parse_step(&steps, &"second"), Some("second"));
    assert_eq!(parse_step(&steps, &42i32), None);
    assert_eq!(parse_step(&steps, &()), None);
}

#[test]
fn f2p_048_at_index_is_self_contained() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.at(0).unwrap().id, "first");
    assert!(map.at(9).is_none());
}
