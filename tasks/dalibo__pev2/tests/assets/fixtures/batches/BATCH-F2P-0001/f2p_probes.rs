//! F2P target probes for BATCH-F2P-0001.
//!
//! Each test executes one atomic observation against the native Rust
//! `pev2-core` execution path and asserts the frozen target-only
//! postcondition. The same semantic predicate is evaluated on the source
//! (TypeScript) side by `source/batches/BATCH-F2P-0001/runner`, where the
//! native Rust entry is absent, so the predicate is not satisfied.

use pev2_core::{
    cleanup_source, create_plan, from_json, from_source, from_text, get_plan_content,
    parse_node_type, PlanContent,
};
use serde_json::{json, Value};

fn tov<T: serde::Serialize>(value: &T) -> Value {
    serde_json::to_value(value).unwrap()
}

fn content_root(content: &PlanContent) -> Value {
    tov(content).get("Plan").cloned().expect("root Plan")
}

fn plan_content(plan: &pev2_core::Plan) -> Value {
    tov(plan).get("content").cloned().expect("content")
}

fn f64_at(value: &Value, key: &str) -> f64 {
    value
        .get(key)
        .and_then(|v| v.as_f64())
        .unwrap_or(f64::NAN)
}

fn assert_zero_fields(root: &Value) {
    for key in [
        "Actual Loops",
        "Actual Rows",
        "Actual Total Time",
        "Actual Startup Time",
    ] {
        assert_eq!(f64_at(root, key), 0.0, "{}", key);
    }
}

// R051 : getPlanContent missing Plan error.
#[test]
fn f2p_001_invalid_plan_literal() {
    let err = get_plan_content(&json!({"foo": 1})).unwrap_err();
    assert_eq!(err, "Invalid plan");
}

// R041 : cleanupSource strips psql frames, header and trailing row count.
#[test]
fn f2p_002_cleanup_psql_frame() {
    let out = cleanup_source("│  Seq Scan on t  │\nQUERY PLAN\n(1 row)\n");
    assert_eq!(out, "  Seq Scan on t  \n\n");
}

// R048 : fractional actual rows marker.
#[test]
fn f2p_003_fractional_rows_marker() {
    let root = content_root(
        &from_text(
            "Seq Scan on foo  (cost=0.00..1.00 rows=10 width=4) (actual time=0.1..0.2 rows=0.25 loops=1)",
        )
        .unwrap(),
    );
    assert_eq!(f64_at(&root, "Actual Rows"), 0.25);
    assert_eq!(
        root.get("*Actual Rows Is Fractional").and_then(|v| v.as_bool()),
        Some(true)
    );
}

// R047 : never-executed node is zeroed.
#[test]
fn f2p_004_never_executed_zeroed() {
    let root = content_root(
        &from_text("Hash  (cost=6.12..6.12 rows=212 width=1346) (never executed)").unwrap(),
    );
    assert_zero_fields(&root);
}

// R055 : isVerbose sees Output inside a CTE/InitPlan descendant.
#[test]
fn f2p_005_is_verbose_cte_output() {
    let source = r#"[ { "Plan": { "Node Type": "Aggregate", "Plans": [
        { "Node Type": "Seq Scan", "Parent Relationship": "InitPlan",
          "Subplan Name": "CTE e", "Relation Name": "employes", "Output": ["num_service"] },
        { "Node Type": "CTE Scan", "Parent Relationship": "Outer", "CTE Name": "e" } ] } } ]"#;
    let plan = create_plan("", from_source(source).unwrap(), "").unwrap();
    assert_eq!(tov(&plan).get("isVerbose").and_then(|v| v.as_bool()), Some(true));
}

// R048 : decimal point in actual rows, larger value.
#[test]
fn f2p_006_fractional_rows_decimal() {
    let root = content_root(
        &from_text(
            "Seq Scan on bar  (cost=0.00..1.00 rows=10 width=4) (actual time=0.1..0.2 rows=100.00 loops=1)",
        )
        .unwrap(),
    );
    assert_eq!(f64_at(&root, "Actual Rows"), 100.0);
    assert_eq!(
        root.get("*Actual Rows Is Fractional").and_then(|v| v.as_bool()),
        Some(true)
    );
}

// R041 : pipe frame around the line.
#[test]
fn f2p_007_cleanup_pipe_frame() {
    let out = cleanup_source("|  Seq Scan on t  |\n");
    assert_eq!(out, "  Seq Scan on t  \n");
}

// R041 : wrapping double quotes.
#[test]
fn f2p_008_cleanup_wrapping_quotes() {
    let out = cleanup_source("\"Seq Scan on t\"\n");
    assert_eq!(out, "Seq Scan on t\n");
}

// R042 : invalid text plan errors.
#[test]
fn f2p_009_from_text_invalid() {
    assert_eq!(from_text("not a plan").unwrap_err(), "Unable to parse plan");
}

// R041 : QUERY PLAN header removed.
#[test]
fn f2p_010_cleanup_query_plan_header() {
    let out = cleanup_source("QUERY PLAN\nSeq Scan on t\n");
    assert_eq!(out, "Seq Scan on t\n");
}

// R041 : trailing row-count line removed.
#[test]
fn f2p_011_cleanup_trailing_rowcount() {
    let out = cleanup_source("Seq Scan on t\n(8 rows)\n");
    assert_eq!(out, "Seq Scan on t\n\n");
}

