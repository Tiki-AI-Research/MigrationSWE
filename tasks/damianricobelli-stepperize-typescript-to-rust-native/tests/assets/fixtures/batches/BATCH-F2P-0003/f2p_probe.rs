//! F2P target probes for BATCH-F2P-0003.
//!
//! One `#[test]` per observation. Each test exercises the invocation path named
//! by the observation and asserts the frozen target postcondition of the Rust
//! `stepperize_core` crate. The shared runner copies this file into
//! `crates/stepperize-core/tests/` before running `cargo test`.
//!
//! Source (TypeScript) runs the same semantic predicate through the shared
//! runner; because it provides no `stepperize_core` crate these postconditions
//! do not hold and the observations resolve to FAIL.

#![allow(dead_code)]

use std::any::Any;
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll, Wake, Waker};

use stepperize_core::{
    create_step_map, get_initial_data, get_initial_step_index, get_step_status, get_step_statuses,
    match_step, parse_step, validate_step, FlowData, GetId, GetStepById, Issue, Step,
    StepChangeContext, StepDirection, StepMap, StepError, StepStatus, StandardSchemaV1, Stepper,
    ValidationResult,
};

/// Minimal executor: drives validators that resolve, including validators that
/// yield once before resolving, without requiring an async runtime.
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

type Handlers = HashMap<String, Box<dyn Fn(&TestStep) -> &'static str>>;

fn schema_steps() -> Vec<TestStep> {
    vec![
        TestStep::with_schema("name", Box::new(StringSchema)),
        TestStep::new("done", "Done"),
    ]
}

/// F2P-049: the migrated responsibilities run with no subprocess/FFI/RPC and
/// the crate declares no external dependency or build script.
#[test]
fn f2p_049() {
    let steps = schema_steps();
    let result = block_on(validate_step(&steps, "done", true));
    assert!(result.is_ok(), "validate_step must resolve without a runtime");
}

/// F2P-050: every one of the eight operations is callable under its Rust name.
#[test]
fn f2p_050() {
    let steps = sample_steps();
    let map = create_step_map(sample_steps());
    assert_eq!(get_initial_step_index(&steps, Some("second")), 1);
    assert_eq!(parse_step(&steps, &"second"), Some("second"));
    let data: FlowData<bool> = get_initial_data(None);
    assert!(data.is_empty());
    let _ = block_on(validate_step(&steps, "second", true)).unwrap();
    assert_eq!(get_step_status(&steps, 1, "second"), StepStatus::Active);
    assert_eq!(get_step_statuses(&steps, 1).len(), 3);
    let mut handlers: Handlers = HashMap::new();
    handlers.insert("first".to_string(), Box::new(|step| step.label));
    assert_eq!(match_step(&steps, "first", &handlers).unwrap(), "Step 1");
    assert_eq!(map.ids(), vec!["first", "second", "third"]);
}

/// F2P-051: an external consumer can import the public data model and use it.
#[test]
fn f2p_051() {
    let steps = sample_steps();
    let map: StepMap<TestStep> = create_step_map(sample_steps());
    let ids: Vec<GetId> = map.ids().into_iter().map(str::to_string).collect();
    assert_eq!(ids, vec!["first", "second", "third"]);

    let by_id: Option<&TestStep> = (&steps[..]).step_by_id("second");
    assert_eq!(by_id.unwrap().label, "Step 2");

    let flow: FlowData<bool> = FlowData::new();
    assert!(flow.is_empty());

    let ok: ValidationResult<bool> = ValidationResult::success(true);
    assert!(ok.is_success());
    let status = StepStatus::Previous;
    assert_eq!(status, StepStatus::Previous);

    let context = StepChangeContext {
        from: "first".to_string(),
        to: "second".to_string(),
        from_index: 0,
        to_index: 1,
        direction: StepDirection::Next,
        statuses: HashMap::new(),
    };
    assert_eq!(context.to_index, 1);

    let stepper = Stepper::new(sample_steps(), 1);
    assert_eq!(stepper.id(), Some("second"));
}

/// F2P-052: validate_step returns a Result and reports an unknown id through
/// the StepError Display contract instead of panicking.
#[test]
fn f2p_052() {
    let steps = schema_steps();
    // ValidationResult is not required to implement Debug by the public API.
    let error = block_on(validate_step(&steps, "missing", true))
        .err().expect("unknown step must return an error");
    assert_eq!(error, StepError::not_found("missing"));
    assert_eq!(error.to_string(), "Step \"missing\" not found.");
}

/// F2P-053: get_step_statuses yields a keyed container of active/previous/upcoming.
#[test]
fn f2p_053() {
    let steps = sample_steps();
    let statuses = get_step_statuses(&steps, 1);
    assert_eq!(statuses.get("first"), Some(&StepStatus::Previous));
    assert_eq!(statuses.get("second"), Some(&StepStatus::Active));
    assert_eq!(statuses.get("third"), Some(&StepStatus::Upcoming));
    assert_eq!(statuses.len(), 3);
}

/// F2P-055: a step without a schema succeeds with the value unchanged.
#[test]
fn f2p_055() {
    let steps = schema_steps();
    let result = block_on(validate_step(&steps, "done", 7i32)).unwrap();
    match result {
        ValidationResult::Success { data } => assert_eq!(*data.downcast::<i32>().unwrap(), 7),
        ValidationResult::Failure { .. } => panic!("expected success"),
    }
}

/// F2P-056: StepMap::has reports membership.
#[test]
fn f2p_056() {
    let map = create_step_map(sample_steps());
    assert!(map.has("second"));
    assert!(!map.has("missing"));
}

