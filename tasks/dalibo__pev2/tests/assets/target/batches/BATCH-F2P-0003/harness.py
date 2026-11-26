#!/usr/bin/env python3
"""Target harness for BATCH-F2P-0003 (pev2 TypeScript -> Rust migration).

The harness evaluates the F2P target postconditions of this batch and writes a
JSON report to the path given as the first argument (``{report}`` in the
runtime manifest).

Every potentially long running command is wrapped with an explicit
``timeout`` (coreutils).  No command is allowed to block forever.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

BATCH_ID = "BATCH-F2P-0003"

# Observation families.
RUNTIME_OBS = [
    "F2P-069",
    "F2P-056",
    "F2P-064",
    "F2P-065",
    "F2P-066",
    "F2P-067",
    "F2P-068",
    "F2P-049",
    "F2P-050",
    "F2P-051",
    "F2P-052",
    "F2P-053",
    "F2P-054",
    "F2P-055",
    "F2P-057",
    "F2P-058",
    "F2P-059",
    "F2P-060",
    "F2P-061",
    "F2P-062",
    "F2P-063",
    "F2P-073",
    "F2P-074",
    "F2P-075",
    "F2P-076",
    "F2P-077",
    "F2P-078",
    "F2P-079",
    "F2P-080",
    "F2P-082",
    "F2P-083",
    "F2P-084",
    "F2P-085",
    "F2P-086",
    "F2P-087",
    "F2P-088",
    "F2P-089",
    "F2P-090",
    "F2P-091",
    "F2P-092",
    "F2P-093",
    "F2P-094",
    "F2P-095",
    "F2P-096",
    "F2P-097",
    "F2P-098",
    "F2P-099",
    "F2P-100",
    "F2P-101",
    "F2P-102",
    "F2P-103",
    "F2P-104",
    "F2P-105",
    "F2P-106",
    "F2P-107",
    "F2P-108",
    "F2P-109",
    "F2P-110",
    "F2P-111",
    "F2P-112",
    "F2P-113",
    "F2P-114",
    "F2P-115",
    "F2P-116",
    "F2P-117",
    "F2P-118",
    "F2P-119",
    "F2P-120",
    "F2P-121",
    "F2P-122",
    "F2P-123",
    "F2P-124",
    "F2P-125",
    "F2P-126",
    "F2P-127",
    "F2P-128",
    "F2P-129",
    "F2P-130",
    "F2P-131",
    "F2P-132",
    "F2P-133",
    "F2P-134",
    "F2P-135",
    "F2P-136",
    "F2P-137",
    "F2P-138",
    "F2P-139",
    "F2P-140",
    "F2P-141",
    "F2P-142",
    "F2P-143",
    "F2P-144",
    "F2P-145",
    "F2P-146",
    "F2P-147",
    "F2P-148",
    "F2P-149",
    "F2P-150",
    "F2P-151",
    "F2P-152",
    "F2P-153",
    "F2P-154",
    "F2P-155",
    "F2P-156",
    "F2P-157",
    "F2P-158",
    "F2P-159",
    "F2P-160",
    "F2P-161",
    "F2P-162",
    "F2P-163",
    "F2P-164",
    "F2P-165",
    "F2P-166",
    "F2P-167",
    "F2P-168",
    "F2P-169",
    "F2P-170",
    "F2P-171",
    "F2P-172",
    "F2P-173",
    "F2P-174",
    "F2P-175",
    "F2P-176",
    "F2P-177",
    "F2P-178",
    "F2P-179",
    "F2P-180",
    "F2P-181",
    "F2P-182",
    "F2P-183",
    "F2P-184",
    "F2P-185",
    "F2P-186",
]
MANIFEST_OBS = ["F2P-070", "F2P-071", "F2P-072"]
ALL_OBS = RUNTIME_OBS + MANIFEST_OBS

# The crate must declare the dependency the public contract types require
# (`serde_json::Map`/`Value`).  The former list also demanded the reference
# implementation's incidental crates (`regex`, `fancy-regex`, `wasm-bindgen`,
# `serde-wasm-bindgen`), which the instruction never requires; binding the gate
# to that set fails every legitimate alternative dependency choice.
REQUIRED_DEPS = [
    "serde_json",
]

FORBIDDEN_DEP_TOKENS = [
    "napi",
    "neon",
    "nodejs",
    "node-sys",
    "deno",
    "bun",
    "quickjs",
    "rquickjs",
    "rusty_v8",
    "v8",
    "wasmer",
    "wasmtime",
    "libloading",
]

PROBE_MAX_SECS = 900

# Rust probe compiled from the candidate `examples/` directory.  It prints a
# single line ``F2P_PROBE_JSON:{...}``.
PROBE_RS = r'''// Probe for BATCH-F2P-0003 target postconditions.
use std::panic::{catch_unwind, AssertUnwindSafe};

use pev2_core::{
    cleanup_source, create_plan, find_node_by_subplan_name, from_json, from_source, from_text,
    get_plan_content, parse_node_type, should_show_prop, split_balanced,
};
use serde_json::{json, Map, Value};

fn f(v: &Value, key: &str) -> f64 {
    v.get(key).and_then(|x| x.as_f64()).unwrap_or(f64::NAN)
}

fn fm(v: &Map<String, Value>, key: &str) -> f64 {
    v.get(key).and_then(|x| x.as_f64()).unwrap_or(f64::NAN)
}

fn approx(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-6
}

fn content_of(source: &str) -> Map<String, Value> {
    from_source(source).expect("source parse")
}

fn plan_of(source: &str) -> Map<String, Value> {
    let content = content_of(source);
    create_plan("", content, "").expect("createPlan")
}

fn should_show() -> bool {
    should_show_prop("Filter", &json!("(a = 1)"))
        && !should_show_prop("Filter", &json!(""))
        && !should_show_prop("Node Type", &json!("Seq Scan"))
        && !should_show_prop("Actual Rows", &json!(0))
}

fn cleanup_frames() -> bool {
    cleanup_source("│  Seq Scan on t  │\nQUERY PLAN\n(1 row)\n") == "  Seq Scan on t  \n\n"
}

fn never_executed() -> bool {
    let content = from_text("Hash  (cost=6.12..6.12 rows=212 width=1346) (never executed)")
        .expect("from_text never executed");
    let root = content.get("Plan").expect("plan");
    approx(f(root, "Actual Loops"), 0.0)
        && approx(f(root, "Actual Rows"), 0.0)
        && approx(f(root, "Actual Total Time"), 0.0)
        && approx(f(root, "Actual Startup Time"), 0.0)
}

fn node_type_fields() -> bool {
    let merge = parse_node_type("Merge Left Join");
    let nested = parse_node_type("Nested Loop");
    let hash = parse_node_type("Hash Join");
    merge.get("Node Type").and_then(|v| v.as_str()) == Some("Merge Join")
        && merge.get("Join Type").and_then(|v| v.as_str()) == Some("Left")
        && nested.get("Node Type").and_then(|v| v.as_str()) == Some("Nested Loop")
        && hash.get("Join Type").and_then(|v| v.as_str()) == Some("Inner")
}

fn balanced_split() -> bool {
    split_balanced("a,(b,c),d", ",") == vec!["a".to_string(), "(b,c)".to_string(), "d".to_string()]
}

fn io_source() -> &'static str {
    r#"
     Update on a  (cost=0.00..266690.72 rows=0 width=0) (actual time=10.00..11.00 rows=0 loops=1)
         Buffers: shared hit=2 read=4 dirtied=6 written=8
         I/O Timings: read=2 write=4
         ->  Seq Scan on a  (cost=0.00..0.00 rows=10 width=1) (actual time=9.00..10.00 rows=10000000 loops=1)
               Buffers: shared hit=1 read=2 written=4
               I/O Timings: read=1 write=2
    "#
}

fn max_io() -> bool {
    let content = plan_of(io_source());
    let c = content.get("content").expect("content");
    approx(f(c, "maxIo"), 3.0)
}

fn fractional_rows() -> bool {
    let content = from_text(
        "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4) (actual time=0.1..0.2 rows=0.25 loops=1)",
    )
    .expect("from_text fractional");
    let root = content.get("Plan").expect("plan");
    approx(f(root, "Actual Rows"), 0.25)
        && root.get("*Actual Rows Is Fractional").and_then(|v| v.as_bool()) == Some(true)
}

fn hash_aggregate() -> bool {
    let agg = parse_node_type("HashAggregate");
    agg.get("Node Type").and_then(|v| v.as_str()) == Some("Aggregate")
        && agg.get("Partial Mode").and_then(|v| v.as_str()) == Some("Simple")
        && agg.get("Strategy").and_then(|v| v.as_str()) == Some("Hashed")
        && approx(fm(&agg, "Planned Partitions"), 0.0)
}

fn modify_insert() -> bool {
    let source_json = r#"
      [ { "Plan": { "Node Type": "ModifyTable", "Operation": "Insert",
            "Relation Name": "foo", "Alias": "foo",
            "Startup Cost": 0.00, "Total Cost": 0.01, "Plan Rows": 1, "Plan Width": 24,
            "Actual Startup Time": 0.568, "Actual Total Time": 0.568,
            "Actual Rows": 0, "Actual Loops": 1,
            "Plans": [ { "Node Type": "Result", "Parent Relationship": "Member" } ] } } ]
    "#;
    let content = from_source(source_json).expect("json parse");
    let plan = create_plan("", content, "").expect("createPlan");
    let root = plan
        .get("content")
        .and_then(|c| c.get("Plan"))
        .cloned()
        .unwrap_or(Value::Null);
    root.get("Node Type").and_then(|v| v.as_str()) == Some("Insert")
}

fn unable_to_parse() -> bool {
    matches!(from_text("not a plan"), Err(ref e) if e == "Unable to parse plan")
}

fn invalid_plan() -> bool {
    matches!(get_plan_content(&json!({"foo": 1})), Err(ref e) if e == "Invalid plan")
}

fn max_estimate() -> bool {
    let analyzed = plan_of(
        "Seq Scan on foo  (cost=0.00..1.00 rows=10 width=4) (actual time=0.1..0.2 rows=100 loops=1)",
    );
    let plain = plan_of("Seq Scan on foo  (cost=0.00..1.00 rows=10 width=4)");
    let a = analyzed
        .get("content")
        .map(|c| f(c, "maxEstimateFactor"))
        .unwrap_or(f64::NAN);
    let b = plain
        .get("content")
        .map(|c| f(c, "maxEstimateFactor"))
        .unwrap_or(f64::NAN);
    approx(a, 20.0) && approx(b, 1.0)
}

fn max_blocks_io() -> bool {
    let content = plan_of(io_source());
    let c = content.get("content").expect("content");
    let shared = c
        .get("maxBlocks")
        .and_then(|b| b.get("Shared"))
        .and_then(|v| v.as_f64())
        .unwrap_or(f64::NAN);
    approx(f(c, "maxIo"), 3.0) && approx(shared, 13.0)
}

fn json_double_quotes() -> bool {
    let source = "\"[\n  {\n    \"\"Plan\"\": {\n      \"\"Node Type\"\": \"\"Seq Scan\"\",\n      \"\"Plan Rows\"\": 5\n    }\n  }\n]\"";
    match from_json(source) {
        Ok(content) => {
            content
                .get("Plan")
                .and_then(|p| p.get("Node Type"))
                .and_then(|v| v.as_str())
                == Some("Seq Scan")
        }
        Err(_) => false,
    }
}

fn is_verbose_cte() -> bool {
    let source = r#"
[
  { "Plan": {
      "Node Type": "Aggregate",
      "Startup Cost": 38.51, "Total Cost": 38.52,
      "Plan Rows": 1, "Plan Width": 4,
      "Plans": [
        { "Node Type": "Seq Scan", "Parent Relationship": "InitPlan",
          "Subplan Name": "CTE e", "Relation Name": "employes",
          "Startup Cost": 0.00, "Total Cost": 38.25,
          "Plan Rows": 11, "Plan Width": 8,
          "Output": ["num_service"] },
        { "Node Type": "CTE Scan", "Parent Relationship": "Outer",
          "CTE Name": "e",
          "Startup Cost": 0.00, "Total Cost": 0.25,
          "Plan Rows": 4, "Plan Width": 4 }
      ] } }
]
"#;
    let content = from_source(source).expect("parse");
    let plan = create_plan("", content, "").expect("createPlan");
    let verbose = plan.get("isVerbose").and_then(|v| v.as_bool()) == Some(true);
    let ctes_ok = plan
        .get("ctes")
        .and_then(|v| v.as_array())
        .map(|a| a.len() == 1)
        .unwrap_or(false);
    verbose && ctes_ok
}


fn child_node<'a>(m: &'a Map<String, Value>, index: usize) -> &'a Map<String, Value> {
    m.get("Plans")
        .and_then(|v| v.as_array())
        .and_then(|a| a.get(index))
        .and_then(|v| v.as_object())
        .expect("child node")
}

fn plan_root<'a>(plan: &'a Map<String, Value>) -> &'a Map<String, Value> {
    plan.get("content")
        .and_then(|v| v.as_object())
        .and_then(|c| c.get("Plan"))
        .and_then(|v| v.as_object())
        .expect("plan root")
}

fn num_of(m: &Map<String, Value>, key: &str) -> f64 {
    m.get(key).and_then(|v| v.as_f64()).unwrap_or(f64::NAN)
}

fn sum_exclusive(m: &Map<String, Value>) -> f64 {
    let mut total = num_of(m, "*Duration (exclusive)");
    if let Some(children) = m.get("Plans").and_then(|v| v.as_array()) {
        for c in children {
            if let Some(obj) = c.as_object() {
                total += sum_exclusive(obj);
            }
        }
    }
    total
}

fn worker_launched_duration() -> bool {
    let plan = plan_of(r##" Finalize Aggregate  (cost=105660.73..105660.74 rows=1 width=8) (actual time=1092.605..1094.361 rows=1 loops=1)
   Output: sum((blah.i + j.j))
   ->  Gather  (cost=105658.26..105660.67 rows=24 width=8) (actual time=1076.814..1094.352 rows=8 loops=1)
         Output: (PARTIAL sum((blah.i + j.j)))
         Workers Planned: 24
         Workers Launched: 7
         ->  Partial Aggregate  (cost=104658.26..104658.27 rows=1 width=8) (actual time=1080.614..1080.614 rows=1 loops=8)
               Output: PARTIAL sum((blah.i + j.j))
               Worker 0:  actual time=1086.259..1086.260 rows=1 loops=1
               Worker 1:  actual time=1084.860..1084.861 rows=1 loops=1
               Worker 2:  actual time=1069.617..1069.618 rows=1 loops=1
               Worker 3:  actual time=1074.450..1074.452 rows=1 loops=1
               Worker 4:  actual time=1079.213..1079.214 rows=1 loops=1
               Worker 5:  actual time=1085.714..1085.715 rows=1 loops=1
               Worker 6:  actual time=1088.632..1088.632 rows=1 loops=1
               ->  Nested Loop  (cost=0.26..83824.92 rows=4166667 width=8) (actual time=0.454..1072.781 rows=125000 loops=8)
                     Output: blah.i, j.j
                     Worker 0:  actual time=0.505..1078.589 rows=83620 loops=1
                     Worker 1:  actual time=0.505..1077.215 rows=83620 loops=1
                     Worker 2:  actual time=0.455..1061.673 rows=167240 loops=1
                     Worker 3:  actual time=0.349..1066.490 rows=166060 loops=1
                     Worker 4:  actual time=0.419..1071.207 rows=164980 loops=1
                     Worker 5:  actual time=0.636..1077.959 rows=83620 loops=1
                     Worker 6:  actual time=0.611..1081.030 rows=83620 loops=1
                     ->  Parallel Seq Scan on public.blah  (cost=0.00..484.67 rows=4167 width=4) (actual time=0.007..1.455 rows=12500 loops=8)
                           Output: blah.i
                           Worker 0:  actual time=0.008..1.363 rows=8362 loops=1
                           Worker 1:  actual time=0.007..1.346 rows=8362 loops=1
                           Worker 2:  actual time=0.006..1.534 rows=16724 loops=1
                           Worker 3:  actual time=0.006..1.590 rows=16606 loops=1
                           Worker 4:  actual time=0.007..1.544 rows=16498 loops=1
                           Worker 5:  actual time=0.012..1.383 rows=8362 loops=1
                           Worker 6:  actual time=0.005..1.353 rows=8362 loops=1
                     ->  Function Scan on pg_catalog.generate_series j  (cost=0.26..10.26 rows=1000 width=4) (actual time=0.084..0.084 rows=10 loops=100000)
                           Output: j.j
                           Function Call: generate_series(1, expensive(((blah.i + 10) - blah.i)))
                           Worker 0:  actual time=0.126..0.127 rows=10 loops=8362
                           Worker 1:  actual time=0.126..0.127 rows=10 loops=8362
                           Worker 2:  actual time=0.062..0.062 rows=10 loops=16724
                           Worker 3:  actual time=0.063..0.063 rows=10 loops=16606
                           Worker 4:  actual time=0.063..0.064 rows=10 loops=16498
                           Worker 5:  actual time=0.126..0.127 rows=10 loops=8362
                           Worker 6:  actual time=0.126..0.127 rows=10 loops=8362
 Planning Time: 0.333 ms
 Execution Time: 1094.777 ms
"##);
    let root = plan_root(&plan);
    let gather = child_node(root, 0);
    let partial = child_node(gather, 0);
    let nested = child_node(partial, 0);
    let func = child_node(nested, 1);
    approx(num_of(func, "*Duration (exclusive)"), 1050.0)
}

fn parallel_worker_duration() -> bool {
    let plan = plan_of(r##"[
  {
    "Plan": {
      "Node Type": "Gather Merge",
      "Parallel Aware": false,
      "Startup Cost": 735306.27,
      "Total Cost": 1707599.95,
      "Plan Rows": 8333364,
      "Plan Width": 17,
      "Actual Startup Time": 1592.880,
      "Actual Total Time": 3805.404,
      "Actual Rows": 10000000,
      "Actual Loops": 1,
      "Workers Planned": 2,
      "Workers Launched": 2,
      "Plans": [
        {
          "Node Type": "Sort",
          "Parent Relationship": "Outer",
          "Parallel Aware": false,
          "Startup Cost": 734306.25,
          "Total Cost": 744722.95,
          "Plan Rows": 4166682,
          "Plan Width": 17,
          "Actual Startup Time": 1517.978,
          "Actual Total Time": 2020.558,
          "Actual Rows": 3333333,
          "Actual Loops": 3,
          "Sort Key": ["c1"],
          "Sort Method": "external merge",
          "Sort Space Used": 123600,
          "Sort Space Type": "Disk",
          "Workers": [
            {
              "Worker Number": 0,
              "Sort Method": "external merge",
              "Sort Space Used": 73744,
              "Sort Space Type": "Disk"
            },
            {
              "Worker Number": 1,
              "Sort Method": "external merge",
              "Sort Space Used": 75672,
              "Sort Space Type": "Disk"
            }
          ],
          "Plans": [
            {
              "Node Type": "Seq Scan",
              "Parent Relationship": "Outer",
              "Parallel Aware": true,
              "Relation Name": "t1",
              "Alias": "t1",
              "Startup Cost": 0.00,
              "Total Cost": 105264.82,
              "Plan Rows": 4166682,
              "Plan Width": 17,
              "Actual Startup Time": 0.214,
              "Actual Total Time": 400.681,
              "Actual Rows": 3333333,
              "Actual Loops": 3
            }
          ]
        }
      ]
    },
    "Planning Time": 0.158,
    "Triggers": [
    ],
    "Execution Time": 4225.445
  }
]
"##);
    let root = plan_root(&plan);
    let sort = child_node(root, 0);
    let scan = child_node(sort, 0);
    approx(num_of(sort, "*Duration (exclusive)"), 1619.877)
        && approx(num_of(scan, "*Duration (exclusive)"), 400.681)
}

fn parallel_gather_child_duration() -> bool {
    let plan = plan_of(r##"Limit  (cost=220725.07..220759.59 rows=1 width=27) (actual time=1621.743..1626.219 rows=1 loops=1)
  ->  Finalize GroupAggregate  (cost=220725.07..220966.73 rows=7 width=27) (actual time=1621.740..1621.740 rows=1 loops=1)
        Group Key: lineitem.l_shipmode
        ->  Gather Merge  (cost=220725.07..220966.55 rows=14 width=27) (actual time=1620.648..1626.206 rows=4 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              ->  Partial GroupAggregate  (cost=219725.04..219964.91 rows=7 width=27) (actual time=1616.481..1617.546 rows=2 loops=3)
                    Group Key: lineitem.l_shipmode
                    ->  Sort  (cost=219725.04..219755.02 rows=11990 width=27) (actual time=1615.387..1615.753 rows=10409 loops=3)
                          Sort Key: lineitem.l_shipmode
                          Sort Method: quicksort  Memory: 1198kB
                          Worker 0:  Sort Method: quicksort  Memory: 1195kB
                          Worker 1:  Sort Method: quicksort  Memory: 1200kB
                          ->  Parallel Hash Join  (cost=177956.54..218912.75 rows=11990 width=27) (actual time=1497.271..1613.187 rows=10409 loops=3)
                                Hash Cond: (orders.o_orderkey = lineitem.l_orderkey)
                                ->  Parallel Seq Scan on orders  (cost=0.00..33875.00 rows=625000 width=20) (actual time=0.007..55.525 rows=500000 loops=3)
                                ->  Parallel Hash  (cost=177806.66..177806.66 rows=11990 width=19) (actual time=1496.967..1496.967 rows=10409 loops=3)
                                      Buckets: 32768  Batches: 1  Memory Usage: 2016kB
                                      ->  Parallel Seq Scan on lineitem  (cost=0.00..177806.66 rows=11990 width=19) (actual time=0.361..1490.221 rows=10409 loops=3)
                                            Filter: ((l_shipmode = ANY ('{MAIL,AIR}'::bpchar[])) AND (l_commitdate < l_receiptdate) AND (l_shipdate < l_commitdate) AND (l_receiptdate >= '1996-01-01'::date) AND (l_receiptdate < '1997-01-01 00:00:00'::timestamp without time zone))
                                            Rows Removed by Filter: 1989996
Planning Time: 3.634 ms
Execution Time: 1626.725 ms
"##);
    let root = plan_root(&plan);
    let finalize = child_node(root, 0);
    let gather = child_node(finalize, 0);
    let agg = child_node(gather, 0);
    (num_of(agg, "*Duration (exclusive)") - 1.793).abs() < 0.01
}

fn cte_only_duration_excluded() -> bool {
    let plan = plan_of(r##"Result  (cost=0.01..0.05 rows=2 width=4) (actual time=1001.083..1001.087 rows=2 loops=1)
 CTE test
   ->  Result  (cost=0.00..0.01 rows=1 width=0) (actual time=1001.076..1001.076 rows=1 loops=1)
 ->  Append  (cost=0.00..0.04 rows=2 width=4) (actual time=1001.082..1001.085 rows=2 loops=1)
       ->  CTE Scan on test  (cost=0.00..0.02 rows=1 width=4) (actual time=1001.081..1001.082 rows=1 loops=1)
       ->  CTE Scan on test  (cost=0.00..0.02 rows=1 width=4) (actual time=0.001..0.002 rows=1 loops=1)
Total runtime: 1001.133 ms
(7 rows)
"##);
    let root = plan_root(&plan);
    num_of(root, "*Duration (exclusive)") - 0.002 < 0.0
}

fn cte_exclusive_time_conserved() -> bool {
    let plan = plan_of(r##"Nested Loop  (cost=19202.84..76099.33 rows=53175 width=144) (actual time=146.530..2999.096 rows=4379 loops=1)
  ->  HashAggregate  (cost=19202.08..19733.83 rows=53175 width=16) (actual time=144.782..147.879 rows=4379 loops=1)
        Group Key: m.principal_id
        ->  Hash Join  (cost=7564.47..18843.90 rows=143269 width=16) (actual time=121.642..142.157 rows=4608 loops=1)
              Hash Cond: (se.id = m.community_id)
              ->  Function Scan on find_all_id_under_node se  (cost=0.25..10.25 rows=1000 width=16) (actual time=0.603..0.604 rows=6 loops=1)
              ->  Hash  (cost=5280.54..5280.54 rows=118054 width=32) (actual time=121.000..121.000 rows=117942 loops=1)
                    Buckets: 65536  Batches: 4  Memory Usage: 2688kB
                    ->  Seq Scan on member_community_map m  (cost=0.00..5280.54 rows=118054 width=32) (actual time=0.010..54.564 rows=117942 loops=1)
  ->  CTE Scan on acl_row  (cost=0.76..1.03 rows=1 width=144) (actual time=0.649..0.650 rows=1 loops=4379)
        CTE acl_row
          ->  Result  (cost=0.00..0.76 rows=1 width=112) (actual time=0.607..0.607 rows=1 loops=4379)
Planning time: 0.650 ms
Execution time: 2999.940 ms
"##);
    let root = plan_root(&plan);
    let total = num_of(root, "Actual Total Time");
    let mut sum = sum_exclusive(root);
    if let Some(ctes) = plan.get("ctes").and_then(|v| v.as_array()) {
        for c in ctes {
            if let Some(obj) = c.as_object() {
                sum += sum_exclusive(obj);
            }
        }
    }
    sum > total * 0.99 && sum < total * 1.01
}

fn io_timing_speed_exclusive() -> bool {
    let plan = plan_of(r##"     Update on a  (cost=0.00..266690.72 rows=0 width=0) (actual time=10.00..11.00 rows=0 loops=1)
         Buffers: shared hit=2 read=4 dirtied=6 written=8
         I/O Timings: read=2 write=4
         ->  Seq Scan on a  (cost=0.00..0.00 rows=10 width=1) (actual time=9.00..10.00 rows=10000000 loops=1)
               Buffers: shared hit=1 read=2 written=4
               I/O Timings: read=1 write=2
    "##);
    let root = plan_root(&plan);
    approx(num_of(root, "*I/O Read Speed (exclusive)"), 2000.0)
        && approx(num_of(root, "*I/O Write Speed (exclusive)"), 2000.0)
}

fn io_timing_speed_all_scopes() -> bool {
    let plan = plan_of(r##" Seq Scan on public.actor  (cost=0.00..4.00 rows=200 width=25) (actual time=0.010..0.030 rows=200 loops=1)
   Output: actor_id, first_name, last_name, last_update
   Buffers: shared read=2
   I/O Timings: read=0.011
 Planning:
   Buffers: shared hit=39 read=7
   I/O Timings: read=0.049
 Planning Time: 0.223 ms
 Execution Time: 0.049 ms
"##);
    let root = plan_root(&plan);
    approx(num_of(root, "*I/O Read Time (all scopes)"), 0.011)
        && approx(num_of(root, "*I/O Read Time speed (all scopes)"), 181818.182)
}


fn cget<'a>(m: &'a Map<String, Value>, key: &str) -> &'a Value {
    m.get(key).unwrap_or(&Value::Null)
}

fn content_root<'a>(content: &'a Map<String, Value>) -> &'a Map<String, Value> {
    content.get("Plan").and_then(|v| v.as_object()).expect("content root")
}

fn plan_extra<'a>(plan: &'a Map<String, Value>) -> &'a Map<String, Value> {
    plan.get("content").and_then(|v| v.as_object()).expect("content")
}

fn child_opt<'a>(m: &'a Map<String, Value>, index: usize) -> Option<&'a Map<String, Value>> {
    m.get("Plans")
        .and_then(|v| v.as_array())
        .and_then(|a| a.get(index))
        .and_then(|v| v.as_object())
}

fn st(m: &Map<String, Value>, key: &str) -> String {
    m.get(key).and_then(|v| v.as_str()).unwrap_or("").to_string()
}

fn slist(m: &Map<String, Value>, key: &str, expected: &[&str]) -> bool {
    match m.get(key).and_then(|v| v.as_array()) {
        Some(a) => {
            a.len() == expected.len()
                && a.iter().zip(expected.iter()).all(|(x, e)| x.as_str() == Some(*e))
        }
        None => false,
    }
}

fn wrapped_line_reassembly() -> bool {
    let plan = plan_of(r##"                                                                  QUERY PLAN
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Sort  (cost=32.88..32.96 rows=32 width=224) (actual time=0.532..0.532 
rows=0 loops=1)
   Sort Key: n.nspname, c.relname
   Sort Method: quicksort  Memory: 25kB
   ->  Hash Join  (cost=1.09..32.08 rows=32 width=224) (actual time=0.5
24..0.525 rows=0 loops=1)
         Hash Cond: (c.relnamespace = n.oid)
         ->  Seq Scan on pg_class c  (cost=0.00..29.84 rows=64 width=73
) (actual time=0.026..0.397 rows=128 loops=1)
               Filter: ((relkind = ANY ('{r,p,v,m,S,f,""}'::"char"[])) AND pg_table_is_visible(oid))
               Rows Removed by Filter: 261
         ->  Hash  (cost=1.07..1.07 rows=2 width=68) (actual time=0.062..0.062 rows=2 loops=1)
               Buckets: 1024  Batches: 1  Memory Usage: 9kB
               ->  Seq Scan on pg_namespace n  (cost=0.00..1.07 rows=2 width=68) (actual time=0.032..0.038 rows=2 loops=1)
                     Filter: ((nspname <> 'pg_catalog'::name) AND (nspname <> 'information_schema'::name) AND (nspname !~ '^pg_toast'::text))
                     Rows Removed by Filter: 3
 Planning Time: 0.636 ms
 Execution Time: 0.616 ms
(15 rows)
"##);
    let root = plan_root(&plan);
    approx(num_of(root, "Actual Total Time"), 0.532)
        && slist(root, "Sort Key", &["n.nspname", "c.relname"])
}

fn mixed_tabs_spaces() -> bool {
    let plan = plan_of(r##"Hash Join  (cost=17116.79..44169.26 rows=4827 width=25)
   Hash Cond: (artist.area = country.id)
   CTE artist_count
 	->  Limit  (cost=17111.20..17111.20 rows=1 width=12)
       	->  Sort  (cost=17111.20..17111.58 rows=152 width=12)
             	Sort Key: (count(*)) DESC
             	->  Finalize GroupAggregate  (cost=17071.93..17110.44 rows=152 width=12)
                   	Group Key: artist_1.area
                   	->  Gather Merge  (cost=17071.93..17107.40 rows=304 width=12)
                         	Workers Planned: 2
                         	->  Sort  (cost=16071.91..16072.29 rows=152 width=12)
                               	Sort Key: artist_1.area
                               	->  Partial HashAggregate  (cost=16064.88..16066.40 rows=152 width=12)
                                     	Group Key: artist_1.area
                                     	->  Parallel Seq Scan on artist artist_1  (cost=0.00..15146.88 rows=183599 width=4)
                                           	Filter: (area IS NOT NULL)
   ->  Seq Scan on artist  (cost=0.00..22383.32 rows=1240532 width=18)
   ->  Hash  (cost=5.58..5.58 rows=1 width=19)
     	->  Hash Join  (cost=0.03..5.58 rows=1 width=19)
           	Hash Cond: (country.id = artist_count.area)
           	->  Seq Scan on country  (cost=0.00..4.57 rows=257 width=15)
           	->  Hash  (cost=0.02..0.02 rows=1 width=4)
                 	->  CTE Scan on artist_count  (cost=0.00..0.02 rows=1 width=4)
"##);
    let root = plan_root(&plan);
    let count = root.get("Plans").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);
    let first_ok = child_opt(root, 0)
        .map(|c| st(c, "Subplan Name") == "CTE artist_count")
        .unwrap_or(false);
    count == 3 && first_ok
}

fn quote_last_line_execution_time() -> bool {
    let content = content_of(r##""Aggregate  (cost=24729994.62..24729994.63 rows=1 width=8) (actual time=126922.755..126980.072 rows=1 loops=1)"
"  Buffers: shared hit=31033317 read=640327, temp read=271916 written=271928"
"Planning:"
"  Buffers: shared hit=227 read=9"
"Planning Time: 14.620 ms"
"JIT:"
"  Functions: 829"
"  Options: Inlining true, Optimization true, Expressions true, Deforming true"
"  Timing: Generation 113.965 ms, Inlining 706.405 ms, Optimization 7838.603 ms, Emission 7380.843 ms, Total 16039.815 ms"
"Execution Time: 127241.780 ms""##);
    approx(num_of(&content, "Execution Time"), 127241.78)
}

fn disabled_node() -> bool {
    let content = content_of(r##"      Seq Scan on t1  (cost=0.00..21370.00 rows=333333 width=22)
        Disabled: true
        Filter: ((c1 + 0) < 1000000)
      Settings: enable_seqscan = 'off'
    "##);
    let root = content_root(&content);
    cget(root, "Disabled").as_bool() == Some(true)
}

fn index_scan_direction() -> bool {
    let plan = plan_of(r##"Limit  (cost=0.27..0.48 rows=1 width=265) (actual time=0.010..0.010 rows=1 loops=1)
  ->  Index Scan Backward using pg_class_relname_nsp_index on pg_class  (cost=0.27..81.94 rows=395 width=265) (actual time=0.009..0.009 rows=1 loops=1)
Planning Time: 0.475 ms
Execution Time: 0.088 ms

"##);
    let root = plan_root(&plan);
    child_opt(root, 0)
        .map(|c| st(c, "Scan Direction") == "Backward"
            && st(c, "Index Name") == "pg_class_relname_nsp_index")
        .unwrap_or(false)
}

fn wal_root_record() -> bool {
    let plan = plan_of(r##"create table titi as select generate_series(1, 5000) as i;
SELECT 5000
explain (analyze, buffers, wal) with x1 as (update titi set i=i+1 returning i)  select * from x1 limit 10 ;
                                                     QUERY PLAN
--------------------------------------------------------------------------------------------------------------------
 Limit  (cost=96.31..96.51 rows=10 width=4) (actual time=0.059..0.086 rows=10 loops=1)
   Buffers: shared hit=21
   WAL: records=20 bytes=1320
   CTE x1
     ->  Update on titi  (cost=0.00..96.31 rows=5865 width=10) (actual time=0.056..11.377 rows=5000 loops=1)
           Buffers: shared hit=10065 read=2 dirtied=22 written=22
           WAL: records=10000 bytes=660000
           ->  Seq Scan on titi  (cost=0.00..96.31 rows=5865 width=10) (actual time=0.019..1.346 rows=5000 loops=1)
                 Buffers: shared hit=23
   ->  CTE Scan on x1  (cost=0.00..117.30 rows=5865 width=4) (actual time=0.058..0.082 rows=10 loops=1)
         Buffers: shared hit=21
         WAL: records=20 bytes=1320
 Planning:
   Buffers: shared hit=3
 Planning Time: 0.098 ms
 Execution Time: 11.797 ms
(16 rows)
"##);
    let root = plan_root(&plan);
    approx(num_of(root, "WAL Records"), 20.0)
        && approx(num_of(root, "WAL Bytes"), 1320.0)
        && approx(num_of(root, "WAL FPI"), 0.0)
}

fn wal_fpi_override() -> bool {
    let plan = plan_of(r##"drop table if exists tutu; create table tutu (i int) WITH (autovacuum_enabled = off) ; explain (analyze,buffers,wal) insert into tutu SELECT i from generate_series (1,10000000) i;
DROP TABLE
Temps : 47,201 ms
CREATE TABLE
Temps : 1,695 ms
                                                                  QUERY PLAN
----------------------------------------------------------------------------------------------------------------------------------------------
 Insert on tutu  (cost=0.00..100000.00 rows=10000000 width=4) (actual time=11543.790..11543.791 rows=0 loops=1)
   Buffers: shared hit=10088481 read=12 dirtied=44248 written=66480, temp read=17090 written=17090
   WAL: records=10000000 fpi=1 bytes=590001027
   ->  Function Scan on generate_series i  (cost=0.00..100000.00 rows=10000000 width=4) (actual time=826.266..2089.643 rows=10000000 loops=1)
         Buffers: temp read=17090 written=17090
 Planning Time: 0.039 ms
 JIT:
   Functions: 2
   Options: Inlining false, Optimization false, Expressions true, Deforming true
   Timing: Generation 0.153 ms, Inlining 0.000 ms, Optimization 0.068 ms, Emission 0.574 ms, Total 0.795 ms
 Execution Time: 11560.132 ms
(11 lignes)
"##);
    let root = plan_root(&plan);
    approx(num_of(root, "WAL Records"), 10000000.0)
        && approx(num_of(root, "WAL Bytes"), 590001027.0)
        && approx(num_of(root, "WAL FPI"), 1.0)
}

fn triggers_count() -> bool {
    let plan = plan_of(r##"      Insert on foo  (cost=0.00..0.01 rows=1 width=24) (actual time=0.504..0.505 rows=0 loops=1)
        ->  Result  (cost=0.00..0.01 rows=1 width=24) (actual time=0.019..0.020 rows=1 loops=1)
      Planning Time: 0.072 ms
      Trigger foo_system_time_generated_always: time=0.401 calls=1
      Trigger foo_system_time_write_history: time=0.260 calls=1
      Execution Time: 0.842 ms
    "##);
    plan_extra(&plan)
        .get("Triggers")
        .and_then(|v| v.as_array())
        .map(|a| a.len() == 2)
        .unwrap_or(false)
}

fn planned_partitions_only() -> bool {
    let plan = plan_of(r##"      HashAggregate  (cost=13760592.78..14872410.30 rows=1736877 width=61)
        Group Key: ml.offer_type, ml.property_main_type, COALESCE(ml.district_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(ml.sale_price, ml.gross_rent_monthly, ml.rent_net_monthly, '0'::real)
        Filter: (array_length(array_agg(ml.id), 1) > 1)
        Planned Partitions: 128
        ->  Seq Scan on post_merged_listings ml  (cost=0.00..11025300.36 rows=32660208 width=33)
              Filter: (duplicate_id IS NULL)
    "##);
    let root = plan_root(&plan);
    approx(num_of(root, "Planned Partitions"), 128.0)
        && !root.contains_key("HashAgg Batches")
        && !root.contains_key("Peak Memory Usage")
        && !root.contains_key("Disk Usage")
}

fn planned_partitions_batches() -> bool {
    let plan = plan_of(r##" HashAggregate  (cost=79468.50..98636.70 rows=940257 width=11) (actual time=231.946..413.626 rows=970675 loops=1)
   Group Key: name
   Planned Partitions: 32  Batches: 193  Memory Usage: 4153kB  Disk Usage: 31752kB
   ->  Seq Scan on t1  (cost=0.00..15406.00 rows=1000000 width=11) (actual time=0.118..36.474 rows=1000000 loops=1)
 Planning Time: 0.201 ms
 Execution Time: 438.186 ms
(6 rows)

Time: 438.861 ms
    "##);
    let root = plan_root(&plan);
    approx(num_of(root, "Planned Partitions"), 32.0)
        && approx(num_of(root, "HashAgg Batches"), 193.0)
        && approx(num_of(root, "Peak Memory Usage"), 4153.0)
        && approx(num_of(root, "Disk Usage"), 31752.0)
}

fn hash_buckets() -> bool {
    let plan = plan_of(r##"                                                           QUERY PLAN                                                            
---------------------------------------------------------------------------------------------------------------------------------
 Hash Join  (cost=471037.90..1304640.12 rows=14999862 width=114) (actual time=1480.644..9895.695 rows=15001138 loops=1)
   Hash Cond: (details.eid = entetes.eid)
   Buffers: shared hit=15940 read=282899, temp read=246065 written=246065
   I/O Timings: shared read=242.957, temp read=317.342 write=830.025
   ->  Seq Scan on details  (cost=0.00..335215.62 rows=15001362 width=65) (actual time=0.014..1114.539 rows=15001289 loops=1)
         Filter: (b IS NOT NULL)
         Buffers: shared hit=1031 read=184171
         I/O Timings: shared read=157.615
   ->  Hash  (cost=238637.70..238637.70 rows=9999056 width=57) (actual time=1474.384..1474.385 rows=9999900 loops=1)
         Buckets: 1048576  Batches: 16  Memory Usage: 62533kB
         Buffers: shared hit=14909 read=98728, temp written=88109
         I/O Timings: shared read=85.342, temp write=294.483
         ->  Seq Scan on entetes  (cost=0.00..238637.70 rows=9999056 width=57) (actual time=0.016..567.370 rows=9999900 loops=1)
               Filter: (a > 100)
               Rows Removed by Filter: 101
               Buffers: shared hit=14909 read=98728
               I/O Timings: shared read=85.342
 Planning:
   Buffers: shared hit=8
 Planning Time: 0.119 ms
 Execution Time: 10244.597 ms
"##);
    let root = plan_root(&plan);
    match child_opt(root, 1) {
        Some(h) => {
            approx(num_of(h, "Hash Buckets"), 1048576.0)
                && approx(num_of(h, "Original Hash Buckets"), 1048576.0)
                && approx(num_of(h, "Hash Batches"), 16.0)
                && approx(num_of(h, "Original Hash Batches"), 16.0)
                && approx(num_of(h, "Peak Memory Usage"), 62533.0)
        }
        None => false,
    }
}

fn merge_node_type() -> bool {
    let plan = plan_of(r##"Merge on public.tb  (cost=1.00..1.05 rows=0 width=0)
"##);
    st(plan_root(&plan), "Node Type") == "Merge"
}

fn subquery_scan_alias() -> bool {
    let plan = plan_of(r##"EXPLAIN SELECT * FROM (SELECT  * FROM pg_class LIMIT 40 ) foo where relname = 'aa';
                               QUERY PLAN
-------------------------------------------------------------------------
 Subquery Scan on foo  (cost=0.00..2.27 rows=1 width=265)
   Filter: (foo.relname = 'aa'::name)
   ->  Limit  (cost=0.00..1.77 rows=40 width=265)
         ->  Seq Scan on pg_class  (cost=0.00..18.10 rows=410 width=265)
(4 rows)
"##);
    let root = plan_root(&plan);
    st(root, "Node Type") == "Subquery Scan" && st(root, "Alias") == "foo"
}

fn group_key_strategy() -> bool {
    let plan = plan_of(r##"HashAggregate  (cost=5471.00..5721.00 rows=20000 width=53)
  Group Key: customer_id, status
  ->  Seq Scan on orders  (cost=0.00..3471.00 rows=200000 width=19)
"##);
    let root = plan_root(&plan);
    st(root, "Strategy") == "Hashed"
        && slist(root, "Group Key", &["customer_id", "status"])
        && approx(num_of(root, "Planned Partitions"), 0.0)
}


fn parse_content(source: &str) -> Option<Map<String, Value>> {
    from_source(source).ok()
}

fn deep_subset(actual: &Value, expected: &Value) -> bool {
    match expected {
        Value::Object(em) => actual
            .as_object()
            .map(|am| em.iter().all(|(k, v)| am.get(k).map(|a| deep_subset(a, v)).unwrap_or(false)))
            .unwrap_or(false),
        Value::Array(ea) => actual
            .as_array()
            .map(|aa| aa.len() == ea.len() && aa.iter().zip(ea.iter()).all(|(a, e)| deep_subset(a, e)))
            .unwrap_or(false),
        Value::Number(en) => match actual.as_f64() {
            Some(an) => {
                let ev = en.as_f64().unwrap_or(f64::NAN);
                (an - ev).abs() <= 1e-6 || (an - ev).abs() <= 1e-6 * ev.abs()
            }
            None => false,
        },
        _ => actual == expected,
    }
}

fn fixture_matches(plan: &str, expect: &str) -> bool {
    let content = match parse_content(plan) {
        Some(c) => c,
        None => return false,
    };
    let expected: Value = match serde_json::from_str(expect) {
        Ok(v) => v,
        Err(_) => return false,
    };
    deep_subset(&Value::Object(content), &expected)
}

fn ft_00() -> bool { fixture_matches(r##"Seq Scan on tenk1  (cost=0.00..333.00 rows=10000 width=148)
"##, r##"{"Plan":{"Node Type":"Seq Scan","Async Capable":false,"Parallel Aware":false,"Relation Name":"tenk1","Alias":"tenk1","Startup Cost":0,"Total Cost":333,"Plan Rows":10000,"Plan Width":148}}"##) }
fn ft_01() -> bool { fixture_matches(r##"Seq Scan on test  (cost=0.00..34.00 rows=2400 width=4) (actual time=0.052..2.183 rows=1000 loops=1)
"##, r##"{"Plan":{"Node Type":"Seq Scan","Relation Name":"test","Startup Cost":0,"Total Cost":34,"Plan Rows":2400,"Plan Width":4,"Actual Startup Time":0.052,"Actual Total Time":2.183,"Actual Rows":1000,"Actual Loops":1}}"##) }
fn ft_02() -> bool { fixture_matches(r##"Seq Scan on test x  (cost=0.00..14.00 rows=1000 width=4) (actual time=0.025..1.985 rows=1000 loops=1)
"##, r##"{"Plan":{"Node Type":"Seq Scan","Relation Name":"test","Alias":"x","Startup Cost":0,"Total Cost":14,"Plan Rows":1000,"Plan Width":4,"Actual Startup Time":0.025,"Actual Total Time":1.985,"Actual Rows":1000,"Actual Loops":1}}"##) }
fn ft_03() -> bool { fixture_matches(r##" Index Scan using test_pkey on test  (cost=0.00..8.27 rows=1 width=4)
   Index Cond: (id = 4)
"##, r##"{"Plan":{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"test_pkey","Relation Name":"test","Startup Cost":0,"Total Cost":8.27,"Plan Rows":1,"Plan Width":4,"Index Cond":"(id = 4)"}}"##) }
fn ft_04() -> bool { fixture_matches(r##" Index Scan using test_pkey on test t  (cost=0.00..8.27 rows=1 width=4) (actual time=0.029..0.033 rows=1 loops=1)
   Index Cond: (id = 1)
"##, r##"{"Plan":{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"test_pkey","Relation Name":"test","Alias":"t","Startup Cost":0,"Total Cost":8.27,"Plan Rows":1,"Plan Width":4,"Actual Startup Time":0.029,"Actual Total Time":0.033,"Actual Rows":1,"Actual Loops":1,"Index Cond":"(id = 1)"}}"##) }
fn ft_05() -> bool { fixture_matches(r##"# explain analyze select * from test as t where id = 1;
                                                    QUERY PLAN
------------------------------------------------------------------------------------------------------------------
 Index Scan using test_pkey on test t  (cost=0.00..8.27 rows=1 width=4) (actual time=0.029..0.033 rows=1 loops=1)
   Index Cond: (id = 1)
 Total runtime: 0.083 ms
(3 rows)
"##, r##"{"Plan":{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"test_pkey","Relation Name":"test","Alias":"t","Startup Cost":0,"Total Cost":8.27,"Plan Rows":1,"Plan Width":4,"Actual Startup Time":0.029,"Actual Total Time":0.033,"Actual Rows":1,"Actual Loops":1,"Index Cond":"(id = 1)"},"Total Runtime":0.083}"##) }
fn ft_06() -> bool { fixture_matches(r##"# explain select 1;
                QUERY PLAN
------------------------------------------
 Result  (cost=0.00..0.01 rows=1 width=0)
(1 row)
"##, r##"{"Plan":{"Node Type":"Result","Startup Cost":0,"Total Cost":0.01,"Plan Rows":1,"Plan Width":0}}"##) }
fn ft_07() -> bool { fixture_matches(r##"# explain select * from test limit 1;
                          QUERY PLAN
--------------------------------------------------------------
 Limit  (cost=0.00..0.01 rows=1 width=4)
   ->  Seq Scan on test  (cost=0.00..14.00 rows=1000 width=4)
(2 rows)
"##, r##"{"Plan":{"Node Type":"Limit","Startup Cost":0,"Total Cost":0.01,"Plan Rows":1,"Plan Width":4,"Plans":[{"Node Type":"Seq Scan","Relation Name":"test","Startup Cost":0,"Total Cost":14,"Plan Rows":1000,"Plan Width":4}]}}"##) }
fn ft_08() -> bool { fixture_matches(r##"# explain select * from test t1 join test t2 on t1.id = t2.id + 1 limit 1;
                                     QUERY PLAN
-------------------------------------------------------------------------------------
 Limit  (cost=0.00..0.34 rows=1 width=8)
   ->  Nested Loop  (cost=0.00..335.20 rows=1000 width=8)
         ->  Seq Scan on test t2  (cost=0.00..14.00 rows=1000 width=4)
         ->  Index Scan using test_pkey on test t1  (cost=0.00..0.31 rows=1 width=4)
               Index Cond: (t1.id = (t2.id + 1))
(5 rows)
"##, r##"{"Plan":{"Node Type":"Limit","Startup Cost":0,"Total Cost":0.34,"Plan Rows":1,"Plan Width":8,"Plans":[{"Node Type":"Nested Loop","Startup Cost":0,"Total Cost":335.2,"Plan Rows":1000,"Plan Width":8,"Plans":[{"Node Type":"Seq Scan","Relation Name":"test","Alias":"t2","Startup Cost":0,"Total Cost":14,"Plan Rows":1000,"Plan Width":4},{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"test_pkey","Relation Name":"test","Alias":"t1","Startup Cost":0,"Total Cost":0.31,"Plan Rows":1,"Plan Width":4,"Index Cond":"(t1.id = (t2.id + 1))"}]}]}}"##) }
fn ft_09() -> bool { fixture_matches(r##"                                                                                             QUERY PLAN
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 Limit  (cost=18602.75..19244.22 rows=1 width=636) (actual time=126.516..126.517 rows=1 loops=1)
   ->  Nested Loop  (cost=0.00..2254139.98 rows=3514 width=636) (actual time=2.637..126.488 rows=30 loops=1)
         ->  Nested Loop  (cost=0.00..1860597.05 rows=73883 width=640) (actual time=0.214..121.234 rows=588 loops=1)
               ->  Index Scan Backward using index_stories_on_published_at on stories  (cost=0.00..405621.70 rows=1303286 width=636) (actual time=0.099..52.447 rows=10157 loops=1)
                     Filter: (NOT hidden)
               ->  Index Scan using index_story_tag_attachments_on_story_id_and_story_tag_id on story_tag_attachments  (cost=0.00..1.10 rows=1 width=4) (actual time=0.005..0.005 rows=0 loops=10157)
                     Index Cond: ((story_tag_attachments.story_id = stories.id) AND (story_tag_attachments.story_tag_id = 166))
         ->  Index Scan using index_story_region_attachments_on_story_id_and_region_id on story_region_attachments  (cost=0.00..5.31 rows=1 width=4) (actual time=0.007..0.007 rows=0 loops=588)
               Index Cond: ((story_region_attachments.story_id = stories.id) AND (story_region_attachments.region_id = 26824))
 Total runtime: 126.625 ms
(10 rows)

"##, r##"{"Plan":{"Node Type":"Limit","Startup Cost":18602.75,"Total Cost":19244.22,"Plan Rows":1,"Plan Width":636,"Actual Startup Time":126.516,"Actual Total Time":126.517,"Actual Rows":1,"Actual Loops":1,"Plans":[{"Node Type":"Nested Loop","Startup Cost":0,"Total Cost":2254139.98,"Plan Rows":3514,"Plan Width":636,"Actual Startup Time":2.637,"Actual Total Time":126.488,"Actual Rows":30,"Actual Loops":1,"Plans":[{"Node Type":"Nested Loop","Startup Cost":0,"Total Cost":1860597.05,"Plan Rows":73883,"Plan Width":640,"Actual Startup Time":0.214,"Actual Total Time":121.234,"Actual Rows":588,"Actual Loops":1,"Plans":[{"Node Type":"Index Scan","Scan Direction":"Backward","Index Name":"index_stories_on_published_at","Relation Name":"stories","Startup Cost":0,"Total Cost":405621.7,"Plan Rows":1303286,"Plan Width":636,"Actual Startup Time":0.099,"Actual Total Time":52.447,"Actual Rows":10157,"Actual Loops":1,"Filter":"(NOT hidden)"},{"Node Type":"Index Scan","Index Name":"index_story_tag_attachments_on_story_id_and_story_tag_id","Relation Name":"story_tag_attachments","Startup Cost":0,"Total Cost":1.1,"Plan Rows":1,"Plan Width":4,"Actual Startup Time":0.005,"Actual Total Time":0.005,"Actual Rows":0,"Actual Loops":10157,"Index Cond":"((story_tag_attachments.story_id = stories.id) AND (story_tag_attachments.story_tag_id = 166))"}]},{"Node Type":"Index Scan","Index Name":"index_story_region_attachments_on_story_id_and_region_id","Relation Name":"story_region_attachments","Startup Cost":0,"Total Cost":5.31,"Plan Rows":1,"Plan Width":4,"Actual Startup Time":0.007,"Actual Total Time":0.007,"Actual Rows":0,"Actual Loops":588,"Index Cond":"((story_region_attachments.story_id = stories.id) AND (story_region_attachments.region_id = 26824))"}]}]},"Total Runtime":126.625}"##) }
fn ft_10() -> bool { fixture_matches(r##"# explain analyze select 1 = (select 1);
                                         QUERY PLAN
--------------------------------------------------------------------------------------------
 Result  (cost=0.01..0.02 rows=1 width=0) (actual time=0.033..0.035 rows=1 loops=1)
   InitPlan
     ->  Result  (cost=0.00..0.01 rows=1 width=0) (actual time=0.003..0.005 rows=1 loops=1)
 Total runtime: 0.234 ms
(4 rows)
"##, r##"{"Plan":{"Node Type":"Result","Startup Cost":0.01,"Total Cost":0.02,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.033,"Actual Total Time":0.035,"Actual Rows":1,"Actual Loops":1,"Plans":[{"Node Type":"Result","Startup Cost":0,"Total Cost":0.01,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.003,"Actual Total Time":0.005,"Actual Rows":1,"Actual Loops":1,"Parent Relationship":"InitPlan","Subplan Name":"InitPlan"}]},"Total Runtime":0.234}"##) }
fn ft_11() -> bool { fixture_matches(r##"# explain select *, (select oid::int4 from pg_class c2 where c2.relname = c.relname) - oid::int4 from pg_class c;
                                              QUERY PLAN
------------------------------------------------------------------------------------------------------
 Seq Scan on pg_class c  (cost=0.00..1885.60 rows=227 width=200)
   SubPlan
     ->  Index Scan using pg_class_relname_nsp_index on pg_class c2  (cost=0.00..8.27 rows=1 width=4)
           Index Cond: (relname = $0)
(4 rows)
"##, r##"{"Plan":{"Node Type":"Seq Scan","Relation Name":"pg_class","Alias":"c","Startup Cost":0,"Total Cost":1885.6,"Plan Rows":227,"Plan Width":200,"Plans":[{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"pg_class_relname_nsp_index","Relation Name":"pg_class","Alias":"c2","Startup Cost":0,"Total Cost":8.27,"Plan Rows":1,"Plan Width":4,"Parent Relationship":"SubPlan","Subplan Name":"SubPlan","Index Cond":"(relname = $0)"}]}}"##) }
fn ft_12() -> bool { fixture_matches(r##"# explain analyze select *, (select 1) + (select oid::int4 from pg_class c2 where c2.relname = c.relname) - oid::int4 from pg_class c;
                                                  QUERY PLAN
---------------------------------------------------------------------------------------------------------------
 Seq Scan on pg_class c  (cost=0.00..1490.37 rows=226 width=200) (actual time=0.092..11.416 rows=226 loops=1)
   InitPlan
     ->  Result  (cost=0.00..0.00 rows=1 width=0) (actual time=0.002..0.004 rows=1 loops=1)
   SubPlan
     ->  Seq Scan on pg_class c2  (cost=0.00..6.57 rows=1 width=4) (actual time=0.022..0.043 rows=1 loops=226)
           Filter: (relname = $1)
 Total runtime: 11.804 ms
(7 rows)

"##, r##"{"Plan":{"Node Type":"Seq Scan","Relation Name":"pg_class","Alias":"c","Startup Cost":0,"Total Cost":1490.37,"Plan Rows":226,"Plan Width":200,"Actual Startup Time":0.092,"Actual Total Time":11.416,"Actual Rows":226,"Actual Loops":1,"Plans":[{"Node Type":"Result","Startup Cost":0,"Total Cost":0,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.002,"Actual Total Time":0.004,"Actual Rows":1,"Actual Loops":1,"Parent Relationship":"InitPlan","Subplan Name":"InitPlan"},{"Node Type":"Seq Scan","Relation Name":"pg_class","Alias":"c2","Startup Cost":0,"Total Cost":6.57,"Plan Rows":1,"Plan Width":4,"Actual Startup Time":0.022,"Actual Total Time":0.043,"Actual Rows":1,"Actual Loops":226,"Parent Relationship":"SubPlan","Subplan Name":"SubPlan","Filter":"(relname = $1)"}]},"Total Runtime":11.804}"##) }
fn ft_13() -> bool { fixture_matches(r##"# - plan copied from http://explain-analyze.info/ - only for testing purposes

 Limit  (cost=186.82..186.82 rows=1 width=359) (actual time=1352.355..1352.405 rows=50 loops=1)
   ->  Sort  (cost=186.82..186.82 rows=1 width=359) (actual time=1352.354..1352.373 rows=50 loops=1)
         Sort Key: s.last_updated_date
         Sort Method:  top-N heapsort  Memory: 50kB
         ->  Nested Loop Left Join  (cost=2.16..186.81 rows=1 width=359) (actual time=0.733..1271.399 rows=13988 loops=1)
               ->  Nested Loop Left Join  (cost=2.16..186.49 rows=1 width=351) (actual time=0.706..1116.630 rows=13988 loops=1)
                     Filter: ((mu.mls_id IS NULL) OR (mu.mls_update_id = (subplan)))
                     ->  Nested Loop  (cost=2.16..19.03 rows=1 width=321) (actual time=0.676..204.019 rows=13988 loops=1)
                           Join Filter: _st_contains(poly_inter_1.the_geom, s.centroid)
                           ->  Subquery Scan poly_inter_1  (cost=2.16..2.44 rows=1 width=32) (actual time=0.520..0.531 rows=1 loops=1)
                                 Filter: (st_geometrytype(poly_inter_1.the_geom) ~~* '%polygon%'::text)
                                 ->  Result  (cost=2.16..2.18 rows=1 width=0) (actual time=0.433..0.441 rows=1 loops=1)
                                       InitPlan
                                         ->  Seq Scan on markets  (cost=0.00..2.15 rows=1 width=32) (actual time=0.015..0.020 rows=1 loops=1)
                                               Filter: ((name)::text = 'seattle'::text)
                                         ->  Result  (cost=0.00..0.01 rows=1 width=0) (actual time=0.048..0.050 rows=1 loops=1)
                           ->  Index Scan using listing_search_keys_centroid on listing_search_keys s  (cost=0.00..16.58 rows=1 width=421) (actual time=0.131..139.571 rows=13988 loops=1)
                                 Index Cond: (poly_inter_1.the_geom && s.centroid)
                                 Filter: ((NOT s.is_townhome) AND (s.property_type_id = ANY ('{3,6}'::bigint[])) AND (s.access_level <= 3) AND (s.listing_type_id = ANY ('{1,2,3}'::integer[])) AND (0 <> (s.search_status_id & 1)) AND (s.last_updated_date >= (('now'::text)::date - '7 days'::interval)))
                     ->  Index Scan using mls_updates_listing_id_and_field on mls_updates mu  (cost=0.00..4.19 rows=7 width=55) (actual time=0.010..0.012 rows=2 loops=13988)
                           Index Cond: (mu.listing_id = s.listing_id)
                           Filter: (((mu.mls_column_updated)::text <> 'search_status_id'::text) OR (((mu.mls_column_old_value)::text <> '32'::text) AND ((mu.mls_column_new_value)::text <> '32'::text)))
                     SubPlan
                       ->  Limit  (cost=23.31..23.31 rows=1 width=16) (actual time=0.030..0.031 rows=1 loops=22035)
                             ->  Sort  (cost=23.31..23.32 rows=7 width=16) (actual time=0.029..0.029 rows=1 loops=22035)
                                   Sort Key: mu2.update_date
                                   Sort Method:  quicksort  Memory: 25kB
                                   ->  Index Scan using mls_updates_listing_id_and_field on mls_updates mu2  (cost=0.00..23.27 rows=7 width=16) (actual time=0.005..0.019 rows=12 loops=22035)
                                         Index Cond: (listing_id = $0)
                                         Filter: (((mls_column_updated)::text <> 'search_status_id'::text) OR (((mls_column_old_value)::text <> '32'::text) AND ((mls_column_new_value)::text <> '32'::text)))
               ->  Index Scan using markets_pkey on markets m  (cost=0.00..0.27 rows=1 width=16) (actual time=0.002..0.003 rows=1 loops=13988)
                     Index Cond: (s.primary_market_id = m.market_id)
 Total runtime: 1352.834 ms
(33 rows)
"##, r##"{"Plan":{"Node Type":"Limit","Startup Cost":186.82,"Total Cost":186.82,"Plan Rows":1,"Plan Width":359,"Actual Startup Time":1352.355,"Actual Total Time":1352.405,"Actual Rows":50,"Actual Loops":1,"Plans":[{"Node Type":"Sort","Startup Cost":186.82,"Total Cost":186.82,"Plan Rows":1,"Plan Width":359,"Actual Startup Time":1352.354,"Actual Total Time":1352.373,"Actual Rows":50,"Actual Loops":1,"Sort Key":["s.last_updated_date"],"Sort Method":"top-N heapsort","Sort Space Used":"50","Sort Space Type":"Memory","Plans":[{"Node Type":"Nested Loop","Join Type":"Left","Startup Cost":2.16,"Total Cost":186.81,"Plan Rows":1,"Plan Width":359,"Actual Startup Time":0.733,"Actual Total Time":1271.399,"Actual Rows":13988,"Actual Loops":1,"Plans":[{"Node Type":"Nested Loop","Join Type":"Left","Startup Cost":2.16,"Total Cost":186.49,"Plan Rows":1,"Plan Width":351,"Actual Startup Time":0.706,"Actual Total Time":1116.63,"Actual Rows":13988,"Actual Loops":1,"Filter":"((mu.mls_id IS NULL) OR (mu.mls_update_id = (subplan)))","Plans":[{"Node Type":"Nested Loop","Startup Cost":2.16,"Total Cost":19.03,"Plan Rows":1,"Plan Width":321,"Actual Startup Time":0.676,"Actual Total Time":204.019,"Actual Rows":13988,"Actual Loops":1,"Join Filter":"_st_contains(poly_inter_1.the_geom, s.centroid)","Plans":[{"Node Type":"Subquery Scan poly_inter_1","Startup Cost":2.16,"Total Cost":2.44,"Plan Rows":1,"Plan Width":32,"Actual Startup Time":0.52,"Actual Total Time":0.531,"Actual Rows":1,"Actual Loops":1,"Filter":"(st_geometrytype(poly_inter_1.the_geom) ~~* '%polygon%'::text)","Plans":[{"Node Type":"Result","Startup Cost":2.16,"Total Cost":2.18,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.433,"Actual Total Time":0.441,"Actual Rows":1,"Actual Loops":1,"Plans":[{"Node Type":"Seq Scan","Relation Name":"markets","Startup Cost":0,"Total Cost":2.15,"Plan Rows":1,"Plan Width":32,"Actual Startup Time":0.015,"Actual Total Time":0.02,"Actual Rows":1,"Actual Loops":1,"Parent Relationship":"InitPlan","Subplan Name":"InitPlan","Filter":"((name)::text = 'seattle'::text)"},{"Node Type":"Result","Startup Cost":0,"Total Cost":0.01,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.048,"Actual Total Time":0.05,"Actual Rows":1,"Actual Loops":1,"Parent Relationship":"InitPlan","Subplan Name":"InitPlan"}]}]},{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"listing_search_keys_centroid","Relation Name":"listing_search_keys","Alias":"s","Startup Cost":0,"Total Cost":16.58,"Plan Rows":1,"Plan Width":421,"Actual Startup Time":0.131,"Actual Total Time":139.571,"Actual Rows":13988,"Actual Loops":1,"Index Cond":"(poly_inter_1.the_geom && s.centroid)","Filter":"((NOT s.is_townhome) AND (s.property_type_id = ANY ('{3,6}'::bigint[])) AND (s.access_level <= 3) AND (s.listing_type_id = ANY ('{1,2,3}'::integer[])) AND (0 <> (s.search_status_id & 1)) AND (s.last_updated_date >= (('now'::text)::date - '7 days'::interval)))"}]},{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"mls_updates_listing_id_and_field","Relation Name":"mls_updates","Alias":"mu","Startup Cost":0,"Total Cost":4.19,"Plan Rows":7,"Plan Width":55,"Actual Startup Time":0.01,"Actual Total Time":0.012,"Actual Rows":2,"Actual Loops":13988,"Index Cond":"(mu.listing_id = s.listing_id)","Filter":"(((mu.mls_column_updated)::text <> 'search_status_id'::text) OR (((mu.mls_column_old_value)::text <> '32'::text) AND ((mu.mls_column_new_value)::text <> '32'::text)))"},{"Node Type":"Limit","Startup Cost":23.31,"Total Cost":23.31,"Plan Rows":1,"Plan Width":16,"Actual Startup Time":0.03,"Actual Total Time":0.031,"Actual Rows":1,"Actual Loops":22035,"Parent Relationship":"SubPlan","Subplan Name":"SubPlan","Plans":[{"Node Type":"Sort","Startup Cost":23.31,"Total Cost":23.32,"Plan Rows":7,"Plan Width":16,"Actual Startup Time":0.029,"Actual Total Time":0.029,"Actual Rows":1,"Actual Loops":22035,"Sort Key":["mu2.update_date"],"Sort Method":"quicksort","Sort Space Used":"25","Sort Space Type":"Memory","Plans":[{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"mls_updates_listing_id_and_field","Relation Name":"mls_updates","Alias":"mu2","Startup Cost":0,"Total Cost":23.27,"Plan Rows":7,"Plan Width":16,"Actual Startup Time":0.005,"Actual Total Time":0.019,"Actual Rows":12,"Actual Loops":22035,"Index Cond":"(listing_id = $0)","Filter":"(((mls_column_updated)::text <> 'search_status_id'::text) OR (((mls_column_old_value)::text <> '32'::text) AND ((mls_column_new_value)::text <> '32'::text)))"}]}]}]},{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"markets_pkey","Relation Name":"markets","Alias":"m","Startup Cost":0,"Total Cost":0.27,"Plan Rows":1,"Plan Width":16,"Actual Startup Time":0.002,"Actual Total Time":0.003,"Actual Rows":1,"Actual Loops":13988,"Index Cond":"(s.primary_market_id = m.market_id)"}]}]}]},"Total Runtime":1352.834}"##) }
fn ft_14() -> bool { fixture_matches(r##"Hash Join  (cost=6.65..104.10 rows=1600 width=1363) (actual time=0.002..0.002 rows=0 loops=1)
  Hash Cond: ("outer".uzytkownik_id = "inner".id)
  ->  Seq Scan on uprawnienie_uzytkownik uu  (cost=0.00..26.00 rows=1600 width=17) (actual time=0.001..0.001 rows=0 loops=1)
  ->  Hash  (cost=6.12..6.12 rows=212 width=1346) (never executed)
        ->  Seq Scan on uprawnienie u  (cost=0.00..6.12 rows=212 width=1346) (never executed)
"##, r##"{"Plan":{"Node Type":"Hash Join","Startup Cost":6.65,"Total Cost":104.1,"Plan Rows":1600,"Plan Width":1363,"Actual Startup Time":0.002,"Actual Total Time":0.002,"Actual Rows":0,"Actual Loops":1,"Hash Cond":"(\"outer\".uzytkownik_id = \"inner\".id)","Plans":[{"Node Type":"Seq Scan","Relation Name":"uprawnienie_uzytkownik","Alias":"uu","Startup Cost":0,"Total Cost":26,"Plan Rows":1600,"Plan Width":17,"Actual Startup Time":0.001,"Actual Total Time":0.001,"Actual Rows":0,"Actual Loops":1},{"Node Type":"Hash","Startup Cost":6.12,"Total Cost":6.12,"Plan Rows":212,"Plan Width":1346,"Actual Loops":0,"Actual Rows":0,"Plans":[{"Node Type":"Seq Scan","Relation Name":"uprawnienie","Alias":"u","Startup Cost":0,"Total Cost":6.12,"Plan Rows":212,"Plan Width":1346,"Actual Loops":0,"Actual Rows":0}]}]}}"##) }
fn ft_15() -> bool { fixture_matches(r##" Bitmap Heap Scan on poi  (cost=39.88..3946.78 rows=1113 width=323)
   Recheck Cond: ((poi_id >= 100) AND (poi_id <= 3000))
   ->  Bitmap Index Scan on poi_pkey  (cost=0.00..39.60 rows=1113 width=0)
         Index Cond: ((poi_id >= 100) AND (poi_id <= 3000))

"##, r##"{"Plan":{"Node Type":"Bitmap Heap Scan","Relation Name":"poi","Startup Cost":39.88,"Total Cost":3946.78,"Plan Rows":1113,"Plan Width":323,"Recheck Cond":"((poi_id >= 100) AND (poi_id <= 3000))","Plans":[{"Node Type":"Bitmap Index Scan","Index Name":"poi_pkey","Startup Cost":0,"Total Cost":39.6,"Plan Rows":1113,"Plan Width":0,"Index Cond":"((poi_id >= 100) AND (poi_id <= 3000))"}]}}"##) }
fn ft_16() -> bool { fixture_matches(r##" Limit  (cost=0.00..0.08 rows=2 width=36)
   ->  Index Scan Backward using w on x  (cost=0.00..5770.49 rows=149976 width=36)
"##, r##"{"Plan":{"Node Type":"Limit","Startup Cost":0,"Total Cost":0.08,"Plan Rows":2,"Plan Width":36,"Plans":[{"Node Type":"Index Scan","Scan Direction":"Backward","Index Name":"w","Relation Name":"x","Startup Cost":0,"Total Cost":5770.49,"Plan Rows":149976,"Plan Width":36}]}}"##) }
fn ft_17() -> bool { fixture_matches(r##" ->  Index Scan using id_contact_event_idx on of_contact_event e  (cost=30.00..34.33 rows=1 width=40) (actual time=0.003..0.003 rows=1 loops=19499)
       Index Cond: (e.id_contact_event = CASE WHEN (("outer".client_status_cc = 2) AND ("outer".client_status_unit = 2) AND ("outer".id_employee_responsible IS NULL) AND ("outer".id_unit_responsible IS NULL)) THEN (subplan) ELSE "outer".latest_event END)
       SubPlan
         ->  Limit  (cost=29.99..30.00 rows=1 width=19) (actual time=0.018..0.018 rows=1 loops=5570)
               ->  Sort  (cost=29.99..30.00 rows=4 width=19) (actual time=0.018..0.018 rows=1 loops=5570)
                     Sort Key: status, date_registration, visit
                     ->  Index Scan using idx_of_contact_event_id_contact_client on of_contact_event ev2  (cost=0.00..29.95 rows=4 width=19) (actual time=0.006..0.012 rows=6 loops=5570)
                           Index Cond: (id_contact_client = $0)
                           Filter: (id_contact_event < $1)
         ->  Limit  (cost=29.99..30.00 rows=1 width=19) (never executed)
               ->  Sort  (cost=29.99..30.00 rows=4 width=19) (never executed)
                     Sort Key: status, date_registration, visit
                     ->  Index Scan using idx_of_contact_event_id_contact_client on of_contact_event ev2  (cost=0.00..29.95 rows=4 width=19) (never executed)
                           Index Cond: (id_contact_client = $0)
                           Filter: (id_contact_event < $1)
"##, r##"{"Plan":{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"id_contact_event_idx","Relation Name":"of_contact_event","Alias":"e","Startup Cost":30,"Total Cost":34.33,"Plan Rows":1,"Plan Width":40,"Actual Startup Time":0.003,"Actual Total Time":0.003,"Actual Rows":1,"Actual Loops":19499,"Index Cond":"(e.id_contact_event = CASE WHEN ((\"outer\".client_status_cc = 2) AND (\"outer\".client_status_unit = 2) AND (\"outer\".id_employee_responsible IS NULL) AND (\"outer\".id_unit_responsible IS NULL)) THEN (subplan) ELSE \"outer\".latest_event END)","Plans":[{"Node Type":"Limit","Startup Cost":29.99,"Total Cost":30,"Plan Rows":1,"Plan Width":19,"Actual Startup Time":0.018,"Actual Total Time":0.018,"Actual Rows":1,"Actual Loops":5570,"Parent Relationship":"SubPlan","Subplan Name":"SubPlan","Plans":[{"Node Type":"Sort","Startup Cost":29.99,"Total Cost":30,"Plan Rows":4,"Plan Width":19,"Actual Startup Time":0.018,"Actual Total Time":0.018,"Actual Rows":1,"Actual Loops":5570,"Sort Key":["status","date_registration","visit"],"Plans":[{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"idx_of_contact_event_id_contact_client","Relation Name":"of_contact_event","Alias":"ev2","Startup Cost":0,"Total Cost":29.95,"Plan Rows":4,"Plan Width":19,"Actual Startup Time":0.006,"Actual Total Time":0.012,"Actual Rows":6,"Actual Loops":5570,"Index Cond":"(id_contact_client = $0)","Filter":"(id_contact_event < $1)"}]}]},{"Node Type":"Limit","Startup Cost":29.99,"Total Cost":30,"Plan Rows":1,"Plan Width":19,"Actual Loops":0,"Actual Rows":0,"Parent Relationship":"SubPlan","Subplan Name":"SubPlan","Plans":[{"Node Type":"Sort","Startup Cost":29.99,"Total Cost":30,"Plan Rows":4,"Plan Width":19,"Actual Loops":0,"Actual Rows":0,"Sort Key":["status","date_registration","visit"],"Plans":[{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"idx_of_contact_event_id_contact_client","Relation Name":"of_contact_event","Alias":"ev2","Startup Cost":0,"Total Cost":29.95,"Plan Rows":4,"Plan Width":19,"Actual Loops":0,"Actual Rows":0,"Index Cond":"(id_contact_client = $0)","Filter":"(id_contact_event < $1)"}]}]}]}}"##) }
fn ft_18() -> bool { fixture_matches(r##"
Sort  (cost=19735.07..19735.08 rows=1 width=108) (actual time=984.815..984.898 rows=705 loops=1)
  Sort Key: gp.idx_gabinetu, (COALESCE(z.godz_otwarcia, gp.godz_otwarcia))
  Sort Method:  quicksort  Memory: 124kB
  ->  Nested Loop Left Join  (cost=14296.97..19735.06 rows=1 width=108) (actual time=625.283..983.437 rows=705 loops=1)
        Join Filter: (($1 + s.s) <= COALESCE(gu.data_do, '3000-01-01'::date))
        Filter: (((CASE WHEN (z.idx_gdz_pracy IS NULL) THEN gp.czy_ukryty ELSE z.czy_ukryty END)::"char" = 'N'::"char") OR ps_chkupr(246) OR (NOT (gu.idx_pracownika IS NULL)))
        ->  Hash Left Join  (cost=14296.72..19733.98 rows=1 width=114) (actual time=624.938..960.508 rows=705 loops=1)
              Hash Cond: ((gp.idx_gdz_pracy = z.idx_gdz_pracy_wyjsciowy) AND (($1 + s.s) = z.wazny_od))
              Filter: ((((CASE WHEN (z.idx_gdz_pracy IS NULL) THEN gp.czy_lokalny ELSE z.czy_lokalny END)::"char" = 'N'::"char") OR (g.idx_placowki = ps_akt_placowka())) AND ((SubPlan 1) = $5))
              ->  Merge Join  (cost=3509.52..3900.07 rows=1192 width=74) (actual time=490.880..654.251 rows=14703 loops=1)
                    Merge Cond: ((date_part('dow'::text, (($1 + s.s))::timestamp without time zone)) = ((gp.dzien_tygodnia)::double precision))
                    Join Filter: ((($1 + s.s) >= gp.wazny_od) AND (($1 + s.s) <= gp.wazny_do))
                    ->  Sort  (cost=64.83..67.33 rows=1000 width=4) (actual time=0.056..0.059 rows=11 loops=1)
                          Sort Key: (date_part('dow'::text, (($1 + s.s))::timestamp without time zone))
                          Sort Method:  quicksort  Memory: 25kB
                          ->  Function Scan on generate_series s  (cost=0.00..15.00 rows=1000 width=4) (actual time=0.032..0.041 rows=11 loops=1)
                    ->  Sort  (cost=3444.69..3450.06 rows=2146 width=70) (actual time=490.804..561.202 rows=173693 loops=1)
                          Sort Key: ((gp.dzien_tygodnia)::double precision)
                          Sort Method:  external sort  Disk: 11312kB
                          ->  Nested Loop  (cost=46.42..3325.94 rows=2146 width=70) (actual time=0.461..198.491 rows=113394 loops=1)
                                ->  Hash Join  (cost=46.42..129.38 rows=19 width=12) (actual time=0.437..3.798 rows=1252 loops=1)
                                      Hash Cond: (g.idx_placowki = p.idx_placowki)
                                      ->  Seq Scan on s_gabinety g  (cost=0.00..82.63 rows=38 width=8) (actual time=0.009..1.747 rows=2443 loops=1)
                                            Filter: ((idx_placowki = $3) OR ($3 = 0))
                                      ->  Hash  (cost=46.19..46.19 rows=19 width=4) (actual time=0.416..0.416 rows=134 loops=1)
                                            ->  Seq Scan on s_lokalizacje_placowki p  (cost=0.00..46.19 rows=19 width=4) (actual time=0.008..0.342 rows=134 loops=1)
                                                  Filter: ((idx_lokalizacji = $5) OR ($5 = 0))
                                ->  Index Scan using s_gabinety_godziny_pracy_idx on s_gabinety_godziny_pracy gp  (cost=0.00..166.83 rows=113 width=62) (actual time=0.007..0.107 rows=91 loops=1252)
                                      Index Cond: (gp.idx_gabinetu = g.idx_gabinetu)
                                      Filter: (gp.idx_gdz_pracy_wyjsciowy IS NULL)
              ->  Hash  (cost=5637.28..5637.28 rows=207928 width=50) (actual time=122.250..122.250 rows=2778 loops=1)
                    ->  Seq Scan on s_gabinety_godziny_pracy z  (cost=0.00..5637.28 rows=207928 width=50) (actual time=0.014..94.524 rows=207931 loops=1)
              SubPlan 1
                ->  Aggregate  (cost=36.21..36.22 rows=1 width=0) (actual time=0.005..0.005 rows=1 loops=11341)
                      ->  Bitmap Heap Scan on s_gabinety_godziny_pracy_uslugi gpu  (cost=33.18..36.21 rows=1 width=0) (actual time=0.004..0.004 rows=0 loops=11341)
                            Recheck Cond: ((idx_gdz_pracy = COALESCE($0, $1)) AND (idx_wariantu_suslugi = ANY ($4)))
                            ->  Bitmap Index Scan on s_gabinety_godziny_pracy_uslugi_pkey  (cost=0.00..33.18 rows=1 width=0) (actual time=0.003..0.003 rows=0 loops=11341)
                                  Index Cond: ((idx_gdz_pracy = COALESCE($0, $1)) AND (idx_wariantu_suslugi = ANY ($4)))
        ->  Index Scan using s_lokalizacje_placowki_upr_graf_ukr_pkey on s_lokalizacje_placowki_upr_graf_ukr gu  (cost=0.25..0.81 rows=1 width=16) (actual time=0.000..0.000 rows=0 loops=705)
              Index Cond: ((gu.idx_placowki = p.idx_placowki) AND (gu.idx_pracownika = ps_akt_user()) AND (($1 + s.s) >= gu.data_od))
Total runtime: 987.935 ms
"##, r##"{"Plan":{"Node Type":"Sort","Startup Cost":19735.07,"Total Cost":19735.08,"Plan Rows":1,"Plan Width":108,"Actual Startup Time":984.815,"Actual Total Time":984.898,"Actual Rows":705,"Actual Loops":1,"Sort Key":["gp.idx_gabinetu","(COALESCE(z.godz_otwarcia, gp.godz_otwarcia))"],"Sort Method":"quicksort","Sort Space Used":"124","Sort Space Type":"Memory","Plans":[{"Node Type":"Nested Loop","Join Type":"Left","Startup Cost":14296.97,"Total Cost":19735.06,"Plan Rows":1,"Plan Width":108,"Actual Startup Time":625.283,"Actual Total Time":983.437,"Actual Rows":705,"Actual Loops":1,"Join Filter":"(($1 + s.s) <= COALESCE(gu.data_do, '3000-01-01'::date))","Filter":"(((CASE WHEN (z.idx_gdz_pracy IS NULL) THEN gp.czy_ukryty ELSE z.czy_ukryty END)::\"char\" = 'N'::\"char\") OR ps_chkupr(246) OR (NOT (gu.idx_pracownika IS NULL)))","Plans":[{"Node Type":"Hash Join","Join Type":"Left","Startup Cost":14296.72,"Total Cost":19733.98,"Plan Rows":1,"Plan Width":114,"Actual Startup Time":624.938,"Actual Total Time":960.508,"Actual Rows":705,"Actual Loops":1,"Hash Cond":"((gp.idx_gdz_pracy = z.idx_gdz_pracy_wyjsciowy) AND (($1 + s.s) = z.wazny_od))","Filter":"((((CASE WHEN (z.idx_gdz_pracy IS NULL) THEN gp.czy_lokalny ELSE z.czy_lokalny END)::\"char\" = 'N'::\"char\") OR (g.idx_placowki = ps_akt_placowka())) AND ((SubPlan 1) = $5))","Plans":[{"Node Type":"Merge Join","Startup Cost":3509.52,"Total Cost":3900.07,"Plan Rows":1192,"Plan Width":74,"Actual Startup Time":490.88,"Actual Total Time":654.251,"Actual Rows":14703,"Actual Loops":1,"Merge Cond":"((date_part('dow'::text, (($1 + s.s))::timestamp without time zone)) = ((gp.dzien_tygodnia)::double precision))","Join Filter":"((($1 + s.s) >= gp.wazny_od) AND (($1 + s.s) <= gp.wazny_do))","Plans":[{"Node Type":"Sort","Startup Cost":64.83,"Total Cost":67.33,"Plan Rows":1000,"Plan Width":4,"Actual Startup Time":0.056,"Actual Total Time":0.059,"Actual Rows":11,"Actual Loops":1,"Sort Key":["(date_part('dow'::text, (($1 + s.s))::timestamp without time zone))"],"Sort Method":"quicksort","Sort Space Used":"25","Sort Space Type":"Memory","Plans":[{"Node Type":"Function Scan","Function Name":"generate_series","Alias":"s","Startup Cost":0,"Total Cost":15,"Plan Rows":1000,"Plan Width":4,"Actual Startup Time":0.032,"Actual Total Time":0.041,"Actual Rows":11,"Actual Loops":1}]},{"Node Type":"Sort","Startup Cost":3444.69,"Total Cost":3450.06,"Plan Rows":2146,"Plan Width":70,"Actual Startup Time":490.804,"Actual Total Time":561.202,"Actual Rows":173693,"Actual Loops":1,"Sort Key":["((gp.dzien_tygodnia)::double precision)"],"Sort Method":"external sort","Sort Space Used":"11312","Sort Space Type":"Disk","Plans":[{"Node Type":"Nested Loop","Startup Cost":46.42,"Total Cost":3325.94,"Plan Rows":2146,"Plan Width":70,"Actual Startup Time":0.461,"Actual Total Time":198.491,"Actual Rows":113394,"Actual Loops":1,"Plans":[{"Node Type":"Hash Join","Startup Cost":46.42,"Total Cost":129.38,"Plan Rows":19,"Plan Width":12,"Actual Startup Time":0.437,"Actual Total Time":3.798,"Actual Rows":1252,"Actual Loops":1,"Hash Cond":"(g.idx_placowki = p.idx_placowki)","Plans":[{"Node Type":"Seq Scan","Relation Name":"s_gabinety","Alias":"g","Startup Cost":0,"Total Cost":82.63,"Plan Rows":38,"Plan Width":8,"Actual Startup Time":0.009,"Actual Total Time":1.747,"Actual Rows":2443,"Actual Loops":1,"Filter":"((idx_placowki = $3) OR ($3 = 0))"},{"Node Type":"Hash","Startup Cost":46.19,"Total Cost":46.19,"Plan Rows":19,"Plan Width":4,"Actual Startup Time":0.416,"Actual Total Time":0.416,"Actual Rows":134,"Actual Loops":1,"Plans":[{"Node Type":"Seq Scan","Relation Name":"s_lokalizacje_placowki","Alias":"p","Startup Cost":0,"Total Cost":46.19,"Plan Rows":19,"Plan Width":4,"Actual Startup Time":0.008,"Actual Total Time":0.342,"Actual Rows":134,"Actual Loops":1,"Filter":"((idx_lokalizacji = $5) OR ($5 = 0))"}]}]},{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"s_gabinety_godziny_pracy_idx","Relation Name":"s_gabinety_godziny_pracy","Alias":"gp","Startup Cost":0,"Total Cost":166.83,"Plan Rows":113,"Plan Width":62,"Actual Startup Time":0.007,"Actual Total Time":0.107,"Actual Rows":91,"Actual Loops":1252,"Index Cond":"(gp.idx_gabinetu = g.idx_gabinetu)","Filter":"(gp.idx_gdz_pracy_wyjsciowy IS NULL)"}]}]}]},{"Node Type":"Hash","Startup Cost":5637.28,"Total Cost":5637.28,"Plan Rows":207928,"Plan Width":50,"Actual Startup Time":122.25,"Actual Total Time":122.25,"Actual Rows":2778,"Actual Loops":1,"Plans":[{"Node Type":"Seq Scan","Relation Name":"s_gabinety_godziny_pracy","Alias":"z","Startup Cost":0,"Total Cost":5637.28,"Plan Rows":207928,"Plan Width":50,"Actual Startup Time":0.014,"Actual Total Time":94.524,"Actual Rows":207931,"Actual Loops":1}]},{"Node Type":"Aggregate","Startup Cost":36.21,"Total Cost":36.22,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.005,"Actual Total Time":0.005,"Actual Rows":1,"Actual Loops":11341,"Parent Relationship":"SubPlan","Subplan Name":"SubPlan 1","Plans":[{"Node Type":"Bitmap Heap Scan","Relation Name":"s_gabinety_godziny_pracy_uslugi","Alias":"gpu","Startup Cost":33.18,"Total Cost":36.21,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.004,"Actual Total Time":0.004,"Actual Rows":0,"Actual Loops":11341,"Recheck Cond":"((idx_gdz_pracy = COALESCE($0, $1)) AND (idx_wariantu_suslugi = ANY ($4)))","Plans":[{"Node Type":"Bitmap Index Scan","Index Name":"s_gabinety_godziny_pracy_uslugi_pkey","Startup Cost":0,"Total Cost":33.18,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.003,"Actual Total Time":0.003,"Actual Rows":0,"Actual Loops":11341,"Index Cond":"((idx_gdz_pracy = COALESCE($0, $1)) AND (idx_wariantu_suslugi = ANY ($4)))"}]}]}]},{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"s_lokalizacje_placowki_upr_graf_ukr_pkey","Relation Name":"s_lokalizacje_placowki_upr_graf_ukr","Alias":"gu","Startup Cost":0.25,"Total Cost":0.81,"Plan Rows":1,"Plan Width":16,"Actual Startup Time":0,"Actual Total Time":0,"Actual Rows":0,"Actual Loops":705,"Index Cond":"((gu.idx_placowki = p.idx_placowki) AND (gu.idx_pracownika = ps_akt_user()) AND (($1 + s.s) >= gu.data_od))"}]}]},"Total Runtime":987.935}"##) }
fn ft_19() -> bool { fixture_matches(r##""Limit  (cost=0.00..0.08 rows=2 width=190) (actual time=0.033..0.043 rows=2 loops=1)"
"  ->  Seq Scan on pg_class  (cost=0.00..13.61 rows=361 width=190) (actual time=0.028..0.030 rows=2 loops=1)"
"##, r##"{"Plan":{"Node Type":"Limit","Startup Cost":0,"Total Cost":0.08,"Plan Rows":2,"Plan Width":190,"Actual Startup Time":0.033,"Actual Total Time":0.043,"Actual Rows":2,"Actual Loops":1,"Plans":[{"Node Type":"Seq Scan","Relation Name":"pg_class","Startup Cost":0,"Total Cost":13.61,"Plan Rows":361,"Plan Width":190,"Actual Startup Time":0.028,"Actual Total Time":0.03,"Actual Rows":2,"Actual Loops":1}]}}"##) }
fn ft_20() -> bool { fixture_matches(r##"                                                                      QUERY PLAN                                                                      
------------------------------------------------------------------------------------------------------------------------------------------------------
 Index Scan using pg_class_oid_index on pg_class  (cost=0.17..8.44 rows=1 width=194) (actual time=0.035..0.036 rows=1 loops=1)
   Index Cond: (oid = $1)
   InitPlan 2 (returns $1)
     ->  Result  (cost=0.16..0.17 rows=1 width=0) (actual time=0.023..0.023 rows=1 loops=1)
           InitPlan 1 (returns $0)
             ->  Limit  (cost=0.00..0.16 rows=1 width=4) (actual time=0.020..0.021 rows=1 loops=1)
                   ->  Index Scan using pg_class_oid_index on pg_class  (cost=0.00..44.47 rows=281 width=4) (actual time=0.019..0.019 rows=1 loops=1)
                         Index Cond: (oid IS NOT NULL)
 Total runtime: 0.081 ms
"##, r##"{"Plan":{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"pg_class_oid_index","Relation Name":"pg_class","Startup Cost":0.17,"Total Cost":8.44,"Plan Rows":1,"Plan Width":194,"Actual Startup Time":0.035,"Actual Total Time":0.036,"Actual Rows":1,"Actual Loops":1,"Index Cond":"(oid = $1)","Plans":[{"Node Type":"Result","Startup Cost":0.16,"Total Cost":0.17,"Plan Rows":1,"Plan Width":0,"Actual Startup Time":0.023,"Actual Total Time":0.023,"Actual Rows":1,"Actual Loops":1,"Parent Relationship":"InitPlan","Subplan Name":"InitPlan 2 (returns $1)","Plans":[{"Node Type":"Limit","Startup Cost":0,"Total Cost":0.16,"Plan Rows":1,"Plan Width":4,"Actual Startup Time":0.02,"Actual Total Time":0.021,"Actual Rows":1,"Actual Loops":1,"Parent Relationship":"InitPlan","Subplan Name":"InitPlan 1 (returns $0)","Plans":[{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"pg_class_oid_index","Relation Name":"pg_class","Startup Cost":0,"Total Cost":44.47,"Plan Rows":281,"Plan Width":4,"Actual Startup Time":0.019,"Actual Total Time":0.019,"Actual Rows":1,"Actual Loops":1,"Index Cond":"(oid IS NOT NULL)"}]}]}]},"Total Runtime":0.081}"##) }
fn ft_21() -> bool { fixture_matches(r##""GroupAggregate  (cost=265935.63..285867.54 rows=137462 width=26)"
"  Filter: (count(*) > 1)"
"  ->  Sort  (cost=265935.63..269372.16 rows=1374614 width=26)"
"        Sort Key: latitude_decimal, longitude_decimal"
"        ->  Seq Scan on temp_city  (cost=0.00..27121.14 rows=1374614 width=26)"

"##, r##"{"Plan":{"Node Type":"Aggregate","Strategy":"Sorted","Partial Mode":"Simple","Startup Cost":265935.63,"Total Cost":285867.54,"Plan Rows":137462,"Plan Width":26,"Filter":"(count(*) > 1)","Plans":[{"Node Type":"Sort","Startup Cost":265935.63,"Total Cost":269372.16,"Plan Rows":1374614,"Plan Width":26,"Sort Key":["latitude_decimal","longitude_decimal"],"Plans":[{"Node Type":"Seq Scan","Relation Name":"temp_city","Startup Cost":0,"Total Cost":27121.14,"Plan Rows":1374614,"Plan Width":26}]}]}}"##) }
fn ft_22() -> bool { fixture_matches(r##"'Sort  (cost=454214.22..454299.03 rows=33927 width=49) (actual time=102348.421..102386.840 rows=24935 loops=1)'
'  Sort Key: ak.artikelnr_list'
'  Sort Method:  quicksort  Memory: 3536kB'
'  ->  Nested Loop  (cost=0.00..451661.19 rows=33927 width=49) (actual time=8.244..101961.451 rows=24935 loops=1)'
'        Join Filter: ((aas.artikelnrforvererbung)::text = ANY ((ARRAY[ak.keysum1, ak.keysum2, ak.keysum3, ak.keysum4, ak.keysum5, ak.keysum6])::text[]))'
'        ->  Seq Scan on t_artikelkey ak  (cost=0.00..2312.30 rows=25130 width=237) (actual time=0.018..177.510 rows=25112 loops=1)'
'        ->  Materialize  (cost=0.00..26.72 rows=894 width=10) (actual time=0.002..1.322 rows=894 loops=25112)'
'              ->  Seq Scan on artikel_as aas  (cost=0.00..22.25 rows=894 width=10) (actual time=0.026..2.017 rows=894 loops=1)'
'                    Filter: ((aas_ask)::text = 'PRE'::text)'
'Total runtime: 102423.136 ms'
"##, r##"{"Plan":{"Node Type":"Sort","Startup Cost":454214.22,"Total Cost":454299.03,"Plan Rows":33927,"Plan Width":49,"Actual Startup Time":102348.421,"Actual Total Time":102386.84,"Actual Rows":24935,"Actual Loops":1,"Sort Key":["ak.artikelnr_list"],"Sort Method":"quicksort","Sort Space Used":"3536","Sort Space Type":"Memory","Plans":[{"Node Type":"Nested Loop","Startup Cost":0,"Total Cost":451661.19,"Plan Rows":33927,"Plan Width":49,"Actual Startup Time":8.244,"Actual Total Time":101961.451,"Actual Rows":24935,"Actual Loops":1,"Join Filter":"((aas.artikelnrforvererbung)::text = ANY ((ARRAY[ak.keysum1, ak.keysum2, ak.keysum3, ak.keysum4, ak.keysum5, ak.keysum6])::text[]))","Plans":[{"Node Type":"Seq Scan","Relation Name":"t_artikelkey","Alias":"ak","Startup Cost":0,"Total Cost":2312.3,"Plan Rows":25130,"Plan Width":237,"Actual Startup Time":0.018,"Actual Total Time":177.51,"Actual Rows":25112,"Actual Loops":1},{"Node Type":"Materialize","Startup Cost":0,"Total Cost":26.72,"Plan Rows":894,"Plan Width":10,"Actual Startup Time":0.002,"Actual Total Time":1.322,"Actual Rows":894,"Actual Loops":25112,"Plans":[{"Node Type":"Seq Scan","Relation Name":"artikel_as","Alias":"aas","Startup Cost":0,"Total Cost":22.25,"Plan Rows":894,"Plan Width":10,"Actual Startup Time":0.026,"Actual Total Time":2.017,"Actual Rows":894,"Actual Loops":1,"Filter":"((aas_ask)::text = 'PRE'::text)"}]}]}]},"Total Runtime":102423.136}"##) }
fn ft_23() -> bool { fixture_matches(r##"'Sort  (cost=148927.96..149012.78 rows=33927 width=49) (actual time=2906.084..2946.819 rows=24935 loops=1)'
'  Sort Key: ak.artikelnr_list'
'  Sort Method:  quicksort  Memory: 3536kB'
'  ->  Nested Loop  (cost=1.55..146374.93 rows=33927 width=49) (actual time=0.160..2531.059 rows=24935 loops=1)'
'        ->  Seq Scan on t_artikelkey ak  (cost=0.00..2312.30 rows=25130 width=237) (actual time=0.014..85.614 rows=25112 loops=1)'
'        ->  Bitmap Heap Scan on artikel_as aas  (cost=1.55..5.63 rows=6 width=10) (actual time=0.073..0.074 rows=1 loops=25112)'
'              Recheck Cond: ((aas.artikelnrforvererbung)::text = ANY ((ARRAY[ak.keysum1, ak.keysum2, ak.keysum3, ak.keysum4, ak.keysum5, ak.keysum6])::text[]))'
'              Filter: ((aas.aas_ask)::text = 'PRE'::text)'
'              ->  Bitmap Index Scan on idx_artikel_as_artnrver  (cost=0.00..1.55 rows=6 width=0) (actual time=0.066..0.066 rows=1 loops=25112)'
'                    Index Cond: ((aas.artikelnrforvererbung)::text = ANY ((ARRAY[ak.keysum1, ak.keysum2, ak.keysum3, ak.keysum4, ak.keysum5, ak.keysum6])::text[]))'


"##, r##"{"Plan":{"Node Type":"Sort","Startup Cost":148927.96,"Total Cost":149012.78,"Plan Rows":33927,"Plan Width":49,"Actual Startup Time":2906.084,"Actual Total Time":2946.819,"Actual Rows":24935,"Actual Loops":1,"Sort Key":["ak.artikelnr_list"],"Sort Method":"quicksort","Sort Space Used":"3536","Sort Space Type":"Memory","Plans":[{"Node Type":"Nested Loop","Startup Cost":1.55,"Total Cost":146374.93,"Plan Rows":33927,"Plan Width":49,"Actual Startup Time":0.16,"Actual Total Time":2531.059,"Actual Rows":24935,"Actual Loops":1,"Plans":[{"Node Type":"Seq Scan","Relation Name":"t_artikelkey","Alias":"ak","Startup Cost":0,"Total Cost":2312.3,"Plan Rows":25130,"Plan Width":237,"Actual Startup Time":0.014,"Actual Total Time":85.614,"Actual Rows":25112,"Actual Loops":1},{"Node Type":"Bitmap Heap Scan","Relation Name":"artikel_as","Alias":"aas","Startup Cost":1.55,"Total Cost":5.63,"Plan Rows":6,"Plan Width":10,"Actual Startup Time":0.073,"Actual Total Time":0.074,"Actual Rows":1,"Actual Loops":25112,"Recheck Cond":"((aas.artikelnrforvererbung)::text = ANY ((ARRAY[ak.keysum1, ak.keysum2, ak.keysum3, ak.keysum4, ak.keysum5, ak.keysum6])::text[]))","Filter":"((aas.aas_ask)::text = 'PRE'::text)","Plans":[{"Node Type":"Bitmap Index Scan","Index Name":"idx_artikel_as_artnrver","Startup Cost":0,"Total Cost":1.55,"Plan Rows":6,"Plan Width":0,"Actual Startup Time":0.066,"Actual Total Time":0.066,"Actual Rows":1,"Actual Loops":25112,"Index Cond":"((aas.artikelnrforvererbung)::text = ANY ((ARRAY[ak.keysum1, ak.keysum2, ak.keysum3, ak.keysum4, ak.keysum5, ak.keysum6])::text[]))"}]}]}]}}"##) }
fn ft_24() -> bool { fixture_matches(r##"Unique  (cost=11787.13..12081.81 rows=11787 width=81)"
  ->  Sort  (cost=11787.13..11816.60 rows=11787 width=81)"
        Sort Key: t.user_id, i.email, t.enabled, t.admin, t.abonnement, t.actu, t.first_sbsc, t.thematique, t.creation_date"
        ->  Hash Left Join  (cost=4303.35..10423.04 rows=11787 width=81)"
              Hash Cond: ((acl.account_id)::text = (s.account_id)::text)"
              Filter: (s.account_id IS NULL)"
              ->  Nested Loop Left Join  (cost=2671.24..6316.24 rows=12330 width=92)"
                    ->  Hash Left Join  (cost=2671.24..3742.10 rows=5042 width=91)"
                          Hash Cond: ((u.id)::text = (t.user_id)::text)"
                          ->  Hash Left Join  (cost=1422.12..2047.04 rows=5042 width=33)"
                                Hash Cond: ((i.email)::text = (u.email)::text)"
                                ->  Seq Scan on inrq_information_request i  (cost=0.00..266.42 rows=5042 width=23)"
                                ->  Hash  (cost=886.61..886.61 rows=26361 width=37)"
                                      ->  Seq Scan on sbsc_user u  (cost=0.00..886.61 rows=26361 width=37)"
                          ->  Hash  (cost=635.61..635.61 rows=26361 width=58)"
                                ->  Seq Scan on talend_users t  (cost=0.00..635.61 rows=26361 width=58)"
                    ->  Index Scan using ix_sbscaccntacl_usrid on sbsc_account_acl acl  (cost=0.00..0.47 rows=3 width=21)"
                          Index Cond: ((u.id)::text = (acl.user_id)::text)"
              ->  Hash  (cost=932.27..932.27 rows=40227 width=11)"
                    ->  Seq Scan on sbsc_subscription s  (cost=0.00..932.27 rows=40227 width=11)"
"##, r##"{"Plan":{"Node Type":"Unique","Startup Cost":11787.13,"Total Cost":12081.81,"Plan Rows":11787,"Plan Width":81,"Plans":[{"Node Type":"Sort","Startup Cost":11787.13,"Total Cost":11816.6,"Plan Rows":11787,"Plan Width":81,"Sort Key":["t.user_id","i.email","t.enabled","t.admin","t.abonnement","t.actu","t.first_sbsc","t.thematique","t.creation_date"],"Plans":[{"Node Type":"Hash Join","Join Type":"Left","Startup Cost":4303.35,"Total Cost":10423.04,"Plan Rows":11787,"Plan Width":81,"Hash Cond":"((acl.account_id)::text = (s.account_id)::text)","Filter":"(s.account_id IS NULL)","Plans":[{"Node Type":"Nested Loop","Join Type":"Left","Startup Cost":2671.24,"Total Cost":6316.24,"Plan Rows":12330,"Plan Width":92,"Plans":[{"Node Type":"Hash Join","Join Type":"Left","Startup Cost":2671.24,"Total Cost":3742.1,"Plan Rows":5042,"Plan Width":91,"Hash Cond":"((u.id)::text = (t.user_id)::text)","Plans":[{"Node Type":"Hash Join","Join Type":"Left","Startup Cost":1422.12,"Total Cost":2047.04,"Plan Rows":5042,"Plan Width":33,"Hash Cond":"((i.email)::text = (u.email)::text)","Plans":[{"Node Type":"Seq Scan","Relation Name":"inrq_information_request","Alias":"i","Startup Cost":0,"Total Cost":266.42,"Plan Rows":5042,"Plan Width":23},{"Node Type":"Hash","Startup Cost":886.61,"Total Cost":886.61,"Plan Rows":26361,"Plan Width":37,"Plans":[{"Node Type":"Seq Scan","Relation Name":"sbsc_user","Alias":"u","Startup Cost":0,"Total Cost":886.61,"Plan Rows":26361,"Plan Width":37}]}]},{"Node Type":"Hash","Startup Cost":635.61,"Total Cost":635.61,"Plan Rows":26361,"Plan Width":58,"Plans":[{"Node Type":"Seq Scan","Relation Name":"talend_users","Alias":"t","Startup Cost":0,"Total Cost":635.61,"Plan Rows":26361,"Plan Width":58}]}]},{"Node Type":"Index Scan","Scan Direction":"Forward","Index Name":"ix_sbscaccntacl_usrid","Relation Name":"sbsc_account_acl","Alias":"acl","Startup Cost":0,"Total Cost":0.47,"Plan Rows":3,"Plan Width":21,"Index Cond":"((u.id)::text = (acl.user_id)::text)"}]},{"Node Type":"Hash","Startup Cost":932.27,"Total Cost":932.27,"Plan Rows":40227,"Plan Width":11,"Plans":[{"Node Type":"Seq Scan","Relation Name":"sbsc_subscription","Alias":"s","Startup Cost":0,"Total Cost":932.27,"Plan Rows":40227,"Plan Width":11}]}]}]}]}}"##) }
fn ft_25() -> bool { fixture_matches(r##"                                                  QUERY PLAN                                                   
---------------------------------------------------------------------------------------------------------------
 Sort  (cost=29.81..30.67 rows=342 width=262) (actual time=0.440..0.537 rows=352 loops=1)
   Sort Key: relallvisible
   Sort Method: quicksort  Memory: 118kB
   ->  Seq Scan on pg_class  (cost=0.00..15.42 rows=342 width=262) (actual time=0.021..0.175 rows=352 loops=1)
 Planning Time: 0.255 ms
 Execution Time: 0.678 ms
(6 rows)
"##, r##"{"Plan":{"Node Type":"Sort","Startup Cost":29.81,"Total Cost":30.67,"Plan Rows":342,"Plan Width":262,"Actual Startup Time":0.44,"Actual Total Time":0.537,"Actual Rows":352,"Actual Loops":1,"Sort Key":["relallvisible"],"Sort Method":"quicksort","Sort Space Used":"118","Sort Space Type":"Memory","Plans":[{"Node Type":"Seq Scan","Relation Name":"pg_class","Startup Cost":0,"Total Cost":15.42,"Plan Rows":342,"Plan Width":262,"Actual Startup Time":0.021,"Actual Total Time":0.175,"Actual Rows":352,"Actual Loops":1}]},"Planning Time":0.255,"Execution Time":0.678}"##) }
fn ft_26() -> bool { fixture_matches(r##"                            QUERY PLAN                            
------------------------------------------------------------------
 Seq Scan on pg_class (actual time=0.016..0.149 rows=342 loops=1)
 Planning Time: 0.188 ms
 Execution Time: 0.238 ms
(3 lignes)
"##, r##"{"Plan":{"Node Type":"Seq Scan","Relation Name":"pg_class","Actual Startup Time":0.016,"Actual Total Time":0.149,"Actual Rows":342,"Actual Loops":1},"Planning Time":0.188,"Execution Time":0.238}"##) }
fn ft_27() -> bool { fixture_matches(r##"explain (analyze, timing off) select * from pg_class ;
                                      QUERY PLAN                                       
---------------------------------------------------------------------------------------
 Seq Scan on pg_class  (cost=0.00..32.98 rows=698 width=776) (actual rows=698 loops=1)
 Planning Time: 0.051 ms
 Execution Time: 0.105 ms
(3 lignes)
"##, r##"{"Plan":{"Node Type":"Seq Scan","Relation Name":"pg_class","Startup Cost":0,"Total Cost":32.98,"Plan Rows":698,"Plan Width":776,"Actual Rows":698,"Actual Loops":1},"Planning Time":0.051,"Execution Time":0.105}"##) }
fn ft_28() -> bool { fixture_matches(r##"# I/O Timings
BEGIN; SET track_io_timing = ON; EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM pg_class; COMMIT;
                                               QUERY PLAN
---------------------------------------------------------------------------------------------------------
 Seq Scan on pg_class  (cost=0.00..16.95 rows=395 width=265) (actual time=0.003..0.044 rows=395 loops=1)
   Buffers: shared hit=10 read=3 written=1
   I/O Timings: read=0.014 write=0.005
 Planning Time: 0.327 ms
 Execution Time: 0.105 ms
(5 lignes)
"##, r##"{"Plan":{"Node Type":"Seq Scan","Relation Name":"pg_class","Startup Cost":0,"Total Cost":16.95,"Plan Rows":395,"Plan Width":265,"Actual Startup Time":0.003,"Actual Total Time":0.044,"Actual Rows":395,"Actual Loops":1,"Shared Hit Blocks":10,"Shared Read Blocks":3,"Shared Written Blocks":1,"Shared Dirtied Blocks":0,"I/O Read Time":0.014,"I/O Write Time":0.005},"Planning Time":0.327,"Execution Time":0.105}"##) }
fn ft_29() -> bool { fixture_matches(r##"# JIT
BEGIN; SET jit_above_cost = 10; EXPLAIN ANALYZE SELECT SUM(relpages) FROM pg_class; COMMIT;

                                                 QUERY PLAN
-------------------------------------------------------------------------------------------------------------
 Aggregate  (cost=17.94..17.95 rows=1 width=8) (actual time=2.250..2.250 rows=1 loops=1)
   ->  Seq Scan on pg_class  (cost=0.00..16.95 rows=395 width=4) (actual time=0.007..0.038 rows=397 loops=1)
 Planning Time: 0.052 ms
 JIT:
   Functions: 3
   Options: Inlining false, Optimization false, Expressions true, Deforming true
   Timing: Generation 0.340 ms, Inlining 0.000 ms, Optimization 0.168 ms, Emission 1.907 ms, Total 2.414 ms
 Execution Time: 2.625 ms
(8 lignes)
"##, r##"{"Plan":{"Node Type":"Aggregate","Startup Cost":17.94,"Total Cost":17.95,"Plan Rows":1,"Plan Width":8,"Actual Startup Time":2.25,"Actual Total Time":2.25,"Actual Rows":1,"Actual Loops":1,"Plans":[{"Node Type":"Seq Scan","Relation Name":"pg_class","Startup Cost":0.0,"Total Cost":16.95,"Plan Rows":395,"Plan Width":4,"Actual Startup Time":0.007,"Actual Total Time":0.038,"Actual Rows":397,"Actual Loops":1}]},"Planning Time":0.052,"JIT":{"Functions":3,"Options":{"Inlining":false,"Optimization":false,"Expressions":true,"Deforming":true},"Timing":{"Generation":0.34,"Inlining":0,"Optimization":0.168,"Emission":1.907,"Total":2.414}},"Execution Time":2.625}"##) }
fn ft_30() -> bool { fixture_matches(r##" Seq Scan on pg_class  (cost=0.00..17.99 rows=499 width=267) (actual time=0.039..0.494 rows=494 loops=1)
 Settings: effective_io_concurrency = '100', enable_partitionwise_aggregate = 'on', enable_partitionwise_join = 'on', from_collapse_limit = '10', join_collapse_limit = '10', max_parallel_workers = '4', random_page_cost = '1', temp_buffers = '32MB', work_mem = '30MB'
 Planning Time: 6.615 ms
 Execution Time: 1.190 ms
"##, r##"{"Plan":{"Node Type":"Seq Scan","Relation Name":"pg_class","Startup Cost":0,"Total Cost":17.99,"Plan Rows":499,"Plan Width":267,"Actual Startup Time":0.039,"Actual Total Time":0.494,"Actual Rows":494,"Actual Loops":1},"Settings":{"effective_io_concurrency":"100","enable_partitionwise_aggregate":"on","enable_partitionwise_join":"on","from_collapse_limit":"10","join_collapse_limit":"10","max_parallel_workers":"4","random_page_cost":"1","temp_buffers":"32MB","work_mem":"30MB"},"Planning Time":6.615,"Execution Time":1.19}"##) }
fn ft_31() -> bool { fixture_matches(r##"                                         explain_analyze_without_memory                                         
----------------------------------------------------------------------------------------------------------------
 Limit (actual rows=70 loops=1)
   ->  Incremental Sort (actual rows=70 loops=1)
         Sort Key: t.a, t.b
         Presorted Key: t.a
         Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 123kB  Peak Memory: 234kB
         Pre-sorted Groups: 5  Sort Methods: top-N heapsort, quicksort  Average Memory: 456kB  Peak Memory: 567kB
         ->  Sort (actual rows=1000 loops=1)
               Sort Key: t.a
               Sort Method: quicksort  Memory: 678kB
               ->  Seq Scan on t (actual rows=1000 loops=1)
(10 rows)
"##, r##"{"Plan":{"Node Type":"Limit","Plans":[{"Node Type":"Incremental Sort","Sort Key":["t.a","t.b"],"Presorted Key":["t.a"],"Full-sort Groups":{"Group Count":1,"Sort Methods Used":["quicksort"],"Sort Space Memory":{"Average Sort Space Used":123,"Peak Sort Space Used":234}},"Pre-sorted Groups":{"Group Count":5,"Sort Methods Used":["top-N heapsort","quicksort"],"Sort Space Memory":{"Average Sort Space Used":456,"Peak Sort Space Used":567}},"Plans":[{"Node Type":"Sort","Sort Key":["t.a"],"Sort Method":"quicksort","Sort Space Used":"678","Sort Space Type":"Memory","Plans":[{"Node Type":"Seq Scan","Relation Name":"t"}]}]}]}}"##) }
fn ft_32() -> bool { fixture_matches(r##"                                                 QUERY PLAN                                                 
------------------------------------------------------------------------------------------------------------
 Tid Range Scan on test  (cost=0.00..142.05 rows=9605 width=4) (actual time=0.019..2.116 rows=9600 loops=1)
   TID Cond: (ctid > '(400,0)'::tid)
   Buffers: shared hit=43
 Planning Time: 0.079 ms
 Execution Time: 3.372 ms
(5 rows)
"##, r##"{"Plan":{"Node Type":"Tid Range Scan","Relation Name":"test","Startup Cost":0,"Total Cost":142.05,"Plan Rows":9605,"Plan Width":4,"Actual Startup Time":0.019,"Actual Total Time":2.116,"Actual Rows":9600,"Actual Loops":1,"TID Cond":"(ctid > '(400,0)'::tid)","Shared Hit Blocks":43,"Shared Read Blocks":0,"Shared Written Blocks":0,"Shared Dirtied Blocks":0},"Planning Time":0.079,"Execution Time":3.372}"##) }
fn ft_33() -> bool { fixture_matches(r##"                                                   QUERY PLAN
----------------------------------------------------------------------------------------------------------------
 Aggregate (actual time=55.764..55.768 rows=1 loops=1)
   ->  Append (actual time=37.084..54.758 rows=18000 loops=1)
         ->  Async Foreign Scan on population_frara population_1 (actual time=0.724..1.722 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frbfc population_2 (actual time=0.799..1.617 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frbre population_3 (actual time=0.701..1.532 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frcor population_4 (actual time=0.622..1.423 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frcvl population_5 (actual time=0.610..1.429 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frges population_6 (actual time=0.740..1.542 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frgua population_7 (actual time=0.653..1.453 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frguf population_8 (actual time=13.221..14.045 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frhdf population_9 (actual time=3.847..4.675 rows=1000 loops=1)
         ->  Async Foreign Scan on population_fridf population_10 (actual time=0.667..1.466 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frlre population_11 (actual time=0.650..1.442 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frmay population_12 (actual time=1.321..2.119 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frmtq population_13 (actual time=0.911..2.158 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frnaq population_14 (actual time=0.695..1.488 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frnor population_15 (actual time=0.761..1.645 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frocc population_16 (actual time=9.212..10.008 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frpac population_17 (actual time=0.440..1.170 rows=1000 loops=1)
         ->  Async Foreign Scan on population_frpdl population_18 (actual time=1.074..1.807 rows=1000 loops=1)
 Planning Time: 2.369 ms
 Execution Time: 62.467 ms
"##, r##"{"Plan":{"Node Type":"Aggregate","Actual Startup Time":55.764,"Actual Total Time":55.768,"Actual Rows":1,"Actual Loops":1,"Plans":[{"Node Type":"Append","Actual Startup Time":37.084,"Actual Total Time":54.758,"Actual Rows":18000,"Actual Loops":1,"Plans":[{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frara","Alias":"population_1","Actual Startup Time":0.724,"Actual Total Time":1.722,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frbfc","Alias":"population_2","Actual Startup Time":0.799,"Actual Total Time":1.617,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frbre","Alias":"population_3","Actual Startup Time":0.701,"Actual Total Time":1.532,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frcor","Alias":"population_4","Actual Startup Time":0.622,"Actual Total Time":1.423,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frcvl","Alias":"population_5","Actual Startup Time":0.61,"Actual Total Time":1.429,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frges","Alias":"population_6","Actual Startup Time":0.74,"Actual Total Time":1.542,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frgua","Alias":"population_7","Actual Startup Time":0.653,"Actual Total Time":1.453,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frguf","Alias":"population_8","Actual Startup Time":13.221,"Actual Total Time":14.045,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frhdf","Alias":"population_9","Actual Startup Time":3.847,"Actual Total Time":4.675,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_fridf","Alias":"population_10","Actual Startup Time":0.667,"Actual Total Time":1.466,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frlre","Alias":"population_11","Actual Startup Time":0.65,"Actual Total Time":1.442,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frmay","Alias":"population_12","Actual Startup Time":1.321,"Actual Total Time":2.119,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frmtq","Alias":"population_13","Actual Startup Time":0.911,"Actual Total Time":2.158,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frnaq","Alias":"population_14","Actual Startup Time":0.695,"Actual Total Time":1.488,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frnor","Alias":"population_15","Actual Startup Time":0.761,"Actual Total Time":1.645,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frocc","Alias":"population_16","Actual Startup Time":9.212,"Actual Total Time":10.008,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frpac","Alias":"population_17","Actual Startup Time":0.44,"Actual Total Time":1.17,"Actual Rows":1000,"Actual Loops":1},{"Node Type":"Foreign Scan","Async Capable":true,"Relation Name":"population_frpdl","Alias":"population_18","Actual Startup Time":1.074,"Actual Total Time":1.807,"Actual Rows":1000,"Actual Loops":1}]}]},"Planning Time":2.369,"Execution Time":62.467}"##) }
fn ft_34() -> bool { fixture_matches(r##"QUERY PLAN                          
-------------------------------------------------------------
 Result  (cost=0.00..0.01 rows=1 width=4)
 Settings: search_path = 'public, magasin', work_mem = '8MB'
"##, r##"{"Plan":{"Node Type":"Result","Startup Cost":0,"Total Cost":0.01,"Plan Rows":1,"Plan Width":4},"Settings":{"search_path":"public, magasin","work_mem":"8MB"}}"##) }
fn ft_35() -> bool { fixture_matches(r##"[14003](2038152)postgres@pgbench_100=# set work_mem to '1MB';
SET
Temps : 0,305 ms
[14003](2038152)postgres@pgbench_100=# CREATE TEMP table pp AS SELECT * FROM pgbench_accounts LIMIT 1000000;
SELECT 1000000
Temps : 542,276 ms
[14003](2038152)postgres@pgbench_100=# EXPLAIN (ANALYZE,VERBOSE,BUFFERS) SELECT * FROM pp UNION ALL SELECT * FROM pgbench_accounts order by filler ;
                                                                    QUERY PLAN                                                                     
═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
 Sort  (cost=3300922.10..3326782.40 rows=10344118 width=105) (actual time=19388.365..21360.337 rows=11000000 loops=1)
   Output: pp.aid, pp.bid, pp.abalance, pp.filler
   Sort Key: pp.filler
   Sort Method: external merge  Disk: 1151688kB
   Buffers: shared hit=35 read=163903, local read=16394 dirtied=16394 written=16394, temp read=778130 written=780004
   I/O Timings: read=1116.067
   ->  Append  (cost=0.00..335490.77 rows=10344118 width=105) (actual time=0.035..3226.246 rows=11000000 loops=1)
         Buffers: shared hit=32 read=163903, local read=16394 dirtied=16394 written=16394
         I/O Timings: read=1116.067
         ->  Seq Scan on pg_temp_5.pp  (cost=0.00..19836.74 rows=344274 width=352) (actual time=0.033..210.609 rows=1000000 loops=1)
               Output: pp.aid, pp.bid, pp.abalance, pp.filler
               Buffers: local read=16394 dirtied=16394 written=16394
               I/O Timings: read=39.730
         ->  Seq Scan on public.pgbench_accounts  (cost=0.00..263933.44 rows=9999844 width=97) (actual time=0.007..2169.508 rows=10000000 loops=1)
               Output: pgbench_accounts.aid, pgbench_accounts.bid, pgbench_accounts.abalance, pgbench_accounts.filler
               Buffers: shared hit=32 read=163903
               I/O Timings: read=1076.337
 Query Identifier: -5424821330993577306
 Planning:
   Buffers: shared hit=32 read=8
   I/O Timings: read=0.073
 Planning Time: 0.292 ms
 Execution Time: 22103.138 ms
(23 lignes)
"##, r##"{"Plan":{"Actual Loops":1,"Actual Rows":11000000,"Actual Startup Time":19388.365,"Actual Total Time":21360.337,"Node Type":"Sort","Plans":[{"Actual Loops":1,"Actual Rows":11000000,"Actual Startup Time":0.035,"Actual Total Time":3226.246,"Node Type":"Append","Plans":[{"Actual Loops":1,"Actual Rows":1000000,"Actual Startup Time":0.033,"Actual Total Time":210.609,"Node Type":"Seq Scan","Plan Rows":344274,"Total Cost":19836.74,"Relation Name":"pg_temp_5.pp","Startup Cost":0,"Plan Width":352,"Output":"pp.aid, pp.bid, pp.abalance, pp.filler","Local Hit Blocks":0,"Local Read Blocks":16394,"Local Written Blocks":16394,"Local Dirtied Blocks":16394,"I/O Read Time":39.73,"I/O Write Time":0},{"Actual Loops":1,"Actual Rows":10000000,"Actual Startup Time":0.007,"Actual Total Time":2169.508,"Node Type":"Seq Scan","Plan Rows":9999844,"Total Cost":263933.44,"Relation Name":"public.pgbench_accounts","Startup Cost":0,"Plan Width":97,"Output":"pgbench_accounts.aid, pgbench_accounts.bid, pgbench_accounts.abalance, pgbench_accounts.filler","Shared Hit Blocks":32,"Shared Read Blocks":163903,"Shared Written Blocks":0,"Shared Dirtied Blocks":0,"I/O Read Time":1076.337,"I/O Write Time":0}],"Plan Rows":10344118,"Total Cost":335490.77,"Startup Cost":0,"Plan Width":105,"Shared Hit Blocks":32,"Shared Read Blocks":163903,"Shared Written Blocks":0,"Shared Dirtied Blocks":0,"Local Hit Blocks":0,"Local Read Blocks":16394,"Local Written Blocks":16394,"Local Dirtied Blocks":16394,"I/O Read Time":1116.067,"I/O Write Time":0}],"Plan Rows":10344118,"Total Cost":3326782.4,"Startup Cost":3300922.1,"Plan Width":105,"Output":"pp.aid, pp.bid, pp.abalance, pp.filler","Sort Key":["pp.filler"],"Sort Method":"external merge","Sort Space Used":"1151688","Sort Space Type":"Disk","Shared Hit Blocks":35,"Shared Read Blocks":163903,"Shared Written Blocks":0,"Shared Dirtied Blocks":0,"Local Hit Blocks":0,"Local Read Blocks":16394,"Local Written Blocks":16394,"Local Dirtied Blocks":16394,"Temp Hit Blocks":0,"Temp Read Blocks":778130,"Temp Written Blocks":780004,"Temp Dirtied Blocks":0,"I/O Read Time":1116.067,"I/O Write Time":0},"Query Identifier":-5424821330993577000,"Planning":{"I/O Read Time":0.073,"I/O Write Time":0,"Shared Dirtied Blocks":0,"Shared Hit Blocks":32,"Shared Read Blocks":8,"Shared Written Blocks":0},"Planning Time":0.292,"Execution Time":22103.138}"##) }
fn ft_36() -> bool { fixture_matches(r##"Not a real plan. Showing how line breaks (with empty space at the begining) in an Output can break parsing.
Also parsing shouldn't break if Output is followed by new node at a lower level.
                                                                    QUERY PLAN                                                                     
═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
 Nested Loop Left Join  (cost=0.00..0.00 rows=1 width=1)
   ->  Bitmap Heap Scan on a  (cost=0.00..0.00 rows=1 width=1)
         Output: blah,
 dude 
         ->  Bitmap Index Scan on rel_users_exams_pkey  (cost=0.00..0.00 rows=1 width=0)
   ->  Sort  (cost=0.00..0.00 rows=1 width=1)
         ->  Seq Scan on a  (cost=0.00..0.00 rows=1 width=1)
               Output: a1
   ->  Sort  (cost=0.00..0.00 rows=1 width=1)
"##, r##"{"Plan":{"Node Type":"Nested Loop","Join Type":"Left","Plans":[{"Node Type":"Bitmap Heap Scan","Plans":[{"Node Type":"Bitmap Index Scan"}]},{"Node Type":"Sort","Plans":[{"Node Type":"Seq Scan"}]},{"Node Type":"Sort"}]}}"##) }
fn ft_37() -> bool { fixture_matches(r##"Nested Loop Left Join  (cost=11.95..28.52 rows=5 width=157)
   ->  Bitmap Heap Scan on public.rel_users_exams  (cost=11.80..20.27 rows=5 width=52)
         Output: rel_users_exams.user_username, rel_users_exams.exam_id
         ->  Bitmap Index Scan on rel_users_exams_pkey  (cost=0.00..11.80 rows=5 width=0)
 Execution Time: 9762.684 ms
"##, r##"{"Plan":{"Node Type":"Nested Loop","Join Type":"Left","Plans":[{"Node Type":"Bitmap Heap Scan","Plans":[{"Node Type":"Bitmap Index Scan"}]}]},"Execution Time":9762.684}"##) }
fn ft_38() -> bool { fixture_matches(r##"Merge on public.tb  (cost=1.00..1.05 rows=0 width=0)
"##, r##"{"Plan":{"Node Type":"Merge"}}"##) }
fn ft_39() -> bool { fixture_matches(r##"Sort  (cost=585066.84..585066.85 rows=1 width=473) (actual time=156773.141..156784.582 rows=498 loops=1)
  Output: (replace((au.login)::text, ((eeof.nom_enseigne)::text || '_'::text), ''::text)), ((ph.attr -> '96'::text)), ((ph.attr -> '253'::text)), eau.civilite, au.nom, au.prenom, eau.mail_representant, eau.telephone, ((ph.attr -> '382'::text)), ((ph.attr -> '11'::text)), eau.date_inscription, eau.nombre_connexion, eau.date_derniere_connexion, (COALESCE((sum(euuo.points)), '0'::numeric)), (COALESCE((sum(euuo_1.points)), '0'::numeric)), (COALESCE((sum(eodc.points)), '0'::numeric)), (COALESCE((sum(vpruo.points_restants_ope)), '0'::bigint)), (COALESCE((sum(vpruo_1.points_restants_ope)), '0'::bigint)), (CASE WHEN ((count(af.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af.fk_utilisateur)) END), (CASE WHEN ((count(af_1.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_1.fk_utilisateur)) END), (CASE WHEN ((count(af_2.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_2.fk_utilisateur)) END), (CASE WHEN ((count(af_3.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_3.fk_utilisateur)) END), (CASE WHEN ((count(af_4.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_4.fk_utilisateur)) END), (CASE WHEN ((count(af_5.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_5.fk_utilisateur)) END), (CASE WHEN ((count(af_6.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_6.fk_utilisateur)) END), (CASE WHEN ((count(af_7.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_7.fk_utilisateur)) END), (CASE WHEN ((count(af_8.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_8.fk_utilisateur)) END), (CASE WHEN ((count(af_9.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_9.fk_utilisateur)) END), (CASE WHEN ((count(af_10.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_10.fk_utilisateur)) END), (CASE WHEN ((count(af_11.fk_utilisateur)) IS NULL) THEN '0'::bigint ELSE (count(af_11.fk_utilisateur)) END), (string_agg(DISTINCT (er.role_enseigne)::text, ', '::text)), au.login
  Sort Key: au.login
  Sort Method: quicksort  Memory: 281kB
  Buffers: shared hit=27082613 read=2278
Planning:
  Buffers: shared hit=2055 read=10
Planning Time: 38.350 ms
Execution Time: 156803.034 ms
"##, r##"{"Plan":{"Node Type":"Sort","Shared Hit Blocks":27082613,"Shared Read Blocks":2278}}"##) }
fn ft_40() -> bool { fixture_matches(r##"Limit  (cost=0.27..0.48 rows=1 width=265) (actual time=0.010..0.010 rows=1 loops=1)
  ->  Index Scan Backward using pg_class_relname_nsp_index on pg_class  (cost=0.27..81.94 rows=395 width=265) (actual time=0.009..0.009 rows=1 loops=1)
Planning Time: 0.475 ms
Execution Time: 0.088 ms

"##, r##"{"Plan":{"Node Type":"Limit","Plans":[{"Node Type":"Index Scan","Scan Direction":"Backward","Index Name":"pg_class_relname_nsp_index","Relation Name":"pg_class"}]}}"##) }
fn ft_41() -> bool { fixture_matches(r##"Aggregate  (cost=87342.28..87342.29 rows=1 width=64) (actual time=683.349..683.350 rows=1 loops=1)
  Output: sum((COALESCE(((((col1).a + (col1).b) + (col1).c) + (col1).d), '0'::numeric))), sum((COALESCE((col2).y, '0'::numeric)))
  Buffers: shared hit=1673369
  ->  Index Only Scan using "mytable:2020-12-09_coalesce_coalesce1_col1_col2_idx" on public."mytable:2020-12-09"  (cost=0.43..65531.43 rows=1744867 width=115) (actual time=0.012..400.547 rows=1749995 loops=1)
        Output: (COALESCE(((((col1).a + (col1).b) + (col1).c) + (col1).d), '0'::numeric)), (COALESCE((col2).y, '0'::numeric)), col1, col2
        Heap Fetches: 0
        Buffers: shared hit=1673369
Settings: constraint_exclusion = 'on', effective_cache_size = '30GB', enable_partitionwise_aggregate = 'on', enable_partitionwise_join = 'on', geqo_effort = '10', jit = 'off', max_parallel_workers = '1', max_parallel_workers_per_gather = '0', random_page_cost = '1.1', temp_buffers = '80MB', work_mem = '32MB'
Planning Time: 0.103 ms
Execution Time: 683.367 ms
"##, r##"{"Plan":{"Node Type":"Aggregate","Plans":[{"Node Type":"Index Only Scan","Index Name":"\"mytable:2020-12-09_coalesce_coalesce1_col1_col2_idx\"","Relation Name":"public.\"mytable:2020-12-09\""}]},"Settings":{"constraint_exclusion":"on","effective_cache_size":"30GB","enable_partitionwise_aggregate":"on","enable_partitionwise_join":"on","geqo_effort":"10","jit":"off","max_parallel_workers":"1","max_parallel_workers_per_gather":"0","random_page_cost":"1.1","temp_buffers":"80MB","work_mem":"32MB"}}"##) }
fn ft_42() -> bool { fixture_matches(r##"Seq Scan on t1 (cost=0.00..21370.00 rows=333333 width=22)
   Disabled: true
   Filter: ((c1 + 0) < 1000000)
 Settings: enable_seqscan = 'off'
"##, r##"{"Plan":{"Node Type":"Seq Scan","Parallel Aware":false,"Relation Name":"t1","Startup Cost":0.0,"Total Cost":21370.0,"Plan Rows":333333,"Plan Width":22,"Disabled":true,"Filter":"((c1 + 0) < 1000000)"},"Settings":{"enable_seqscan":"off"}}"##) }
fn ft_43() -> bool { fixture_matches(r##"                                                           QUERY PLAN                                                            
---------------------------------------------------------------------------------------------------------------------------------
 Hash Join  (cost=471037.90..1304640.12 rows=14999862 width=114) (actual time=1480.644..9895.695 rows=15001138 loops=1)
   Hash Cond: (details.eid = entetes.eid)
   Buffers: shared hit=15940 read=282899, temp read=246065 written=246065
   I/O Timings: shared read=242.957, temp read=317.342 write=830.025
   ->  Seq Scan on details  (cost=0.00..335215.62 rows=15001362 width=65) (actual time=0.014..1114.539 rows=15001289 loops=1)
         Filter: (b IS NOT NULL)
         Buffers: shared hit=1031 read=184171
         I/O Timings: shared read=157.615
   ->  Hash  (cost=238637.70..238637.70 rows=9999056 width=57) (actual time=1474.384..1474.385 rows=9999900 loops=1)
         Buckets: 1048576  Batches: 16  Memory Usage: 62533kB
         Buffers: shared hit=14909 read=98728, temp written=88109
         I/O Timings: shared read=85.342, temp write=294.483
         ->  Seq Scan on entetes  (cost=0.00..238637.70 rows=9999056 width=57) (actual time=0.016..567.370 rows=9999900 loops=1)
               Filter: (a > 100)
               Rows Removed by Filter: 101
               Buffers: shared hit=14909 read=98728
               I/O Timings: shared read=85.342
 Planning:
   Buffers: shared hit=8
 Planning Time: 0.119 ms
 Execution Time: 10244.597 ms
"##, r##"{"Plan":{"Node Type":"Hash Join","Plans":[{"Node Type":"Seq Scan"},{"Node Type":"Hash","Shared Hit Blocks":14909,"Shared Read Blocks":98728,"Temp Written Blocks":88109,"Hash Buckets":1048576,"Original Hash Buckets":1048576,"Hash Batches":16,"Original Hash Batches":16,"Peak Memory Usage":62533,"Plans":[{"Node Type":"Seq Scan"}]}]}}"##) }
fn ft_44() -> bool { fixture_matches(r##"-- source: https://www.postgresql.org/message-id/flat/CA%2BhUKGKWWmf%3DWELLG%3DaUGbcugRaSQbtm0tKYiBut-B2rVKX63g%40mail.gmail.com

SET max_parallel_workers_per_gather = 0;
SET synchronize_seqscans = off;
SET work_mem = '4MB';



CREATE TABLE r AS SELECT generate_series(1, 10000000)::int i;
ANALYZE r;



-- 1k uniform keys + 1m duplicates
CREATE TABLE s1k (i int);
INSERT INTO s1k SELECT generate_series(1, 1000)::int i;
ALTER TABLE s1k SET (autovacuum_enabled = off);
ANALYZE s1k;
INSERT INTO s1k SELECT 42 FROM generate_series(1, 1000000);



EXPLAIN ANALYZE SELECT COUNT(*) FROM r JOIN s1k USING (i);
SET
SET
SET
SELECT 10000000
ANALYZE
CREATE TABLE
INSERT 0 1000
ALTER TABLE
ANALYZE
INSERT 0 1000000
                                                            QUERY PLAN                                                            
----------------------------------------------------------------------------------------------------------------------------------
 Aggregate  (cost=302291.30..302291.31 rows=1 width=8) (actual time=3656.686..3656.688 rows=1.00 loops=1)
   Buffers: shared hit=6734 read=41984 dirtied=13621, temp read=49855 written=49855
   ->  Hash Join  (cost=27826.00..300076.30 rows=886000 width=0) (actual time=146.635..3619.685 rows=1001000.00 loops=1)
         Hash Cond: (r.i = s1k.i)
         Buffers: shared hit=6734 read=41984 dirtied=13621, temp read=49855 written=49855
         ->  Seq Scan on r  (cost=0.00..144294.76 rows=10000676 width=4) (actual time=0.201..1200.806 rows=10000000.00 loops=1)
               Buffers: shared hit=2304 read=41984 dirtied=13621
         ->  Hash  (cost=13290.00..13290.00 rows=886000 width=4) (actual time=145.449..145.450 rows=1001000.00 loops=1)
               Buckets: 262144 (originally 262144)  Batches: 2048 (originally 8)  Memory Usage: 35157kB
               Buffers: shared hit=4430, temp written=2930
               ->  Seq Scan on s1k  (cost=0.00..13290.00 rows=886000 width=4) (actual time=0.011..61.783 rows=1001000.00 loops=1)
                     Buffers: shared hit=4430
 Planning:
   Buffers: shared hit=83 read=9
 Planning Time: 0.495 ms
 JIT:
   Functions: 10
   Options: Inlining false, Optimization false, Expressions true, Deforming true
   Timing: Generation 0.492 ms (Deform 0.134 ms), Inlining 0.000 ms, Optimization 0.266 ms, Emission 3.684 ms, Total 4.442 ms
 Execution Time: 3674.128 ms
(20 rows)
"##, r##"{"Plan":{"Node Type":"Aggregate","Plans":[{"Node Type":"Hash Join","Plans":[{"Node Type":"Seq Scan"},{"Node Type":"Hash","Actual Rows":1001000,"Hash Buckets":262144,"Original Hash Buckets":262144,"Hash Batches":2048,"Original Hash Batches":8,"Peak Memory Usage":35157}]}]}}"##) }
fn ft_45() -> bool { fixture_matches(r##"-- taken from https://explain.dalibo.com/plan/bcc3fd5fc7bc771b

 HashAggregate  (cost=79468.50..98636.70 rows=940257 width=11) (actual time=231.946..413.626 rows=970675 loops=1)
   Group Key: name
   Planned Partitions: 32  Batches: 193  Memory Usage: 4153kB  Disk Usage: 31752kB
   ->  Seq Scan on t1  (cost=0.00..15406.00 rows=1000000 width=11) (actual time=0.118..36.474 rows=1000000 loops=1)
 Planning Time: 0.201 ms
 Execution Time: 438.186 ms
(6 rows)

Time: 438.861 ms
"##, r##"{"Plan":{"Node Type":"Aggregate","Strategy":"Hashed","Partial Mode":"Simple","Planned Partitions":32,"HashAgg Batches":193,"Peak Memory Usage":4153,"Disk Usage":31752,"Plans":[{"Node Type":"Seq Scan"}]}}"##) }
fn ft_46() -> bool { fixture_matches(r##"Nested Loop  (cost=0.43..64.45 rows=1000 width=40) (actual time=0.014..0.322 rows=1000 loops=1)
  Buffers: shared hit=15
  ->  Seq Scan on sessions s  (cost=0.00..17.00 rows=1000 width=24) (actual time=0.005..0.044 rows=1000 loops=1)
        Buffers: shared hit=7
  ->  Memoize  (cost=0.43..7.50 rows=1 width=16) (actual time=0.000..0.000 rows=1 loops=1000)
        Cache Key: s.user_id
        Cache Mode: logical
        Hits: 998  Misses: 2  Evictions: 0  Overflows: 0  Memory Usage: 1kB
        Buffers: shared hit=8
        ->  Index Scan using users_pkey on users u  (cost=0.42..7.49 rows=1 width=16) (actual time=0.003..0.003 rows=1 loops=2)
              Index Cond: (id = s.user_id)
              Buffers: shared hit=8
Planning:
  Buffers: shared hit=8
Planning Time: 0.094 ms
Execution Time: 0.375 ms

"##, r##"{"Plan":{"Node Type":"Nested Loop","Plans":[{"Node Type":"Seq Scan"},{"Node Type":"Memoize","Cache Key":"s.user_id","Cache Mode":"logical","Cache Hits":998,"Cache Misses":2,"Cache Evictions":0,"Cache Overflows":0,"Peak Memory Usage":1}]}}"##) }
fn ft_47() -> bool { fixture_matches(r##"HashAggregate  (cost=5471.00..5721.00 rows=20000 width=53)
  Group Key: customer_id, status
  ->  Seq Scan on orders  (cost=0.00..3471.00 rows=200000 width=19)
"##, r##"{"Plan":{"Node Type":"Aggregate","Strategy":"Hashed","Partial Mode":"Simple","Parallel Aware":false,"Async Capable":false,"Startup Cost":5471.0,"Total Cost":5721.0,"Plan Rows":20000,"Plan Width":53,"Disabled":false,"Group Key":["customer_id","status"],"Planned Partitions":0,"Plans":[{"Node Type":"Seq Scan","Parallel Aware":false,"Async Capable":false,"Relation Name":"orders","Startup Cost":0.0,"Total Cost":3471.0,"Plan Rows":200000,"Plan Width":19,"Disabled":false}]}}"##) }
fn ft_48() -> bool { fixture_matches(r##"GroupAggregate  (cost=144.66..170.76 rows=488 width=44)
  Group Key: customer_id
  ->  Sort  (cost=144.66..149.66 rows=2000 width=10)
        Sort Key: customer_id
        ->  Seq Scan on orders  (cost=0.00..35.00 rows=2000 width=10)
"##, r##"{"Plan":{"Node Type":"Aggregate","Strategy":"Sorted","Partial Mode":"Simple","Parallel Aware":false,"Async Capable":false,"Startup Cost":144.66,"Total Cost":170.76,"Plan Rows":488,"Plan Width":44,"Disabled":false,"Group Key":["customer_id"],"Plans":[{"Node Type":"Sort","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Startup Cost":144.66,"Total Cost":149.66,"Plan Rows":2000,"Plan Width":10,"Disabled":false,"Sort Key":["customer_id"],"Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"orders","Alias":"orders","Startup Cost":0.0,"Total Cost":35.0,"Plan Rows":2000,"Plan Width":10,"Disabled":false}]}]}}"##) }
fn ft_49() -> bool { fixture_matches(r##"GroupAggregate  (cost=144.66..311.59 rows=982 width=52)
  Group Key: customer_id, status
  Group Key: customer_id
  Group Key: ()
  Sort Key: status
    Group Key: status
  ->  Sort  (cost=144.66..149.66 rows=2000 width=18)
        Sort Key: customer_id, status
        ->  Seq Scan on orders  (cost=0.00..35.00 rows=2000 width=18)
"##, r##"{"Plan":{"Node Type":"Aggregate","Strategy":"Sorted","Partial Mode":"Simple","Parallel Aware":false,"Async Capable":false,"Startup Cost":144.66,"Total Cost":311.59,"Plan Rows":982,"Plan Width":52,"Disabled":false,"Grouping Sets":[{"Group Keys":[["customer_id","status"],["customer_id"],[]]},{"Sort Key":["status"],"Group Keys":[["status"]]}],"Plans":[{"Node Type":"Sort","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Startup Cost":144.66,"Total Cost":149.66,"Plan Rows":2000,"Plan Width":18,"Disabled":false,"Sort Key":["customer_id","status"],"Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"orders","Alias":"orders","Startup Cost":0.0,"Total Cost":35.0,"Plan Rows":2000,"Plan Width":18,"Disabled":false}]}]}}"##) }
fn ft_50() -> bool { fixture_matches(r##"HashAggregate  (cost=55.00..61.10 rows=488 width=52)
  Group Key: customer_id, status
  ->  Seq Scan on orders  (cost=0.00..35.00 rows=2000 width=18)
"##, r##"{"Plan":{"Node Type":"Aggregate","Strategy":"Hashed","Partial Mode":"Simple","Parallel Aware":false,"Async Capable":false,"Startup Cost":55.0,"Total Cost":61.1,"Plan Rows":488,"Plan Width":52,"Disabled":false,"Group Key":["customer_id","status"],"Planned Partitions":0,"Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"orders","Alias":"orders","Startup Cost":0.0,"Total Cost":35.0,"Plan Rows":2000,"Plan Width":18,"Disabled":false}]}}"##) }
fn ft_51() -> bool { fixture_matches(r##"MixedAggregate  (cost=0.00..107.28 rows=982 width=52)
  Hash Key: customer_id, status
  Hash Key: customer_id
  Hash Key: status
  Group Key: ()
  ->  Seq Scan on orders  (cost=0.00..35.00 rows=2000 width=18)
"##, r##"{"Plan":{"Node Type":"Aggregate","Strategy":"Mixed","Partial Mode":"Simple","Parallel Aware":false,"Async Capable":false,"Startup Cost":0.0,"Total Cost":107.28,"Plan Rows":982,"Plan Width":52,"Disabled":false,"Grouping Sets":[{"Hash Keys":[["customer_id","status"]]},{"Hash Keys":[["customer_id"]]},{"Hash Keys":[["status"]]},{"Group Keys":[[]]}],"Planned Partitions":0,"Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"orders","Alias":"orders","Startup Cost":0.0,"Total Cost":35.0,"Plan Rows":2000,"Plan Width":18,"Disabled":false}]}}"##) }
fn ft_52() -> bool { fixture_matches(r##"Hash Right Anti Join  (cost=2887.00..16639.53 rows=14840 width=18)
  Hash Cond: (o.customer_id = c.id)
  ->  Seq Scan on orders o  (cost=0.00..7703.00 rows=500000 width=4)
  ->  Hash  (cost=1637.00..1637.00 rows=100000 width=18)
        ->  Seq Scan on customers c  (cost=0.00..1637.00 rows=100000 width=18)
"##, r##"{"Plan":{"Node Type":"Hash Join","Parallel Aware":false,"Async Capable":false,"Join Type":"Right Anti","Startup Cost":2887.0,"Total Cost":16639.53,"Plan Rows":14840,"Plan Width":18,"Disabled":false,"Hash Cond":"(o.customer_id = c.id)","Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"orders","Alias":"o","Startup Cost":0.0,"Total Cost":7703.0,"Plan Rows":500000,"Plan Width":4,"Disabled":false},{"Node Type":"Hash","Parent Relationship":"Inner","Parallel Aware":false,"Async Capable":false,"Startup Cost":1637.0,"Total Cost":1637.0,"Plan Rows":100000,"Plan Width":18,"Disabled":false,"Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"customers","Alias":"c","Startup Cost":0.0,"Total Cost":1637.0,"Plan Rows":100000,"Plan Width":18,"Disabled":false}]}]}}"##) }
fn ft_53() -> bool { fixture_matches(r##"Hash Right Semi Join  (cost=2887.00..16639.53 rows=85160 width=18)
  Hash Cond: (o.customer_id = c.id)
  ->  Seq Scan on orders o  (cost=0.00..7703.00 rows=500000 width=4)
  ->  Hash  (cost=1637.00..1637.00 rows=100000 width=18)
        ->  Seq Scan on customers c  (cost=0.00..1637.00 rows=100000 width=18)
"##, r##"{"Plan":{"Node Type":"Hash Join","Parallel Aware":false,"Async Capable":false,"Join Type":"Right Semi","Startup Cost":2887.0,"Total Cost":16639.53,"Plan Rows":85160,"Plan Width":18,"Disabled":false,"Hash Cond":"(o.customer_id = c.id)","Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"orders","Alias":"o","Startup Cost":0.0,"Total Cost":7703.0,"Plan Rows":500000,"Plan Width":4,"Disabled":false},{"Node Type":"Hash","Parent Relationship":"Inner","Parallel Aware":false,"Async Capable":false,"Startup Cost":1637.0,"Total Cost":1637.0,"Plan Rows":100000,"Plan Width":18,"Disabled":false,"Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"customers","Alias":"c","Startup Cost":0.0,"Total Cost":1637.0,"Plan Rows":100000,"Plan Width":18,"Disabled":false}]}]}}"##) }
fn ft_54() -> bool { fixture_matches(r##"Hash Join  (cost=29.50..124.68 rows=5000 width=24)
  Hash Cond: (o.customer_id = c.id)
  ->  Seq Scan on orders o  (cost=0.00..82.00 rows=5000 width=16)
  ->  Hash  (cost=17.00..17.00 rows=1000 width=16)
        ->  Seq Scan on customers c  (cost=0.00..17.00 rows=1000 width=16)
"##, r##"{"Plan":{"Node Type":"Hash Join","Parallel Aware":false,"Async Capable":false,"Join Type":"Inner","Startup Cost":29.5,"Total Cost":124.68,"Plan Rows":5000,"Plan Width":24,"Disabled":false,"Hash Cond":"(o.customer_id = c.id)","Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"orders","Alias":"o","Startup Cost":0.0,"Total Cost":82.0,"Plan Rows":5000,"Plan Width":16,"Disabled":false},{"Node Type":"Hash","Parent Relationship":"Inner","Parallel Aware":false,"Async Capable":false,"Startup Cost":17.0,"Total Cost":17.0,"Plan Rows":1000,"Plan Width":16,"Disabled":false,"Plans":[{"Node Type":"Seq Scan","Parent Relationship":"Outer","Parallel Aware":false,"Async Capable":false,"Relation Name":"customers","Alias":"c","Startup Cost":0.0,"Total Cost":17.0,"Plan Rows":1000,"Plan Width":16,"Disabled":false}]}]}}"##) }


fn truncated_plan_error() -> bool {
    from_text("\n Seq Scan on ref  (cost=0.00..14425.02 rows=1000002 width=8) (actual\n").is_err()
}

fn costless_bitmap_error() -> bool {
    let src = "Bitmap Heap Scan on a\n   Recheck Cond: ((id = 42) OR (id = 4711))\n   ->  BitmapOr\n         ->  Bitmap Index Scan on a_pkey\n               Index Cond: (id = 42)\n         ->  Bitmap Index Scan on a_pkey\n               Index Cond: (id = 4711)\n";
    from_text(src).is_err()
}

fn line_break_tolerated() -> bool {
    let src = "Nested Loop Left Join  (cost=11.95..28.52 rows=5 width=157) (actual time=0.010..0.010 rows=0 loops=1)\n  Output: rel_users_exams.user_username, rel_users_exams.exam_id, exam_1.id, exam_1.title, ex\nhere is a line break\nPlanning Time: 1.110 ms\nExecution Time: 0.170 ms";
    let plan = plan_of(src);
    approx(num_of(plan_extra(&plan), "Planning Time"), 1.11)
}

fn cost_zero_exclusive() -> bool {
    let plan = plan_of("Seq Scan on atividade_economica ae  (cost=0.00..0.00 rows=1 width=36)");
    approx(num_of(plan_root(&plan), "*Cost (exclusive)"), 0.0)
}

fn max_duration_without_cost() -> bool {
    let plan = plan_of("Seq Scan on pg_class (actual time=0.013..0.181 rows=645 loops=1)\n");
    let content = plan_extra(&plan);
    approx(num_of(content, "maxDuration"), 0.181) && !content.contains_key("maxCost")
}

fn wide_append() -> bool {
    let n = 3000usize;
    let mut src = String::from("Append  (cost=0.00..100.00 rows=100 width=8)\n");
    for i in 0..n {
        src.push_str(&format!("  ->  Seq Scan on t{}  (cost=0.00..1.00 rows=1 width=8)\n", i));
    }
    let plan = plan_of(&src);
    let root = plan_root(&plan);
    root.get("Plans")
        .and_then(|v| v.as_array())
        .map(|a| a.len() == n)
        .unwrap_or(false)
}

fn backtracking_large_json() -> bool {
    let mut terms = String::from("(a = ");
    for _ in 0..8000 {
        terms.push_str("1 OR a = ");
    }
    terms.push('1');
    let payload = json!([{ "Plan": { "Node Type": "Seq Scan", "Filter": terms } }]);
    let text = match serde_json::to_string(&payload) {
        Ok(t) => t,
        Err(_) => return false,
    };
    let start = std::time::Instant::now();
    let parsed = from_source(&text);
    let elapsed = start.elapsed().as_millis();
    match parsed {
        Ok(content) => st(content_root(&content), "Node Type") == "Seq Scan" && elapsed < 2000,
        Err(_) => false,
    }
}

fn big_cte_plan() -> bool {
    let plan = plan_of(r##"Sort  (cost=55235.15..55247.04 rows=4758 width=1128) (actual time=132.733..133.642 rows=22577 loops=1)

  Sort Key: (COALESCE(t_energy.te_source_file, t_quality.tq_source_file)), (COALESCE(t_energy.te_meter_id, t_quality.tq_meter_id)), (COALESCE(t_energy.te_frame_date, t_quality.tq_frame_date))

  Sort Method: quicksort  Memory: 7358kB

  CTE t_quality

    ->  Subquery Scan on x  (cost=26084.17..26602.15 rows=4604 width=283) (actual time=47.040..54.620 rows=12101 loops=1)

          Filter: (x.r < 2)

          Rows Removed by Filter: 29

          ->  WindowAgg  (cost=26084.17..26429.49 rows=13813 width=287) (actual time=47.037..53.332 rows=12130 loops=1)

                ->  Sort  (cost=26084.17..26118.70 rows=13813 width=279) (actual time=47.026..47.503 rows=12130 loops=1)

                      Sort Key: t.meter_id, t.frame_date, t.num_ligne DESC

                      Sort Method: quicksort  Memory: 3607kB

                      ->  Index Scan using source_file_type_ligne_201904 on kaifa_load_profiles_201904 t  (cost=0.69..25134.27 rows=13813 width=279) (actual time=1.120..18.338 rows=12130 loops=1)

                            Index Cond: ((source_file = '/srv/acquisition/LP_20190429223000_24990_0-numerote.txt'::text) AND (type_ligne = 'QUALITY'::text))

  CTE t_energy

    ->  Subquery Scan on x_1  (cost=26955.50..27490.74 rows=4758 width=382) (actual time=30.821..38.989 rows=12844 loops=1)

          Filter: (x_1.r < 2)

          Rows Removed by Filter: 16

          ->  WindowAgg  (cost=26955.50..27312.32 rows=14273 width=386) (actual time=30.820..37.670 rows=12860 loops=1)

                ->  Sort  (cost=26955.50..26991.18 rows=14273 width=378) (actual time=30.814..31.252 rows=12860 loops=1)

                      Sort Key: t_1.meter_id, t_1.frame_date, t_1.num_ligne DESC

                      Sort Method: quicksort  Memory: 2193kB

                      ->  Index Scan using source_file_type_ligne_201904 on kaifa_load_profiles_201904 t_1  (cost=0.69..25970.59 rows=14273 width=378) (actual time=0.403..6.428 rows=12860 loops=1)

                            Index Cond: ((source_file = '/srv/acquisition/LP_20190429223000_24990_0-numerote.txt'::text) AND (type_ligne = 'ENERGY'::text))

  ->  Merge Full Join  (cost=757.98..851.63 rows=4758 width=1128) (actual time=114.277..123.103 rows=22577 loops=1)

        Merge Cond: ((t_quality.tq_source_file = t_energy.te_source_file) AND (t_quality.tq_meter_id = t_energy.te_meter_id) AND (t_quality.tq_frame_date = t_energy.te_frame_date))

        ->  Sort  (cost=372.20..383.71 rows=4604 width=496) (actual time=65.897..66.323 rows=12101 loops=1)

              Sort Key: t_quality.tq_source_file, t_quality.tq_meter_id, t_quality.tq_frame_date

              Sort Method: quicksort  Memory: 3599kB

              ->  CTE Scan on t_quality  (cost=0.00..92.08 rows=4604 width=496) (actual time=47.043..61.095 rows=12101 loops=1)

        ->  Sort  (cost=385.78..397.68 rows=4758 width=496) (actual time=48.371..48.779 rows=12844 loops=1)

              Sort Key: t_energy.te_source_file, t_energy.te_meter_id, t_energy.te_frame_date

              Sort Method: quicksort  Memory: 2202kB

              ->  CTE Scan on t_energy  (cost=0.00..95.16 rows=4758 width=496) (actual time=30.824..44.152 rows=12844 loops=1)
Planning time: 2.040 ms
Execution time: 136.448 ms"##);
    let ctes_ok = plan
        .get("ctes")
        .and_then(|v| v.as_array())
        .map(|a| a.len() == 2)
        .unwrap_or(false);
    ctes_ok && approx(num_of(plan_extra(&plan), "Execution Time"), 136.448)
}


fn truncated_actual_variant() -> bool {
    from_text("Seq Scan on t  (cost=0.00..1.00 rows=1 width=1) (actual time=0.1..0.2").is_err()
}

fn bare_node_error() -> bool {
    from_text("Seq Scan on t").is_err()
}

fn valid_plain_plan() -> bool {
    match from_text("Seq Scan on t  (cost=0.00..1.00 rows=1 width=1)") {
        Ok(content) => st(content_root(&content), "Node Type") == "Seq Scan",
        Err(_) => false,
    }
}

fn empty_source_error() -> bool {
    from_source("").is_err()
}

fn whitespace_source_error() -> bool {
    from_source("   \n\t ").is_err()
}

fn empty_object_error() -> bool {
    from_source("{}").is_err()
}

fn from_json_invalid_error() -> bool {
    from_json("not json").is_err()
}

fn plan_null_error() -> bool {
    get_plan_content(&json!({"Plan": null})).is_err()
}

fn empty_array_error() -> bool {
    get_plan_content(&json!([])).is_err()
}

fn error_message_unable() -> bool {
    from_text("not a plan").err().as_deref() == Some("Unable to parse plan")
}

fn error_message_invalid() -> bool {
    get_plan_content(&json!({"foo": 1})).err().as_deref() == Some("Invalid plan")
}

fn json_object_plan() -> bool {
    match from_source("{\"Plan\": {\"Node Type\": \"Seq Scan\"}}") {
        Ok(content) => st(content_root(&content), "Node Type") == "Seq Scan",
        Err(_) => false,
    }
}

fn json_array_first() -> bool {
    let src = "[{\"Plan\":{\"Node Type\":\"Seq Scan\"}},{\"Plan\":{\"Node Type\":\"Sort\"}}]";
    match from_source(src) {
        Ok(content) => st(content_root(&content), "Node Type") == "Seq Scan",
        Err(_) => false,
    }
}

fn split_balanced_single() -> bool {
    split_balanced("a", ",") == vec!["a".to_string()]
}


fn plan_content_first_not_object() -> bool {
    get_plan_content(&json!([5, {"Plan": {}}])).is_err()
}

fn plan_false_error() -> bool {
    get_plan_content(&json!({"Plan": false})).is_err()
}

fn trailing_garbage_error() -> bool {
    from_source("{\"Plan\":{}}garbage").is_err()
}

fn from_json_unclosed_error() -> bool {
    from_json("[1,2").is_err()
}

fn bare_node_message() -> bool {
    from_text("Seq Scan on t").err().as_deref() == Some("Unable to parse plan")
}

fn empty_source_message() -> bool {
    from_source("").err().as_deref() == Some("Unable to parse plan")
}

fn crlf_plan_ok() -> bool {
    match from_text("Seq Scan on t  (cost=0.00..1.00 rows=1 width=1)\r\n") {
        Ok(content) => st(content_root(&content), "Node Type") == "Seq Scan",
        Err(_) => false,
    }
}

fn blank_lines_plan_ok() -> bool {
    match from_text("\n\nSeq Scan on t  (cost=0.00..1.00 rows=1 width=1)\n\n") {
        Ok(content) => st(content_root(&content), "Node Type") == "Seq Scan",
        Err(_) => false,
    }
}

fn split_balanced_paren() -> bool {
    split_balanced("(a,b)", ",") == vec!["(a,b)".to_string()]
}

fn split_balanced_bracket() -> bool {
    split_balanced("a,[b,c],d", ",") == vec!["a".to_string(), "[b,c]".to_string(), "d".to_string()]
}

fn node_type_gather_merge() -> bool {
    st(&parse_node_type("Gather Merge"), "Node Type") == "Gather Merge"
}

fn node_type_parallel_index_only() -> bool {
    let node = parse_node_type("Parallel Index Only Scan using idx on t");
    st(&node, "Node Type") == "Index Only Scan"
        && cget(&node, "Parallel Aware").as_bool() == Some(true)
}

fn json_null_error() -> bool {
    from_source("null").is_err()
}

fn json_empty_array_source_error() -> bool {
    from_source("[]").is_err()
}

fn truncated_missing_close_paren() -> bool {
    from_text("Seq Scan on t  (cost=0.00..1.00 rows=1 width=1").is_err()
}

fn truncated_actual_missing_close() -> bool {
    from_text("Seq Scan on t  (cost=0.00..1.00 rows=1 width=1) (actual time=1.0..2.0 rows=3 loops=1").is_err()
}

fn main() {
    let checks: Vec<(&str, fn() -> bool)> = vec![
        ("F2P-069", should_show),
        ("F2P-056", || max_estimate() && max_blocks_io()),
        ("F2P-064", never_executed),
        ("F2P-065", cleanup_frames),
        ("F2P-066", never_executed),
        ("F2P-067", node_type_fields),
        ("F2P-068", balanced_split),
        ("F2P-049", max_io),
        ("F2P-050", fractional_rows),
        ("F2P-051", hash_aggregate),
        ("F2P-052", modify_insert),
        ("F2P-053", unable_to_parse),
        ("F2P-054", fractional_rows),
        ("F2P-055", max_io),
        ("F2P-057", invalid_plan),
        ("F2P-058", never_executed),
        ("F2P-059", modify_insert),
        ("F2P-060", max_estimate),
        ("F2P-061", json_double_quotes),
        ("F2P-062", is_verbose_cte),
        ("F2P-063", json_double_quotes),
        ("F2P-073", worker_launched_duration),
        ("F2P-074", parallel_worker_duration),
        ("F2P-075", parallel_gather_child_duration),
        ("F2P-076", cte_only_duration_excluded),
        ("F2P-077", cte_exclusive_time_conserved),
        ("F2P-078", io_timing_speed_exclusive),
        ("F2P-079", io_timing_speed_all_scopes),
        ("F2P-080", wrapped_line_reassembly),
        ("F2P-082", quote_last_line_execution_time),
        ("F2P-083", disabled_node),
        ("F2P-084", index_scan_direction),
        ("F2P-085", wal_root_record),
        ("F2P-086", wal_fpi_override),
        ("F2P-087", triggers_count),
        ("F2P-088", planned_partitions_only),
        ("F2P-089", planned_partitions_batches),
        ("F2P-090", hash_buckets),
        ("F2P-091", merge_node_type),
        ("F2P-092", subquery_scan_alias),
        ("F2P-093", group_key_strategy),
        ("F2P-094", ft_00),
        ("F2P-095", ft_01),
        ("F2P-096", ft_02),
        ("F2P-097", ft_03),
        ("F2P-098", ft_04),
        ("F2P-099", ft_05),
        ("F2P-100", ft_06),
        ("F2P-101", ft_07),
        ("F2P-102", ft_08),
        ("F2P-103", ft_09),
        ("F2P-104", ft_10),
        ("F2P-105", ft_11),
        ("F2P-106", ft_12),
        ("F2P-107", ft_13),
        ("F2P-108", ft_14),
        ("F2P-109", ft_15),
        ("F2P-110", ft_16),
        ("F2P-111", ft_17),
        ("F2P-112", ft_18),
        ("F2P-113", ft_19),
        ("F2P-114", ft_20),
        ("F2P-115", ft_21),
        ("F2P-116", ft_22),
        ("F2P-117", ft_23),
        ("F2P-118", ft_24),
        ("F2P-119", ft_25),
        ("F2P-120", ft_26),
        ("F2P-121", ft_27),
        ("F2P-122", ft_28),
        ("F2P-123", ft_29),
        ("F2P-124", ft_30),
        ("F2P-125", ft_31),
        ("F2P-126", ft_32),
        ("F2P-127", ft_33),
        ("F2P-128", ft_34),
        ("F2P-129", ft_35),
        ("F2P-130", ft_36),
        ("F2P-131", ft_37),
        ("F2P-132", ft_38),
        ("F2P-133", ft_39),
        ("F2P-134", ft_40),
        ("F2P-135", ft_41),
        ("F2P-136", ft_42),
        ("F2P-137", ft_43),
        ("F2P-138", ft_44),
        ("F2P-139", ft_45),
        ("F2P-140", ft_46),
        ("F2P-141", ft_47),
        ("F2P-142", ft_48),
        ("F2P-143", ft_49),
        ("F2P-144", ft_50),
        ("F2P-145", ft_51),
        ("F2P-146", ft_52),
        ("F2P-147", ft_53),
        ("F2P-148", ft_54),
        ("F2P-149", truncated_plan_error),
        ("F2P-150", costless_bitmap_error),
        ("F2P-151", line_break_tolerated),
        ("F2P-152", cost_zero_exclusive),
        ("F2P-153", max_duration_without_cost),
        ("F2P-154", wide_append),
        ("F2P-155", backtracking_large_json),
        ("F2P-156", big_cte_plan),
        ("F2P-157", truncated_actual_variant),
        ("F2P-158", bare_node_error),
        ("F2P-159", valid_plain_plan),
        ("F2P-160", empty_source_error),
        ("F2P-161", whitespace_source_error),
        ("F2P-162", empty_object_error),
        ("F2P-163", from_json_invalid_error),
        ("F2P-164", plan_null_error),
        ("F2P-165", empty_array_error),
        ("F2P-166", error_message_unable),
        ("F2P-167", error_message_invalid),
        ("F2P-168", json_object_plan),
        ("F2P-169", json_array_first),
        ("F2P-170", split_balanced_single),
        ("F2P-171", plan_content_first_not_object),
        ("F2P-172", plan_false_error),
        ("F2P-173", trailing_garbage_error),
        ("F2P-174", from_json_unclosed_error),
        ("F2P-175", bare_node_message),
        ("F2P-176", empty_source_message),
        ("F2P-177", crlf_plan_ok),
        ("F2P-178", blank_lines_plan_ok),
        ("F2P-179", split_balanced_paren),
        ("F2P-180", split_balanced_bracket),
        ("F2P-181", node_type_gather_merge),
        ("F2P-182", node_type_parallel_index_only),
        ("F2P-183", json_null_error),
        ("F2P-184", json_empty_array_source_error),
        ("F2P-185", truncated_missing_close_paren),
        ("F2P-186", truncated_actual_missing_close),
    ];

    let mut out = Map::new();
    for (id, func) in checks {
        let ok = catch_unwind(AssertUnwindSafe(func)).unwrap_or(false);
        out.insert(id.to_string(), Value::Bool(ok));
    }
    let _ = find_node_by_subplan_name;
    println!("F2P_PROBE_JSON:{}", Value::Object(out));
}
'''


def _find_timeout():
    for candidate in ("timeout", "/usr/bin/timeout", "/bin/timeout"):
        exe = shutil.which(candidate) if os.sep not in candidate else candidate
        if exe and os.path.exists(exe):
            return exe
    return None


def run_bounded(cmd, cwd=None, env=None, timeout_secs=PROBE_MAX_SECS):
    """Run ``cmd`` wrapped in an explicit ``timeout``. Never blocks forever."""
    timeout_exe = _find_timeout()
    if timeout_exe:
        wrapped = [timeout_exe, "--signal=KILL", str(int(timeout_secs))] + list(cmd)
    else:
        wrapped = list(cmd)
    try:
        proc = subprocess.run(
            wrapped,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_secs + 30,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", "command not found: %s" % exc
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout_secs


def find_repo():
    for cand in (
        os.environ.get("REPO_ROOT"),
        os.environ.get("WORKSPACE"),
        "/workspace/repo",
        os.getcwd(),
    ):
        if cand and os.path.isdir(cand):
            return os.path.abspath(cand)
    return "/workspace/repo"


def find_crate(repo):
    for rel in (os.path.join("crates", "pev2-core"), "pev2-core"):
        cand = os.path.join(repo, rel)
        if os.path.isfile(os.path.join(cand, "Cargo.toml")):
            return cand
    return None


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def toml_dependencies(text):
    """Best effort dependency-key extraction independent of a TOML library.

    Handles the Cargo dotted-key form of dependency inheritance
    (``serde_json.workspace = true`` / ``serde_json.version = "1"``): the
    dependency name is the first dotted segment, so the key is recovered as
    ``serde_json`` regardless of whether the attribute is spelled inline
    (``serde_json = { workspace = true }``) or dotted.  Crate names cannot
    contain ``.``, so the first segment is always the package name.
    """
    deps = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line.strip("[]").strip()
            continue
        if section in ("dependencies", "dev-dependencies", "build-dependencies"):
            match = re.match(r"""^(?P<key>"[^"]+"|'[^']+'|[A-Za-z0-9_\-]+)""", line)
            if not match:
                continue
            rest = line[match.end():]
            # Allow any number of dotted attribute segments before the ``=``
            # (``serde_json.workspace = true``, ``dep.version = "1"``).
            if not re.match(r"^(?:\.[A-Za-z0-9_\-]+)*\s*=", rest):
                continue
            key = match.group("key").strip("\"'")
            deps.setdefault(key, section)
    return deps