// R055 : isVerbose sees a plain descendant Output.
#[test]
fn f2p_012_is_verbose_descendant_output() {
    let source = r#"{ "Plan": { "Node Type": "Aggregate", "Plans": [
        { "Node Type": "Seq Scan", "Relation Name": "t", "Output": ["a"] } ] } }"#;
    let plan = create_plan("", from_source(source).unwrap(), "").unwrap();
    assert_eq!(tov(&plan).get("isVerbose").and_then(|v| v.as_bool()), Some(true));
}

// R044 : node-type parsing strips/re-adds the Join suffix and defaults Inner.
#[test]
fn f2p_013_node_type_entry_identity() {
    assert_eq!(env!("CARGO_PKG_NAME"), "pev2-core");
    let ml = tov(&parse_node_type("Merge Left Join"));
    assert_eq!(ml.get("Node Type").and_then(|v| v.as_str()), Some("Merge Join"));
    assert_eq!(ml.get("Join Type").and_then(|v| v.as_str()), Some("Left"));
    let nl = tov(&parse_node_type("Nested Loop"));
    assert_eq!(nl.get("Node Type").and_then(|v| v.as_str()), Some("Nested Loop"));
    let nlj = tov(&parse_node_type("Nested Loop Join"));
    assert_eq!(nlj.get("Node Type").and_then(|v| v.as_str()), Some("Nested Loop"));
    assert_eq!(nlj.get("Join Type").and_then(|v| v.as_str()), Some("Inner"));
}

// R046 : ModifyTable replaced by Operation in the native metric pass.
#[test]
fn f2p_014_modifytable_operation_json() {
    let source =
        r#"{ "Plan": { "Node Type": "ModifyTable", "Operation": "Insert", "Plans": [] } }"#;
    let plan = create_plan("", from_source(source).unwrap(), "").unwrap();
    let root = plan_content(&plan).get("Plan").cloned().unwrap();
    assert_eq!(root.get("Node Type").and_then(|v| v.as_str()), Some("Insert"));
}

// R049 : maxEstimateFactor doubled default and explicit default.
#[test]
fn f2p_015_max_estimate_factor() {
    let analyzed = create_plan(
        "",
        from_source("Seq Scan on foo  (cost=0.00..1.00 rows=10 width=4) (actual time=0.1..0.2 rows=100 loops=1)").unwrap(),
        "",
    )
    .unwrap();
    assert_eq!(f64_at(&plan_content(&analyzed), "maxEstimateFactor"), 20.0);
    let plain = create_plan(
        "",
        from_source("Seq Scan on foo  (cost=0.00..1.00 rows=10 width=4)").unwrap(),
        "",
    )
    .unwrap();
    assert_eq!(f64_at(&plan_content(&plain), "maxEstimateFactor"), 1.0);
}

// R055 : isVerbose sees Output nested under a CTE child list.
#[test]
fn f2p_016_is_verbose_nested_cte() {
    let source = r#"[ { "Plan": { "Node Type": "Aggregate", "Plans": [
        { "Node Type": "Hash Join", "Plans": [
          { "Node Type": "Seq Scan", "Relation Name": "t", "Output": ["a", "b"] } ] } ] } } ]"#;
    let plan = create_plan("", from_source(source).unwrap(), "").unwrap();
    assert_eq!(tov(&plan).get("isVerbose").and_then(|v| v.as_bool()), Some(true));
}

// R042 : empty text is not a plan.
#[test]
fn f2p_017_from_text_empty() {
    assert_eq!(from_text("").unwrap_err(), "Unable to parse plan");
}

// R046 : text Insert parsing yields Node Type Insert.
#[test]
fn f2p_018_insert_text() {
    let root = content_root(
        &from_text("Insert on foo  (cost=0.00..0.01 rows=1 width=24) (actual time=0.5..0.5 rows=0 loops=1)").unwrap(),
    );
    assert_eq!(root.get("Node Type").and_then(|v| v.as_str()), Some("Insert"));
}

// R047 : never-executed Seq Scan zeroed.
#[test]
fn f2p_019_never_executed_seq_scan() {
    let root = content_root(
        &from_text("Seq Scan on t  (cost=0.00..1.00 rows=1 width=4) (never executed)").unwrap(),
    );
    assert_zero_fields(&root);
}

// R047 : never-executed Nested Loop zeroed.
#[test]
fn f2p_020_never_executed_nested_loop() {
    let root =
        content_root(&from_text("Nested Loop  (cost=0.00..1.00 rows=1 width=4) (never executed)").unwrap());
    assert_zero_fields(&root);
}

// R043 : fromJson slices bracket lines and un-doubles quotes.
#[test]
fn f2p_021_from_json_doubled_quotes() {
    let payload = "\"[\n  {\n    \"\"Plan\"\": {\n      \"\"Node Type\"\": \"\"Seq Scan\"\",\n      \"\"Plan Rows\"\": 5\n    }\n  }\n]\"";
    let content = from_json(payload).unwrap();
    assert_eq!(
        tov(&content).get("Plan").and_then(|p| p.get("Node Type")).and_then(|v| v.as_str()),
        Some("Seq Scan")
    );
}

// R042 : garbage text is not a plan.
#[test]
fn f2p_022_from_text_garbage() {
    assert_eq!(from_text("some random text").unwrap_err(), "Unable to parse plan");
}

// R041 : plus continuation removed.
#[test]
fn f2p_023_cleanup_plus_continuation() {
    let out = cleanup_source("Seq Scan on t    +\n");
    assert_eq!(out, "Seq Scan on t\n");
}

// R042 : node-like but rootless text errors.
#[test]
fn f2p_024_from_text_rootless() {
    let bad = "Bitmap Heap Scan on a\n   Recheck Cond: ((id = 42))\n";
    assert_eq!(from_text(bad).unwrap_err(), "Unable to parse plan");
}
