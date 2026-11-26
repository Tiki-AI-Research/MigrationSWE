//! F2P target-postcondition probes for BATCH-F2P-0001.
//!
//! Each test encodes the *target* (`Rust`) postcondition of one or more
//! `target-runtime` observations. The same semantic predicate is expected to
//! fail on the TypeScript source, which exposes no `stepperize_core` crate.
//!
//! Observation coverage (test -> observation ids):
//! * `all_eight_operations_are_exported_and_run`   -> F2P-001, F2P-010, F2P-012, F2P-018
//! * `parse_step_downcasts_boxed_dynamic_values`   -> F2P-002, F2P-004, F2P-005, F2P-006, F2P-019
//! * `validate_step_awaits_boxed_schema_future`    -> F2P-003, F2P-007, F2P-016, F2P-017, F2P-020, F2P-022
//! * `match_step_uses_not_found_and_no_handler`    -> F2P-008
//! * `create_step_map_preserves_input_id_order`    -> F2P-021, F2P-024
//! * `get_initial_data_returns_independent_clone`  -> F2P-013
//! * `step_error_display_uses_protected_wording`   -> F2P-014
//!
//! The remaining dependency-policy observations (F2P-009, F2P-011, F2P-015,
//! F2P-023) are exercised by `probes/run_target_probe.sh`, which inspects the
//! manifest and rebuilds the crate with the TypeScript source removed.

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

/// Validator whose future is already complete on the first poll.
struct ReadyStringSchema;

impl StandardSchemaV1 for ReadyStringSchema {
    fn vendor(&self) -> &str {
        "ready"
    }

    fn validate(
        &self,
        value: Box<dyn Any>,
    ) -> Pin<Box<dyn Future<Output = Result<Box<dyn Any>, Vec<Issue>>> + '_>> {
        let result = match value.downcast::<&'static str>() {
            Ok(text) if !text.is_empty() => Ok(Box::new(*text) as Box<dyn Any>),
            _ => Err(vec![Issue::new("Required")]),
        };
        Box::pin(std::future::ready(result))
    }
}

/// Validator that yields once before producing its result.
struct YieldingStringSchema;

