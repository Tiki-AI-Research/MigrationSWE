//! DIFF-034: compile against the public API required by instruction.md.
use pev2_core::{JsonMap, Node, Plan, PlanContent};
use serde_json::{Map, Value};

fn main() {
    let _: fn(JsonMap) -> Map<String, Value> = |value| value;
    let _: fn(PlanContent) -> JsonMap = |value| value;
    let _: fn(Plan) -> JsonMap = |value| value;
    let _: fn(Node) -> JsonMap = |value| value;
    let _: fn(&str) -> Result<PlanContent, String> = pev2_core::from_source;
    let _: fn(&str) -> Result<PlanContent, String> = pev2_core::from_text;
    let _: fn(&str) -> Result<PlanContent, String> = pev2_core::from_json;
    let _: fn(&Value) -> Result<PlanContent, String> = pev2_core::get_plan_content;
    let _: fn(&str) -> String = pev2_core::cleanup_source;
    let _: fn(&str) -> Vec<String> = pev2_core::split_into_lines;
    let _: fn(&str, &str) -> Vec<String> = pev2_core::split_balanced;
    let _: fn(&str, PlanContent, &str) -> Result<Plan, String> = pev2_core::create_plan;
    let _: fn(&str) -> Node = pev2_core::parse_node_type;
    let _: fn(&str, &Value) -> bool = pev2_core::should_show_prop;
    let _: fn(&Plan, i64) -> Option<Node> = pev2_core::find_node_by_id;
    let _: fn(&Plan, &str) -> Option<Node> = pev2_core::find_node_by_subplan_name;
    println!("true");
}
