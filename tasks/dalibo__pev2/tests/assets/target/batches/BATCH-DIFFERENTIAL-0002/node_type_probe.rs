//! DIFF-033 / R033 target probe: exercise `pev2_core::parse_node_type` for every
//! node-type string in the shared corpus and emit the produced node maps as
//! JSON for the differential comparison.
//!
//! The probe is compiled as a tiny binary that path-depends on the target
//! crate. Usage: node_type_probe <node-type-cases.json>

use pev2_core::parse_node_type;
use serde_json::{json, Value};

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 2 {
        eprintln!("usage: node_type_probe <node-type-cases.json>");
        std::process::exit(2);
    }
    let text = std::fs::read_to_string(&args[1]).expect("read corpus");
    let corpus: Value = serde_json::from_str(&text).expect("parse corpus");
    let cases = corpus
        .get("cases")
        .and_then(|value| value.as_array())
        .expect("corpus.cases");

    let mut results = Vec::new();
    for case in cases {
        let input = case
            .get("input")
            .and_then(|value| value.as_str())
            .expect("case.input");
        let node = parse_node_type(input);
        results.push(json!({
            "input": input,
            "actual": serde_json::to_value(&node).unwrap(),
        }));
    }
    println!("{}", serde_json::to_string(&results).unwrap());
}