impl StandardSchemaV1 for YieldingStringSchema {
    fn vendor(&self) -> &str {
        "yielding"
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

fn schema_steps(schema: Box<dyn StandardSchemaV1>) -> Vec<TestStep> {
    vec![TestStep::with_schema("name", schema), TestStep::new("done", "Done")]
}

#[test]
fn all_eight_operations_are_exported_and_run() {
    let steps = sample_steps();

    let map = create_step_map(sample_steps());
    assert_eq!(map.ids(), vec!["first", "second", "third"]);
    assert_eq!(get_initial_step_index(&steps, Some("second")), 1);
    assert_eq!(parse_step(&steps, &"second"), Some("second"));
    let data: FlowData<bool> = FlowData::new();
    assert!(get_initial_data(Some(&data)).is_empty());
    assert!(block_on(validate_step(&steps, "first", true)).is_ok());
    assert_eq!(get_step_status(&steps, 1, "second"), StepStatus::Active);
    assert_eq!(get_step_statuses(&steps, 1).len(), 3);

    let mut handlers: Handlers = HashMap::new();
    handlers.insert("second".to_string(), Box::new(|step| step.label));
    assert_eq!(match_step(&steps, "second", &handlers).unwrap(), "Step 2");
}

#[test]
fn parse_step_downcasts_boxed_dynamic_values() {
    let steps = sample_steps();

    // A boxed dynamic value carrying a known id matches.
    let dynamic: Box<dyn Any> = Box::new("second");
    assert_eq!(parse_step(&steps, dynamic.as_ref()), Some("second"));

    // An owned string still downcasts to a known id.
    let owned: Box<dyn Any> = Box::new(String::from("third"));
    assert_eq!(parse_step(&steps, owned.as_ref()), Some("third"));

    // Unknown ids and non-string / absent values yield no value.
    assert_eq!(parse_step(&steps, &"missing"), None);
    assert_eq!(parse_step(&steps, &42i32), None);
    assert_eq!(parse_step(&steps, &()), None);

    // The public boundary is &dyn Any: an Option containing a string is
    // still an enum, not a string value. Do not silently coerce user data.
    assert_eq!(parse_step(&steps, &Some(String::from("second"))), None);
    assert_eq!(parse_step(&steps, &Some("second")), None);
    assert_eq!(parse_step(&steps, &None::<String>), None);
}

#[test]
fn validate_step_awaits_boxed_schema_future() {
    // A validator that resolves on the first poll.
    let ready = schema_steps(Box::new(ReadyStringSchema));
    let result = block_on(validate_step(&ready, "name", "Ada")).unwrap();
    match result {
        ValidationResult::Success { data } => {
            assert_eq!(*data.downcast::<&'static str>().unwrap(), "Ada");
        }
        ValidationResult::Failure { .. } => panic!("expected success"),
    }

    // A validator that yields once still resolves without an external runtime.
    let yielding = schema_steps(Box::new(YieldingStringSchema));
    let result = block_on(validate_step(&yielding, "name", "")).unwrap();
    match result {
        ValidationResult::Failure { issues } => assert_eq!(issues[0].message, "Required"),
        ValidationResult::Success { .. } => panic!("expected failure"),
    }
}

#[test]
fn match_step_uses_not_found_and_no_handler() {
    let steps = sample_steps();

    let empty: Handlers = HashMap::new();
    assert_eq!(
        match_step(&steps, "missing", &empty).unwrap_err(),
        StepError::not_found("missing")
    );

    let mut partial: Handlers = HashMap::new();
    partial.insert("first".to_string(), Box::new(|step| step.label));
    assert_eq!(
        match_step(&steps, "third", &partial).unwrap_err(),
        StepError::no_handler("third")
    );

    // The existing step with a handler dispatches.
    assert_eq!(match_step(&steps, "first", &partial).unwrap(), "Step 1");
}

#[test]
fn create_step_map_preserves_input_id_order() {
    let map = create_step_map(sample_steps());
    assert_eq!(map.ids(), vec!["first", "second", "third"]);

    let empty = create_step_map(Vec::<TestStep>::new());
    assert!(empty.ids().is_empty());
    assert!(empty.first().is_none());
    assert!(empty.last().is_none());
}

#[test]
fn get_initial_data_returns_independent_clone() {
    let mut data: FlowData<bool> = FlowData::new();
    data.insert("first".to_string(), true);

    let mut copy = get_initial_data(Some(&data));
    copy.insert("second".to_string(), false);
    assert!(!data.contains_key("second"));

    let absent: FlowData<bool> = get_initial_data(None);
    assert!(absent.is_empty());
}

#[test]
fn validate_step_preserves_the_generic_input_without_a_schema() {
    // D: Any includes erased boxes. The result must still contain D, rather
    // than normalizing D to whatever happens to be stored inside that box.
    fn unchanged<D: Any>(steps: &[TestStep], value: D) -> D {
        match block_on(validate_step(steps, "first", value)).unwrap() {
            ValidationResult::Success { data } => *data.downcast::<D>()
                .unwrap_or_else(|_| panic!("validate_step changed the input's dynamic type")),
            ValidationResult::Failure { .. } => panic!("schema-free step rejected user data"),
        }
    }

    let steps = sample_steps();
    let value: Box<dyn Any> = Box::new(vec![13u8, 29, 47]);
    let address = value.downcast_ref::<Vec<u8>>().unwrap().as_ptr();
    let output = unchanged(&steps, value);
    let output = output.downcast::<Vec<u8>>().unwrap();
    assert_eq!(output.as_slice(), &[13, 29, 47]);
    assert_eq!(output.as_ptr(), address);

    let value: Box<dyn Any + Send> = Box::new(String::from("sendable data"));
    let output = unchanged(&steps, value);
    assert_eq!(*output.downcast::<String>().unwrap(), "sendable data");

    let value: Box<dyn Any + Send + Sync> = Box::new(91usize);
    let output = unchanged(&steps, value);
    assert_eq!(*output.downcast::<usize>().unwrap(), 91);
}

#[test]
fn validate_step_passes_the_generic_input_to_the_schema() {
    struct BoxSchema;
    impl StandardSchemaV1 for BoxSchema {
        fn vendor(&self) -> &str { "boxed-user-data" }

        fn validate(
            &self,
            value: Box<dyn Any>,
        ) -> Pin<Box<dyn Future<Output = Result<Box<dyn Any>, Vec<Issue>>> + '_>> {
            Box::pin(async move {
                let original = value.downcast::<Box<dyn Any>>()
                    .map_err(|_| vec![Issue::new("input type changed before validation")])?;
                let text = original.downcast_ref::<String>()
                    .ok_or_else(|| vec![Issue::new("expected boxed string user data")])?;
                Ok(Box::new(format!("validated: {text}")) as Box<dyn Any>)
            })
        }
    }

    let steps = schema_steps(Box::new(BoxSchema));
    let value: Box<dyn Any> = Box::new(String::from("payload"));
    match block_on(validate_step(&steps, "name", value)).unwrap() {
        ValidationResult::Success { data } => {
            assert_eq!(*data.downcast::<String>().unwrap(), "validated: payload");
        }
        ValidationResult::Failure { .. } => panic!("schema did not receive the original D value"),
    }
}

#[test]
fn step_error_display_uses_protected_wording() {
    assert_eq!(
        StepError::not_found("x").to_string(),
        "Step \"x\" not found."
    );
    assert_eq!(
        StepError::no_handler("x").to_string(),
        "No match handler found for step \"x\"."
    );
}