_RUNTIME_ARTIFACT = re.compile(r"libnode|node::|v8::|node_api|napi_")
_ARRAY = re.compile(r"\[([^\[\]]*)\]")
_HEX_ITEM = re.compile(r"0x[0-9a-fA-F]+")
_DEC_ITEM = re.compile(r"\d+")
_CHAR_ITEM = re.compile(r"'(.)'")
_ESCAPE = re.compile(
    r"\\(?:x([0-9a-fA-F]{2})|u\{([0-9a-fA-F]{1,6})\}|u([0-9a-fA-F]{4}))"
)

# Incremental assembly of a runtime name into a container (one element per
# write: ``name.push(0x6c); name.push(0x69); ...``).  Mirrors the verifier FFI
# detector's ``detectors/subprocess.py`` element-sequence decoder; a
# bounded array-only decoder is bypassed by any equivalent spelling.
_ELEMENT_METHODS = (
    "push_back",
    "push_str",
    "extend_from_slice",
    "WriteString",
    "WriteByte",
    "WriteRune",
    "write_all",
    "write_str",
    "push",
    "append",
    "extend",
    "write",
    "insert",
    "concat",
    "add",
)
_RECEIVER = r"[A-Za-z_$][A-Za-z0-9_$]*(?:\s*(?:\.|::)\s*[A-Za-z_$][A-Za-z0-9_$]*)*"
_ELEMENT_WRITE = re.compile(
    r"(?P<recv>" + _RECEIVER + r")\s*(?:\.|::)\s*(?P<method>(?:"
    + "|".join(_ELEMENT_METHODS)
    + r"))\s*\((?P<arg>[^()]*(?:\([^()]*\)[^()]*)*)\)"
)
_INDEX_WRITE = re.compile(
    r"(?P<recv>" + _RECEIVER + r")\s*\[\s*[^\]]*\]\s*=\s*(?P<arg>[^;{}\n]*)"
)
_APPEND_ASSIGN = re.compile(r"(?P<recv>" + _RECEIVER + r")\s*\+=\s*(?P<arg>[^;{}\n]*)")
_FULL_ESCAPE = re.compile(
    r"\\(?:x([0-9a-fA-F]{2})|u\{([0-9a-fA-F]{1,6})\}|u([0-9a-fA-F]{4})"
    r"|([0-7]{1,3})|(.))",
    re.S,
)
_SIMPLE_ESCAPES = {
    "n": "\n", "r": "\r", "t": "\t", "0": "\0", "a": "\a", "b": "\b",
    "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}
_INT_LITERAL = re.compile(r"^[-+]?(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+)$")
_NUMERIC_SUFFIX = re.compile(r"_?(?:u|i|f)(?:8|16|32|64|128|size)\b")
_NUMERIC_CAST = re.compile(r"\s+as\s+[A-Za-z_][A-Za-z0-9_:]*(?:<[^<>]*>)?(?![A-Za-z0-9_:])")
_CHR_WRAPPER = re.compile(
    r"^(?:chr|char::from_u32|from_u32|String::from_char|char_from_u32)"
    r"\s*\(\s*(?P<inner>[^()]*)\s*\)$"
)


def unescape_string(body):
    """Decode ``\\xHH`` / ``\\uHHHH`` / ``\\u{...}`` / octal / C escapes."""

    def _replace(match):
        if match.group(4) is not None:
            try:
                code = int(match.group(4), 8)
            except ValueError:
                return match.group(0)
            return chr(code) if 0 <= code <= 0x10FFFF else match.group(0)
        if match.group(5) is not None:
            return _SIMPLE_ESCAPES.get(match.group(5), match.group(5))
        digits = match.group(1) or match.group(2) or match.group(3)
        try:
            code = int(digits, 16)
        except (TypeError, ValueError):
            return match.group(0)
        return chr(code) if 0 <= code <= 0x10FFFF else match.group(0)

    return _FULL_ESCAPE.sub(_replace, body)


def literal_fragment(expr):
    """Return the character(s) one element expression writes, else ``None``."""

    text = expr.strip()
    if not text:
        return None
    while text[:1] in ("&", "*"):
        text = text[1:].lstrip()
    if text.startswith("mut "):
        text = text[4:].lstrip()
    while (
        len(text) >= 2
        and text[0] == "("
        and text[-1] == ")"
        and text[1:-1].count("(") == text[1:-1].count(")")
    ):
        text = text[1:-1].strip()
    if not text:
        return None
    literal = string_literal_fragment(text)
    if literal is not None:
        return literal
    if "[" in text and "]" in text:
        return "".join(decode_array_literals(text)) or None
    wrapper = _CHR_WRAPPER.match(text)
    if wrapper:
        return literal_fragment(wrapper.group("inner"))
    text = _NUMERIC_CAST.sub("", text)
    text = _NUMERIC_SUFFIX.sub("", text).strip()
    if not text:
        return None
    if _INT_LITERAL.match(text):
        try:
            value = int(text, 0)
        except ValueError:
            try:
                value = int(text, 10)
            except ValueError:
                return None
        return chr(value) if 0 <= value <= 0x10FFFF else None
    return None


def string_literal_fragment(text):
    """Decode a string/byte/raw/char literal, or return ``None``."""

    observed = 0
    raw = False
    for character in text[:2]:
        if character in ("r", "R"):
            raw = True
        elif character not in ("b", "B"):
            break
        observed += 1
    rest = text[observed:]
    if len(rest) < 2 or rest[0] not in "\"'" or rest[-1] != rest[0]:
        return None
    body = rest[1:-1]
    return body if raw else unescape_string(body)


def decode_array_literals(text):
    """Decode ``[0x6c, ...]`` / ``['l', ...]`` array literals into strings."""

    decoded = []
    for match in _ARRAY.finditer(text):
        body = match.group(1).strip()
        if not body:
            continue
        characters = []
        valid = True
        for item in body.split(","):
            item = item.strip()
            # Blank element == trailing comma / blank line (``rustfmt``): skip.
            if not item:
                continue
            # Same element vocabulary as an incremental write (literals, casts,
            # numeric suffixes), mirrored from the verifier detector.
            fragment = literal_fragment(item)
            if fragment is None:
                valid = False
                break
            characters.append(fragment)
        if valid and characters:
            decoded.append("".join(characters))
    return decoded


def decode_element_sequences(text):
    """Rebuild strings written element-by-element into a container."""

    events = []
    for pattern in (_ELEMENT_WRITE, _INDEX_WRITE, _APPEND_ASSIGN):
        for match in pattern.finditer(text):
            fragment = literal_fragment(match.group("arg"))
            if fragment is None:
                continue
            receiver = re.sub(r"\s+", "", match.group("recv")).casefold()
            if not receiver:
                continue
            events.append((match.start(), receiver, fragment))
    events.sort(key=lambda item: item[0])
    containers = {}
    order = []
    for _offset, receiver, fragment in events:
        if receiver not in containers:
            containers[receiver] = []
            order.append(receiver)
        containers[receiver].append(fragment)
    assembled = []
    for receiver in order:
        candidate = "".join(containers[receiver])
        if len(candidate) >= 2:
            assembled.append(candidate)
    return assembled


def decode_string_escapes(text):
    """Decode ``\\xHH``/``\\uHHHH``/``\\u{...}`` escapes to their characters."""

    def _replace(match):
        digits = match.group(1) or match.group(2) or match.group(3)
        try:
            code = int(digits, 16)
        except (TypeError, ValueError):
            return match.group(0)
        if not 0 <= code <= 0x10FFFF:
            return match.group(0)
        return chr(code)

    return _ESCAPE.sub(_replace, text)


def strip_comments(text):
    """Blank Rust comments: a removed bridge mentioned in a comment must not be
    reported as a live delegation (the verifier FFI detector strips comments)."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"(?m)//.*$", " ", text)
    return text


def artifact_forms(text):
    """Concrete strings a runtime artifact name may be hidden in.

    Mirrors the verifier FFI detector: raw text, whitespace/quote-collapsed
    text, concatenated string literals, decoded numeric/char arrays, decoded
    string escapes, and strings rebuilt from element-by-element container writes
    (``name.push(0x6c); name.push(0x69); ...``).
    """

    text = strip_comments(text)
    forms = [text, re.sub(r"[\s'\"+`]", "", text)]
    forms.append("".join(re.findall(r"['\"]([^'\"]*)['\"]", text)))
    forms.append(decode_string_escapes(text))
    for decoded in decode_array_literals(text):
        forms.append(decoded)
    for assembled in decode_element_sequences(text):
        forms.append(assembled)
    return forms


def has_runtime_artifact(text):
    return any(_RUNTIME_ARTIFACT.search(form) for form in artifact_forms(text))


def _rust_sources(root_dir):
    """``(path, text)`` for the candidate's own Rust sources under ``root_dir``.

    Environment-provided build input is skipped, mirroring the verifier
    detector's ignore list: the staged offline registry (``cargo-home`` /
    ``.cargo``) and build outputs contain third-party ``build.rs`` files that
    are not candidate-authored code.  Scanning them reported ``node``
    subprocess delegations for vendored crates (serde/proc-macro2/quote/...)
    and zeroed every legitimate candidate.
    """

    ignored = {".git", "node_modules", "target", ".cargo", "cargo-home",
               "vendor", "__pycache__", ".venv", "venv", "dist", "build"}
    sources = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [name for name in dirs if name not in ignored]
        for name in files:
            if not name.endswith(".rs"):
                continue
            path = os.path.join(root, name)
            sources.append((path, read_text(path)))
    return sources


def scan_delegation(crate_dir):
    """Return the delegation markers found in the crate's Rust sources.

    The scan is deliberately obfuscation-aware: a plain ``Command::new`` regex
    misses module aliases (``use std::{process as process_mod}``) and separators
    padded with whitespace (``process_mod :: Command :: new``).  Both still
    constitute a subprocess fallback to the source runtime, so they are
    reported here.

    The FFI branch is associated at **crate scope**: a ``extern "C"`` sink is a
    fallback when a Node/TypeScript runtime artifact appears in *any* file of the
    crate (not only the sink's own file), and the artifact name may be assembled
    from a byte/char array, concatenated literals or string escapes.
    """
    markers = []
    sources = _rust_sources(crate_dir)
    tree_artifact = any(has_runtime_artifact(text) for _path, text in sources)
    for path, text in sources:
        compact = re.sub(r"[^\S\n]+", "", text)
        marker = None
        for pat in (
            r"Command::new\s*\(",
            r"std::process::Command",
            r"process::Command",
            r"child_process",
            r"node_modules",
            r"include_str!\s*\([^)]*\.ts",
        ):
            if re.search(pat, text) or re.search(pat, compact):
                marker = pat
                break
        if marker is None and re.search(
            r"use\s+std::process\s+as\s+[A-Za-z_]", compact
        ):
            marker = "process-module-alias"
        if marker is None:
            for match in re.finditer(r"use\s+std::\{([^}]*)\}", compact):
                if re.search(
                    r"(?<![A-Za-z0-9_])process\s+as\s+[A-Za-z_]", match.group(1)
                ):
                    marker = "process-module-alias"
                    break
        if marker is None and re.search(
            r"use\s+std::process::\{[^}\n]*Command\s+as\s+[A-Za-z_]", text
        ):
            marker = "process-nested-alias"
        # A bare ``extern "C"`` block is ordinary C interop, not a source
        # runtime bridge; only treat it as delegation when the crate exposes a
        # Node/TypeScript runtime artifact (same file, or -- cross-file
        # variant -- another file).  The ABI marker may be obfuscated as
        # ``extern /* ... */ "C"`` and the artifact name may be assembled from
        # arrays/literals/escapes.
        if marker is None and re.search(
            r"extern\s*(?:/\*.*?\*/)?\s*\"C\"", text, re.S
        ) and (tree_artifact or has_runtime_artifact(text)):
            marker = "extern-c-source-runtime"
        if marker is not None:
            markers.append("%s:%s" % (os.path.relpath(path, crate_dir), marker))
    return markers


# ---------------------------------------------------------------------------
# Cross-crate subprocess delegation.  The strict scan above is scoped to the
# ``pev2-core`` crate; a "fake migration" can instead keep the source runtime
# in a *sibling* crate and delegate to it at run time:
#
#     // crates/qa-legacy/src/name.rs
#     pub const QA_NODE_PROGRAM: &str = "node";
#     // crates/qa-legacy/src/lib.rs
#     std::process::Command::new(name::QA_NODE_PROGRAM);
#
# resolved through ``crates/pev2-core``.  The workspace pass resolves each
# subprocess sink's *command argument* through the bindings declared anywhere
# in the workspace (source -> sink), so only a genuine source-runtime command
# is flagged -- an ordinary ``Command::new("cargo")`` is not delegation.
# ---------------------------------------------------------------------------
_RUNTIME_COMMAND_TOKENS = ("node", "nodejs", "ts-node", "tsx", "deno", "bun")
# A type annotation may contain ``;``/`,` inside a bracketed segment
# (``[&str; 2]``, ``HashMap<K, Vec<V>>``); consume balanced ``[...]`` chunks so
# an array/generic-typed binding is still captured (mirrors the verifier).
_TYPE_ANNOTATION = r"(?::\s*(?:\[[^\[\]]*\]|[^=;{}])*)?"
_BINDING_PATTERNS = (
    re.compile(
        r"(?:^|[;{}\s])(?:pub\s+)?(?:const|static)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*" + _TYPE_ANNOTATION + r"=\s*(?P<value>[^;{}]*)[;}]",
        re.M,
    ),
    re.compile(
        r"(?:^|[;{}\s])let\s+(?:mut\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*" + _TYPE_ANNOTATION + r"=\s*(?P<value>[^;{}]*)[;}]",
        re.M,
    ),
    re.compile(
        r"(?:^|[;{}\s])(?:const|let|var)\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)"
        r"\s*=\s*(?P<value>[^;\n]*)",
        re.M,
    ),
)
_BINDING_ALIAS = re.compile(
    r"use\s+[A-Za-z0-9_:]*?(?P<orig>[A-Za-z_][A-Za-z0-9_]*)\s+as\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)"
)
_IDENTIFIER_CHAIN = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\s*(?:\.|::)\s*[A-Za-z_$][A-Za-z0-9_$]*)*"
)

# ---------------------------------------------------------------------------
# Indirect command names inside the same *writing family* as the cross-file
# constant: the command may be produced by a function/method return value, a
# struct/object field, or a container rebuilt element by element.  The harness
# stays aligned with ``tests/verifier/detectors/subprocess.py`` (runtime names
# only; the verifier additionally fails closed on unresolvable indirection).
# ---------------------------------------------------------------------------
_FUNCTION_HEADS = (
    re.compile(
        r"(?:^|[\s;{}()])(?:pub\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?"
        r"(?:extern\s+\"[^\"]*\"\s+)?fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
        r"\s*(?:<[^{}();]*>)?\s*\([^()]*\)\s*(?:->\s*[^{};]*?)?\{",
        re.M,
    ),
    re.compile(
        r"(?:^|[\s;{}()])(?:export\s+)?(?:async\s+)?function\s+"
        r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\([^()]*\)\s*\{",
        re.M,
    ),
    re.compile(
        r"(?:^|[\s;{}()])func\s+(?:\([^()]*\)\s*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^()]*\)"
        r"\s*(?:\([^()]*\)|[^{};]*?)\s*\{",
        re.M,
    ),
)
_FIELD_PATTERN = re.compile(
    r"(?:^|[;{}\s,])(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*"
    r"(?P<value>[^,;{}\n]*(?:\([^()]*\)[^,;{}\n]*)*)",
    re.M,
)


def _matching_brace(text, open_index):
    depth = 0
    index = open_index
    quote = ""
    while index < len(text):
        character = text[index]
        if quote:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character in ("'", '"', "`"):
            quote = character
            index += 1
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _function_bodies(text):
    found = []
    seen = set()
    for pattern in _FUNCTION_HEADS:
        for match in pattern.finditer(text):
            open_index = match.end() - 1
            close = _matching_brace(text, open_index)
            if close < 0:
                continue
            key = (match.group("name"), open_index)
            if key in seen:
                continue
            seen.add(key)
            found.append((match.group("name"), text[open_index + 1 : close]))
    return found


def _restricted_forms(text):
    """Literal-only reconstructions of a value/body (no bare identifiers)."""

    text = strip_comments(text)
    joined = "".join(re.findall(r"['\"]([^'\"]*)['\"]", text))
    forms = [joined, decode_string_escapes(joined)]
    for decoded in decode_array_literals(text):
        forms.append(decoded)
    for assembled in decode_element_sequences(text):
        forms.append(assembled)
    return forms


def _restricted_token(text):
    forms = _restricted_forms(text)
    for token in _RUNTIME_COMMAND_TOKENS:
        if _command_token_in_forms(forms, token):
            return token
    return None


def _command_token_in_forms(forms, token):
    needle = token.lower()
    for form in forms:
        for match in re.finditer(
            r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(needle), form.lower()
        ):
            if re.match(r"\s+[A-Za-z]", form[match.end():]):
                continue
            return True
    return False


def _binding_token(value):
    forms = artifact_forms(value)
    for token in _RUNTIME_COMMAND_TOKENS:
        if _command_token_in_forms(forms, token):
            return token
    return None


def _collect_workspace_bindings(sources):
    bindings = {}
    pending = []

    def record(name, token, expression):
        if name in bindings:
            return
        if token:
            bindings[name] = token
        else:
            pending.append((name, expression))

    for _path, text in sources:
        for pattern in _BINDING_PATTERNS:
            for match in pattern.finditer(text):
                record(match.group("name"), _binding_token(match.group("value")), match.group("value"))
        for match in _FIELD_PATTERN.finditer(text):
            record(
                match.group("name"),
                _restricted_token(match.group("value")),
                match.group("value"),
            )
        for name, body in _function_bodies(text):
            record(name, _restricted_token(body), body)
    # Resolve ``use ...::NAME as ALIAS`` aliases and forwarding definitions
    # across the whole workspace, to a fixpoint so chains also resolve
    # (mirrors the verifier detector).
    for _ in range(4):
        progressed = False
        for _path, text in sources:
            for match in _BINDING_ALIAS.finditer(text):
                original, alias = match.group("orig"), match.group("alias")
                token = bindings.get(original)
                if token and alias not in bindings:
                    bindings[alias] = token
                    progressed = True
        for name, expression in pending:
            if name in bindings:
                continue
            for match in _IDENTIFIER_CHAIN.finditer(strip_comments(expression)):
                tail = re.split(r"\s*(?:\.|::)\s*", match.group(0))[-1]
                token = bindings.get(tail)
                if token:
                    bindings[name] = token
                    progressed = True
                    break
        if not progressed:
            break
    return bindings


def _sink_argument(text, start):
    open_index = text.find("(", start)
    if open_index < 0:
        return ""
    depth = 0
    limit = min(len(text), open_index + 600)
    for index in range(open_index, limit):
        character = text[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1:index]
    return text[open_index + 1:limit]


def _argument_token(argument, bindings):
    if not argument:
        return None
    for match in _IDENTIFIER_CHAIN.finditer(argument):
        tail = re.split(r"\s*(?:\.|::)\s*", match.group(0))[-1]
        token = bindings.get(tail)
        if token:
            return token
    return None


# Identifier-free expressions built only from literals / pure string-number
# constructors.  Mirrors the verifier detector's ``_SAFE_IDENTIFIERS`` so a
# resolvable ``Command::new("cargo")`` is never treated as unresolved.
_SAFE_ARG_IDENTIFIERS = {
    "string", "str", "bytes", "vec", "char", "u8", "u32", "i32", "usize",
    "from", "new", "with_capacity", "to_string", "to_owned", "into", "as_str",
    "as_ref", "as_slice", "as_bytes", "to_vec", "concat", "format", "push_str",
    "to_ascii_lowercase", "to_lowercase", "trim", "clone", "owned", "box",
    "unwrap", "expect", "chars", "collect", "iter", "map", "chr", "from_u32",
    "from_char", "from_utf8", "to_string_lossy", "vec_macro",
}


def _unresolved_indirection(argument):
    """True when ``argument`` reaches a symbol whose value is not shown benign.

    String literals are removed and only the identifier-free constructor
    vocabulary is accepted; anything else (a function parameter, a forwarded
    variable, an opaque field) counts as indirect.
    """

    text = re.sub(r"['\"][^'\"]*['\"]", " ", argument)
    for word in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", text):
        if word.lower() not in _SAFE_ARG_IDENTIFIERS:
            return True
    return False


def scan_workspace_delegation(repo, crate_dir):
    """Cross-crate subprocess delegation via workspace-wide command bindings.

    A subprocess sink outside ``pev2-core`` is a delegation when (a) its command
    argument resolves to a workspace binding that carries a source-runtime
    identity, or (b) the argument is an *unresolved* indirection (a function
    parameter forwarded from another file, an opaque field, ...) while the
    workspace carries a source-runtime command identity.  The ``(b)`` branch
    mirrors the verifier detector's tree-level signal fallback and the FFI
    ``tree_signals`` association, so a command name split across files is not
    assumed innocent (it stays detection-only here; the verifier additionally
    fails closed).
    """

    sources = _rust_sources(repo)
    if not sources:
        return []
    bindings = _collect_workspace_bindings(sources)
    tree_tokens = []
    for _path, text in sources:
        token = _restricted_token(text)
        if token and token not in tree_tokens:
            tree_tokens.append(token)
    crate_prefix = (os.path.abspath(crate_dir) + os.sep) if crate_dir else None
    markers = []
    for path, text in sources:
        if crate_prefix and os.path.abspath(path).startswith(crate_prefix):
            continue  # already covered by the strict crate scan
        compact = re.sub(r"[^\S\n]+", "", text)
        for match in re.finditer(
            r"Command::new\s*\(|std::process::Command|process::Command", compact
        ):
            argument = _sink_argument(compact, match.start())
            token = _argument_token(argument, bindings)
            if not token and tree_tokens and _unresolved_indirection(argument):
                token = tree_tokens[0]
            if token:
                markers.append(
                    "%s:subprocess-source-runtime(%s)"
                    % (os.path.relpath(path, repo), token)
                )
                break
    return markers


def manifest_checks(repo, crate_dir, build_ok):
    """Return (results dict, details dict) for the manifest observations."""
    results = {}
    details = {}
    crate_cargo = os.path.join(crate_dir, "Cargo.toml") if crate_dir else ""
    crate_text = read_text(crate_cargo) if crate_cargo else ""
    deps = toml_dependencies(crate_text)
    dep_names = {name.lower() for name in deps}
    missing = [d for d in REQUIRED_DEPS if d.lower() not in dep_names]
    forbidden = sorted(
        name
        for name in dep_names
        if any(token in name for token in FORBIDDEN_DEP_TOKENS)
    )

    root_cargo = os.path.join(repo, "Cargo.toml")
    root_text = read_text(root_cargo)
    workspace_ok = bool(crate_dir) and (
        "pev2-core" in root_text or "crates" in root_text or os.path.isdir(crate_dir)
    )

    delegation = scan_delegation(crate_dir) if crate_dir else []
    if crate_dir:
        delegation = delegation + scan_workspace_delegation(repo, crate_dir)
    has_build_rs = bool(crate_dir) and any(
        os.path.isfile(os.path.join(crate_dir, name)) for name in ("build.rs", "build.RS")
    )

    # F2P-070: dependency declarations of the native Rust crate.
    results["F2P-070"] = workspace_ok and not missing and not forbidden
    details["F2P-070"] = "missing_deps=%s forbidden=%s" % (missing, forbidden)

    # F2P-071: native implementation, no Node/TypeScript runtime delegation.
    results["F2P-071"] = (
        not missing and not forbidden and not delegation
    )
    details["F2P-071"] = "delegation_markers=%s" % delegation

    # F2P-072: crate builds from Rust sources with no build.rs / FFI / node call.
    results["F2P-072"] = build_ok and not has_build_rs and not forbidden and not delegation
    details["F2P-072"] = "build_ok=%s build_rs=%s" % (build_ok, has_build_rs)
    return results, details


def run_probe(repo, crate_dir):
    """Compile and run the Rust probe; return (status, results, detail)."""
    if crate_dir is None:
        return "FAIL", {}, "pev2-core crate not found under %s" % repo

    env = dict(os.environ)
    env.setdefault("CARGO_NET_OFFLINE", "true")
    if os.environ.get("CARGO_HOME"):
        env["CARGO_HOME"] = os.environ["CARGO_HOME"]

    examples_dir = os.path.join(crate_dir, "examples")
    probe_path = os.path.join(examples_dir, "_f2p_probe_0003.rs")
    tmp_repo = None
    work_repo = repo
    work_crate = crate_dir
    try:
        try:
            os.makedirs(examples_dir, exist_ok=True)
            with open(probe_path, "w", encoding="utf-8") as handle:
                handle.write(PROBE_RS)
            probe_work = probe_path
        except OSError:
            # Read-only checkout: run against a private writable copy.
            tmp_repo = tempfile.mkdtemp(prefix="f2p0003-")
            copy = os.path.join(tmp_repo, "repo")
            shutil.copytree(
                repo,
                copy,
                ignore=shutil.ignore_patterns("target", "node_modules", ".git"),
            )
            work_repo = copy
            work_crate = find_crate(copy)
            if work_crate is None:
                return "FAIL", {}, "crate missing in writable copy"
            probe_work = os.path.join(work_crate, "examples", "_f2p_probe_0003.rs")
            os.makedirs(os.path.dirname(probe_work), exist_ok=True)
            with open(probe_work, "w", encoding="utf-8") as handle:
                handle.write(PROBE_RS)

        cmd = ["cargo", "run", "--offline", "--example", "_f2p_probe_0003", "--quiet"]
        if os.path.isfile(os.path.join(work_repo, "Cargo.lock")) or os.path.isfile(
            os.path.join(work_crate, "Cargo.lock")
        ):
            cmd.insert(2, "--locked")
        code, out, err = run_bounded(cmd, cwd=work_crate, env=env)
    finally:
        try:
            if tmp_repo is None and os.path.isfile(probe_path):
                os.remove(probe_path)
        except OSError:
            pass
        if tmp_repo is not None:
            shutil.rmtree(tmp_repo, ignore_errors=True)

    if code == 127:
        return "INFRA_ERROR", {}, "cargo not available"
    payload = None
    for line in out.splitlines():
        if line.startswith("F2P_PROBE_JSON:"):
            try:
                payload = json.loads(line[len("F2P_PROBE_JSON:") :])
            except ValueError:
                payload = None
    if payload is None:
        if code == 124:
            return "FAIL", {}, "probe timed out"
        return "FAIL", {}, "probe did not run (build failed): %s" % err.strip()[-400:]
    return "OK", payload, ""


def main():
    report_path = sys.argv[1] if len(sys.argv) > 1 else "/logs/result.json"
    repo = find_repo()
    crate_dir = find_crate(repo)

    status, probe_results, probe_detail = run_probe(repo, crate_dir)

    results = {}
    details = {}
    if status == "OK":
        for obs in RUNTIME_OBS:
            passed = bool(probe_results.get(obs, False))
            results[obs] = "PASS" if passed else "FAIL"
            details[obs] = "" if passed else "postcondition not satisfied"
        overall_probe = all(results[o] == "PASS" for o in RUNTIME_OBS)
    else:
        for obs in RUNTIME_OBS:
            results[obs] = "FAIL"
            details[obs] = probe_detail
        overall_probe = False

    manifest_results, manifest_details = manifest_checks(repo, crate_dir, overall_probe)
    for obs in MANIFEST_OBS:
        results[obs] = "PASS" if manifest_results.get(obs) else "FAIL"
        details[obs] = manifest_details.get(obs, "")

    if status == "INFRA_ERROR":
        overall = "INFRA_ERROR"
    else:
        overall = "PASS" if all(results[o] == "PASS" for o in ALL_OBS) else "FAIL"

    report = {
        "batch_id": BATCH_ID,
        "status": overall,
        "observations": {
            obs: {"status": results[obs], "detail": details.get(obs, "")} for obs in ALL_OBS
        },
        "results": [
            {"observation_id": obs, "status": results[obs], "detail": details.get(obs, "")}
            for obs in ALL_OBS
        ],
    }

    parent = os.path.dirname(os.path.abspath(report_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
    except OSError as exc:
        sys.stderr.write("failed to write report: %s\n" % exc)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
