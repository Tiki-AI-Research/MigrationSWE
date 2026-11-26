// Target-side CONTRACT probes for BATCH-CONTRACT-0002.
// Executed in the target (Rust) container by target/batches/BATCH-CONTRACT-0002/run.sh.
// Emits the normalized value of CONTRACT-033, CONTRACT-034, CONTRACT-036 and
// CONTRACT-037 on stdout as JSON. CONTRACT-035 (protected-file digest) is merged
// in by the shell runner.
use std::path::Path;

use serde_json::{json, Map, Value};

const NODE_TYPE_CASES: &[&str] = &[
    "Index Scan using idx on t",
    "Index Scan Backward using idx2 on t2 alias2",
    "Bitmap Index Scan on bidx",
    "Seq Scan on foo",
    "Parallel Seq Scan on t",
    "Async Foreign Scan on t",
    "CTE Scan on cte1 alias_cte",
    "Function Scan on fn alias_fn",
    "Subquery Scan on sub",
    "Nested Loop",
    "Nested Loop Join",
    "Merge Left Join",
    "Hash Join",
    "Hash Left Join",
    "HashAggregate",
    "GroupAggregate",
    "MixedAggregate",
];

const NODE_TYPE_KEYS: &[&str] = &[
    "Node Type",
    "Relation Name",
    "Alias",
    "Index Name",
    "Scan Direction",
    "Join Type",
    "Partial Mode",
    "Strategy",
    "Parallel Aware",
    "Async Capable",
    "Disabled",
    "Planned Partitions",
    "CTE Name",
    "Function Name",
];

const CANONICAL_CAPABILITIES: &[&str] = &[
    "from_source",
    "create_plan",
    "from_text",
    "from_json",
    "cleanup_source",
    "split_into_lines",
    "split_balanced",
    "find_node_by_id",
    "find_node_by_subplan_name",
    "should_show_prop",
];

// Compile-time proof that every public capability named by R002/R034 is exported
// with a callable signature. If a capability is removed or renamed the probe
// fails to build.
fn assert_capabilities() {
    let _ = pev2_core::from_source;
    let _ = pev2_core::create_plan;
    let _ = pev2_core::from_text;
    let _ = pev2_core::from_json;
    let _ = pev2_core::cleanup_source;
    let _ = pev2_core::split_into_lines;
    let _ = pev2_core::split_balanced;
    let _ = pev2_core::find_node_by_id;
    let _ = pev2_core::find_node_by_subplan_name;
    let _ = pev2_core::should_show_prop;
}

fn proposal_033() -> Value {
    let mut out = Map::new();
    for case in NODE_TYPE_CASES {
        let node = pev2_core::parse_node_type(case);
        let mut kept = Map::new();
        for key in NODE_TYPE_KEYS {
            if let Some(v) = node.get(*key) {
                kept.insert((*key).to_string(), v.clone());
            }
        }
        out.insert((*case).to_string(), Value::Object(kept));
    }
    Value::Object(out)
}

fn proposal_034() -> Value {
    json!(CANONICAL_CAPABILITIES)
}

fn proposal_036(repo_root: &Path) -> Value {
    let text = std::fs::read_to_string(
        repo_root.join("fixtures/batches/BATCH-CONTRACT-0002/genuine-plan.txt"),
    )
    .expect("genuine-plan fixture")
    .trim()
    .to_string();
    let content = pev2_core::from_source(&text).expect("parse");
    let plan =
        pev2_core::create_plan_envelope("", content.clone(), "SELECT  a     b").expect("plan");
    let root = plan
        .get("content")
        .and_then(|c| c.get("Plan"))
        .cloned()
        .unwrap_or(Value::Null);
    json!({
        "Node Type": root.get("Node Type").cloned().unwrap_or(Value::Null),
        "Plan Rows": root.get("Plan Rows").cloned().unwrap_or(Value::Null),
        "Total Cost": root.get("Total Cost").cloned().unwrap_or(Value::Null),
        "isAnalyze": plan.get("isAnalyze").cloned().unwrap_or(Value::Null),
        "maxEstimateFactor": plan
            .get("content")
            .and_then(|c| c.get("maxEstimateFactor"))
            .cloned()
            .unwrap_or(Value::Null),
        "query": plan.get("query").cloned().unwrap_or(Value::Null),
    })
}

fn proposal_037(repo_root: &Path) -> Value {
    json!({
        "repo_root": repo_root.to_string_lossy(),
        "base_commit_present": repo_root.join(".git").exists(),
    })
}

fn main() {
    assert_capabilities();
    let repo_root = std::env::var("REPO_ROOT").unwrap_or_else(|_| "/workspace/repo".to_string());
    let repo_root = Path::new(&repo_root);
    let result = json!({
        "CONTRACT-033": proposal_033(),
        "CONTRACT-034": proposal_034(),
        "CONTRACT-036": proposal_036(repo_root),
        "CONTRACT-037": proposal_037(repo_root),
    });
    println!("{}", serde_json::to_string(&result).unwrap());
}