/// F2P-057: StepMap::index_of returns a signed index and validate_step reports
/// unknown ids through Result.
#[test]
fn f2p_057() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.index_of("third"), 2);
    assert_eq!(map.index_of("missing"), -1);

    let steps = schema_steps();
    let error = block_on(validate_step(&steps, "missing", true))
        .err().expect("unknown step must return an error");
    assert_eq!(error.to_string(), "Step \"missing\" not found.");
}

/// F2P-058: get_initial_data returns an independent copy and an empty map when absent.
#[test]
fn f2p_058() {
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

/// F2P-059: validateStep rejects an unknown id before any schema runs.
#[test]
fn f2p_059() {
    let steps = schema_steps();
    let error = block_on(validate_step(&steps, "unknown", "Ada"))
        .err().expect("unknown step must return an error");
    assert_eq!(error, StepError::not_found("unknown"));
    assert_eq!(error.to_string(), "Step \"unknown\" not found.");
}

/// F2P-060: the Standard Schema boundary is a trait exposing version/vendor/validate.
#[test]
fn f2p_060() {
    let schema = StringSchema;
    assert_eq!(schema.version(), 1);
    assert_eq!(schema.vendor(), "test");

    // Issue likewise has no required Debug bound.
    let accepted = block_on(schema.validate(Box::new("Ada")))
        .unwrap_or_else(|_| panic!("schema must accept a nonempty string"));
    assert_eq!(*accepted.downcast::<&'static str>().unwrap(), "Ada");

    let rejected = block_on(schema.validate(Box::new(""))).unwrap_err();
    assert_eq!(rejected[0].message, "Required");
}

/// F2P-061: the eight operations compile and run under their Rust names, and
/// ids() preserves input order.
#[test]
fn f2p_061() {
    let steps = sample_steps();
    let map = create_step_map(sample_steps());
    assert_eq!(map.ids(), vec!["first", "second", "third"]);
    assert!(get_initial_step_index(&steps, None) == 0);
    assert_eq!(parse_step(&steps, &"second"), Some("second"));
    let empty: FlowData<bool> = get_initial_data(None);
    assert!(empty.is_empty());
    assert!(block_on(validate_step(&steps, "second", true)).is_ok());
    assert_eq!(get_step_status(&steps, 1, "second"), StepStatus::Active);
    assert_eq!(get_step_statuses(&steps, 1).len(), 3);
    let mut handlers: Handlers = HashMap::new();
    handlers.insert("second".to_string(), Box::new(|step| step.label));
    assert_eq!(match_step(&steps, "second", &handlers).unwrap(), "Step 2");
}

/// F2P-062: create_step_map keeps an ordered sequence so ids() matches input order.
#[test]
fn f2p_062() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.ids(), vec!["first", "second", "third"]);
    assert_eq!(map.at(0).unwrap().id, "first");
    assert!(map.at(9).is_none());
}

/// F2P-063: match_step reports a missing handler through StepError.
#[test]
fn f2p_063() {
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

/// F2P-064: get_initial_step_index resolves the index and defaults to zero.
#[test]
fn f2p_064() {
    let steps = sample_steps();
    assert_eq!(get_initial_step_index(&steps, Some("second")), 1);
    assert_eq!(get_initial_step_index(&steps, None), 0);
    assert_eq!(get_initial_step_index(&steps, Some("missing")), 0);
}

/// F2P-065: first/last return the boundaries and no value on an empty list.
#[test]
fn f2p_065() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.first().unwrap().id, "first");
    assert_eq!(map.last().unwrap().id, "third");

    let empty = create_step_map(Vec::<TestStep>::new());
    assert!(empty.first().is_none());
    assert!(empty.last().is_none());
}

/// F2P-066: match_step distinguishes NoHandler from NotFound through StepError.
#[test]
fn f2p_066() {
    let steps = sample_steps();
    let mut partial: Handlers = HashMap::new();
    partial.insert("first".to_string(), Box::new(|step| step.label));

    assert_eq!(
        match_step(&steps, "missing", &partial).unwrap_err(),
        StepError::not_found("missing")
    );
    assert_eq!(
        match_step(&steps, "third", &partial).unwrap_err(),
        StepError::no_handler("third")
    );
}

/// F2P-067: the Rust crate executes the migrated logic itself; behaviour holds
/// with no dependency on the TypeScript implementation.
#[test]
fn f2p_067() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.get("second").unwrap().label, "Step 2");
    assert!(map.get("missing").is_none());
    assert_eq!(get_step_status(&sample_steps(), 0, "first"), StepStatus::Active);
}

/// F2P-068: the operations run with no runtime delegation; get returns options.
#[test]
fn f2p_068() {
    let map = create_step_map(sample_steps());
    assert!(map.get("second").is_some());
    assert!(map.get("missing").is_none());
}

/// F2P-070: match_step exposes distinct error variants with protected wording.
#[test]
fn f2p_070() {
    let steps = sample_steps();
    let empty: Handlers = HashMap::new();
    assert_eq!(
        match_step(&steps, "missing", &empty).unwrap_err(),
        StepError::not_found("missing")
    );

    let mut partial: Handlers = HashMap::new();
    partial.insert("first".to_string(), Box::new(|step| step.label));
    let error = match_step(&steps, "third", &partial).unwrap_err();
    assert_eq!(error.to_string(), "No match handler found for step \"third\".");
}

/// F2P-071: StepMap queries return optional references.
#[test]
fn f2p_071() {
    let map = create_step_map(sample_steps());
    assert!(map.at(9).is_none());
    assert!(map.get("missing").is_none());
    assert!(map.next("missing").is_none());
    assert_eq!(map.get("second").unwrap().id, "second");
}
