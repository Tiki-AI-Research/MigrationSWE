//! Differential probe for BATCH-DIFFERENTIAL-0001.
//!
//! Exercises the public pev2-core surface for the 32 shared-corpus
//! observations and prints `DIFF-NNN PASS` / `DIFF-NNN FAIL <detail>` lines.
//! The test itself never panics so that the enclosing `cargo test` run stays
//! green; the harness parses the emitted lines.

use pev2_core::{
    cleanup_source, create_plan, find_node_by_id, find_node_by_subplan_name, from_json, from_source,
    from_text, get_plan_content, parse_node_type, should_show_prop, split_balanced,
    split_into_lines,
};
use serde_json::{json, Value};

fn to_val<T: serde::Serialize>(v: &T) -> Value {
    serde_json::to_value(v).unwrap()
}

fn need(cond: bool, msg: impl Into<String>) -> Result<(), String> {
    if cond {
        Ok(())
    } else {
        Err(msg.into())
    }
}

fn f(v: &Value, k: &str) -> Option<f64> {
    v.get(k).and_then(|x| x.as_f64())
}
fn s<'a>(v: &'a Value, k: &str) -> Option<&'a str> {
    v.get(k).and_then(|x| x.as_str())
}
fn b(v: &Value, k: &str) -> Option<bool> {
    v.get(k).and_then(|x| x.as_bool())
}
fn i64v(v: &Value, k: &str) -> Option<i64> {
    v.get(k).and_then(|x| x.as_i64())
}
fn close(a: Option<f64>, e: f64) -> bool {
    a.map(|x| (x - e).abs() < 1e-6).unwrap_or(false)
}

const SEQ: &str = "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)";
const ANALYZED: &str =
    "Seq Scan on foo  (cost=0.00..1.00 rows=10 width=4) (actual time=0.1..0.2 rows=100 loops=1)";
const DQ_JSON: &str = "\"[\n  {\n    \"\"Plan\"\": {\n      \"\"Node Type\"\": \"\"Seq Scan\"\",\n      \"\"Plan Rows\"\": 5\n    }\n  }\n]\"";
const NESTED_LOOP: &str = "\
 Nested Loop  (cost=0.00..611.27 rows=13400 width=8) (actual time=0.025..10.198 rows=34956 loops=1)
   Join Filter: (t1.c1 > t2.c2)
   Rows Removed by Join Filter: 5244
   ->  Seq Scan on t1  (cost=0.00..6.02 rows=402 width=4) (actual time=0.013..0.058 rows=402 loops=1)
   ->  Materialize  (cost=0.00..2.50 rows=100 width=4) (actual time=0.000..0.008 rows=100 loops=402)
         ->  Seq Scan on t2  (cost=0.00..2.00 rows=100 width=4) (actual time=0.005..0.015 rows=100 loops=1)
 Planning Time: 0.083 ms
 Execution Time: 12.035 ms
";
const CTE_TEXT: &str = "\
 Aggregate  (cost=38.51..38.52 rows=1 width=4) (actual time=0.100..0.200 rows=1 loops=1)
   CTE e
     ->  Seq Scan on employes  (cost=0.00..38.25 rows=11 width=8) (actual time=0.010..0.100 rows=11 loops=1)
   ->  CTE Scan on e  (cost=0.00..0.25 rows=4 width=4) (actual time=0.050..0.150 rows=4 loops=1)
";
const FRACTIONAL: &str = "\
 Nested Loop  (cost=0.00..12.97 rows=4 width=16) (actual time=0.017..0.025 rows=1.00 loops=1)
   ->  Function Scan on unnest w  (cost=0.00..0.04 rows=4 width=12) (actual time=0.006..0.007 rows=4.00 loops=1)
   ->  Limit  (cost=0.00..3.22 rows=1 width=4) (actual time=0.003..0.004 rows=0.25 loops=4)
         ->  Seq Scan on data  (cost=0.00..41.88 rows=13 width=4) (actual time=0.003..0.003 rows=0.25 loops=4)
";
const RICH_JSON: &str = r#"
[
  { "Plan": {
      "Node Type": "Gather",
      "Startup Cost": 1.0, "Total Cost": 2.0, "Plan Rows": 3, "Plan Width": 4,
      "Actual Startup Time": 0.1, "Actual Total Time": 0.2, "Actual Rows": 3, "Actual Loops": 1,
      "Workers Planned": 2, "Workers Launched": 2,
      "Plans": [
        { "Node Type": "Seq Scan", "Parent Relationship": "Outer",
          "Startup Cost": 0.0, "Total Cost": 1.0, "Plan Rows": 1, "Plan Width": 4,
          "Actual Startup Time": 0.0, "Actual Total Time": 0.1, "Actual Rows": 1, "Actual Loops": 1, "Output": ["a"] }
      ] },
    "JIT": { "Functions": 1, "Options": { "Inlining": true } },
    "Planning": { "Shared Hit Blocks": 5, "Shared Read Blocks": 0 },
    "Serialization": { "Time": 1.5, "Output Volume": 10 },
    "Triggers": [ { "Trigger Name": "t1", "Time": 0.1, "Calls": 1 } ],
    "Query Text": "SELECT 1" }
]
"#;

fn run<F: FnOnce() -> Result<(), String>>(id: &str, f: F) {
    let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(f));
    match res {
        Ok(Ok(())) => println!("{id} PASS"),
        Ok(Err(e)) => println!("{id} FAIL {e}"),
        Err(_) => println!("{id} FAIL panic"),
    }
}

#[test]
fn differential() {
    run("DIFF-001", || {
        let content = from_source(ANALYZED).map_err(|e| e)?;
        let plan = create_plan("", content, "").map_err(|e| e)?;
        let pj = to_val(&plan);
        let c = pj.get("content").ok_or("no content")?;
        for k in [
            "maxRows",
            "maxCost",
            "maxTotalCost",
            "maxDuration",
            "maxEstimateFactor",
            "maxIo",
            "maxBlocks",
        ] {
            need(c.get(k).is_some(), format!("missing metric {k}"))?;
        }
        Ok(())
    });

    run("DIFF-002", || {
        let content = from_source(SEQ)?;
        let _ = from_text(SEQ)?;
        let _ = from_json(DQ_JSON)?;
        let _ = cleanup_source(SEQ);
        let _ = split_into_lines(SEQ);
        let _ = split_balanced("a,b", ",");
        let plan = create_plan("", content, "")?;
        let _ = find_node_by_id(&plan, 1);
        let _ = find_node_by_subplan_name(&plan, "x");
        let _ = should_show_prop("Filter", &json!("x"));
        Ok(())
    });

    run("DIFF-003", || {
        let content = from_source(SEQ)?;
        let pj = to_val(&content);
        need(
            s(pj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Seq Scan"),
            "root Node Type",
        )?;
        need(from_source("not a plan").is_err(), "bad text must error")?;
        Ok(())
    });

    run("DIFF-004", || {
        let content = from_source(SEQ)?;
        let plan = create_plan("", content, "SELECT  a     b")?;
        let pj = to_val(&plan);
        need(s(&pj, "query") == Some("SELECT a b"), "query normalized")?;
        let c = pj.get("content").ok_or("no content")?;
        need(c.get("maxTotalCost").is_some(), "maxTotalCost")?;
        need(c.get("maxCost").is_some(), "maxCost")?;
        Ok(())
    });

    run("DIFF-005", || {
        let content = from_text(SEQ)?;
        let pj = to_val(&content);
        need(
            s(pj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Seq Scan"),
            "root Node Type",
        )?;
        Ok(())
    });

    run("DIFF-006", || {
        let content = from_json(DQ_JSON)?;
        let pj = to_val(&content);
        need(
            s(pj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Seq Scan"),
            "embedded JSON node",
        )?;
        Ok(())
    });

    run("DIFF-007", || {
        let cleaned = cleanup_source("\u{2502}  Seq Scan on t  \u{2502}\nQUERY PLAN\n(1 row)\n");
        need(!cleaned.contains("QUERY PLAN"), "QUERY PLAN header kept")?;
        need(!cleaned.contains("(1 row)"), "row count kept")?;
        need(!cleaned.contains('\u{2502}'), "frame char kept")?;
        need(cleaned.contains("Seq Scan on t"), "payload dropped")?;
        let lines = split_into_lines(
            "Nested Loop  (cost=0.00..1.00 rows=1 width=4)\n   Join Filter: (a = 1\n     AND b = 2)\n",
        );
        let unbalanced = lines.iter().any(|l| {
            let mut d = 0i32;
            for ch in l.chars() {
                match ch {
                    '(' => d += 1,
                    ')' => d -= 1,
                    _ => {}
                }
            }
            d != 0
        });
        need(!unbalanced, "unbalanced parentheses after split")?;
        Ok(())
    });

    run("DIFF-008", || {
        let content = from_source(ANALYZED)?;
        let pj = to_val(&content);
        let root = pj.get("Plan").ok_or("no plan")?;
        for k in ["Actual Loops", "Actual Rows", "Total Cost", "Plan Rows"] {
            need(root.get(k).is_some(), format!("missing {k}"))?;
        }
        Ok(())
    });

    run("DIFF-009", || {
        let node = to_val(&parse_node_type("Index Scan using idx on t"));
        need(s(&node, "Node Type") == Some("Index Scan"), "node type")?;
        need(s(&node, "Index Name") == Some("idx"), "index name")?;
        need(s(&node, "Relation Name") == Some("t"), "relation name")?;
        need(s(&node, "Scan Direction") == Some("Forward"), "scan direction")?;
        Ok(())
    });

    run("DIFF-010", || {
        let got = split_balanced("a,(b,c),d", ",");
        need(
            got == vec!["a".to_string(), "(b,c)".to_string(), "d".to_string()],
            format!("got {:?}", got),
        )?;
        Ok(())
    });

    run("DIFF-011", || {
        let content = from_source(CTE_TEXT)?;
        let plan = create_plan("", content, "")?;
        need(find_node_by_id(&plan, 1).is_some(), "find_node_by_id")?;
        need(
            find_node_by_subplan_name(&plan, "e").is_some(),
            "find_node_by_subplan_name",
        )?;
        need(
            should_show_prop("Filter", &json!("(a = 1)")),
            "should_show_prop true",
        )?;
        need(
            !should_show_prop("Filter", &json!("")),
            "should_show_prop empty",
        )?;
        need(
            !should_show_prop("Node Type", &json!("Seq Scan")),
            "should_show_prop node type",
        )?;
        need(
            !should_show_prop("Actual Rows", &json!(0)),
            "should_show_prop zero rows",
        )?;
        Ok(())
    });

    run("DIFF-012", || {
        let content = from_source(RICH_JSON)?;
        let pj = to_val(&content);
        let root = pj.get("Plan").ok_or("no plan")?;
        need(root.get("Plans").and_then(|v| v.as_array()).is_some(), "Plans")?;
        for k in ["JIT", "Planning", "Serialization", "Triggers", "Query Text"] {
            need(pj.get(k).is_some(), format!("missing {k}"))?;
        }
        need(
            root.get("Workers Planned").is_some(),
            "Workers Planned key",
        )?;
        Ok(())
    });

    run("DIFF-013", || {
        let pure = from_source("{\"Plan\": {\"Node Type\": \"Seq Scan\"}}")?;
        let pj = to_val(&pure);
        need(
            s(pj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Seq Scan"),
            "pure JSON",
        )?;
        let framed = from_source(DQ_JSON)?;
        let fj = to_val(&framed);
        need(
            s(fj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Seq Scan"),
            "framed JSON",
        )?;
        let text = from_source(SEQ)?;
        let tj = to_val(&text);
        need(
            s(tj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Seq Scan"),
            "plain text",
        )?;
        Ok(())
    });

    run("DIFF-014", || {
        let cleaned = cleanup_source("\u{2502}  Seq Scan on t  \u{2502}\nQUERY PLAN\n(1 row)\n");
        need(!cleaned.contains("QUERY PLAN"), "QUERY PLAN")?;
        need(!cleaned.contains("(1 row)"), "row count")?;
        need(!cleaned.contains('\u{2502}'), "frame")?;
        let quoted = cleanup_source("\"Seq Scan on t\"\n");
        need(!quoted.contains('"'), "wrapping quotes")?;
        let plus = cleanup_source("Seq Scan on t +\n");
        need(!plus.contains('+'), "plus continuation")?;
        Ok(())
    });

    run("DIFF-015", || {
        let lines = split_into_lines(
            "Nested Loop  (cost=0.00..1.00 rows=1 width=4)\n   Join Filter: (a = 1\n     AND b = 2)\n",
        );
        let unbalanced = lines.iter().any(|l| {
            let mut d = 0i32;
            for ch in l.chars() {
                match ch {
                    '(' => d += 1,
                    ')' => d -= 1,
                    _ => {}
                }
            }
            d != 0
        });
        need(!unbalanced, "unbalanced parentheses")?;
        need(lines.len() >= 2, "fewer lines than expected")?;
        Ok(())
    });

    run("DIFF-016", || {
        let content = from_text(NESTED_LOOP)?;
        let pj = to_val(&content);
        let root = pj.get("Plan").ok_or("no plan")?;
        let plans = root.get("Plans").and_then(|v| v.as_array()).ok_or("no Plans")?;
        need(plans.len() == 2, format!("expected 2 children, got {}", plans.len()))?;
        need(
            s(&plans[0], "Node Type") == Some("Seq Scan"),
            "first child Node Type",
        )?;
        Ok(())
    });

    run("DIFF-017", || {
        let never = from_text("Hash  (cost=6.12..6.12 rows=212 width=1346) (never executed)")?;
        let nj = to_val(&never);
        need(
            i64v(nj.get("Plan").ok_or("no plan")?, "Actual Loops") == Some(0),
            "never executed loops",
        )?;
        let cte = from_source(CTE_TEXT)?;
        let cj = to_val(&cte);
        need(
            cj.get("Plan")
                .and_then(|p| p.get("Plans"))
                .and_then(|v| v.as_array())
                .is_some(),
            "CTE plan children",
        )?;
        let rich = from_source(RICH_JSON)?;
        let rj = to_val(&rich);
        for k in ["JIT", "Planning", "Serialization"] {
            need(rj.get(k).is_some(), format!("missing {k}"))?;
        }
        let buf = from_source(
            "Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)\n  Buffers: shared hit=1 read=2\n  I/O Timings: read=1.0 write=2.0\n",
        )?;
        let bj = to_val(&buf);
        let broot = bj.get("Plan").ok_or("no plan")?;
        need(broot.get("Shared Hit Blocks").is_some(), "buffers")?;
        Ok(())
    });

    run("DIFF-018", || {
        let content = from_text(NESTED_LOOP)?;
        let pj = to_val(&content);
        let root = pj.get("Plan").ok_or("no plan")?;
        let plans = root.get("Plans").and_then(|v| v.as_array()).ok_or("no Plans")?;
        need(
            s(&plans[0], "Parent Relationship") == Some("Outer"),
            "outer",
        )?;
        need(
            s(&plans[1], "Parent Relationship") == Some("Inner"),
            "inner",
        )?;
        let append = from_text(
            "Append  (cost=0.00..1.00 rows=2 width=4)\n  ->  Seq Scan on a  (cost=0.00..0.50 rows=1 width=4)\n  ->  Seq Scan on b  (cost=0.00..0.50 rows=1 width=4)\n",
        )?;
        let aj = to_val(&append);
        let aroot = aj.get("Plan").ok_or("no plan")?;
        let aps = aroot.get("Plans").and_then(|v| v.as_array()).ok_or("no Plans")?;
        need(s(&aps[0], "Parent Relationship") == Some("Member"), "member")?;
        let sub = from_text(
            "Subquery Scan on foo  (cost=0.00..2.27 rows=1 width=265)\n   ->  Limit  (cost=0.00..1.77 rows=40 width=265)\n         ->  Seq Scan on pg_class  (cost=0.00..18.10 rows=410 width=265)\n",
        )?;
        let sj = to_val(&sub);
        let sroot = sj.get("Plan").ok_or("no plan")?;
        let sps = sroot.get("Plans").and_then(|v| v.as_array()).ok_or("no Plans")?;
        need(s(&sps[0], "Parent Relationship") == Some("Subquery"), "subquery")?;
        Ok(())
    });

    run("DIFF-019", || {
        let content = from_text("Hash  (cost=6.12..6.12 rows=212 width=1346) (never executed)")?;
        let pj = to_val(&content);
        let root = pj.get("Plan").ok_or("no plan")?;
        for k in ["Actual Loops", "Actual Rows", "Actual Total Time", "Actual Startup Time"] {
            need(i64v(root, k) == Some(0), format!("{k} should be 0"))?;
        }
        Ok(())
    });

    run("DIFF-020", || {
        let content = from_source(FRACTIONAL)?;
        let pj = to_val(&content);
        let root = pj.get("Plan").ok_or("no plan")?;
        let seq = root
            .get("Plans")
            .and_then(|p| p.as_array())
            .and_then(|p| p.get(1))
            .and_then(|p| p.get("Plans"))
            .and_then(|p| p.as_array())
            .and_then(|p| p.get(0))
            .ok_or("no nested seq scan")?;
        need(close(f(seq, "Actual Rows"), 0.25), "Actual Rows 0.25")?;
        need(
            b(seq, "*Actual Rows Is Fractional") == Some(true),
            "fractional marker",
        )?;
        Ok(())
    });

    run("DIFF-021", || {
        need(
            from_text("not a plan").unwrap_err() == "Unable to parse plan",
            "error message",
        )?;
        Ok(())
    });

    run("DIFF-022", || {
        let content = from_json(DQ_JSON)?;
        let pj = to_val(&content);
        need(
            s(pj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Seq Scan"),
            "sliced JSON",
        )?;
        Ok(())
    });

    run("DIFF-023", || {
        let v = json!({ "foo": 1 });
        let arr = Value::Array(vec![json!({ "Plan": { "Node Type": "Seq Scan" } })]);
        let _ = v;
        let content = get_plan_content(&arr)?;
        let cj = to_val(&content);
        need(
            s(cj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Seq Scan"),
            "array first element",
        )?;
        Ok(())
    });

    run("DIFF-024", || {
        need(
            get_plan_content(&json!({ "foo": 1 })).unwrap_err() == "Invalid plan",
            "invalid plan error",
        )?;
        Ok(())
    });

    run("DIFF-025", || {
        let content = from_source(RICH_JSON)?;
        let plan = create_plan("", content, "")?;
        let pj = to_val(&plan);
        need(b(&pj, "isAnalyze") == Some(true), "isAnalyze true")?;
        need(b(&pj, "isVerbose") == Some(true), "isVerbose true")?;
        let plain = from_source(SEQ)?;
        let plan2 = create_plan("", plain, "")?;
        let pj2 = to_val(&plan2);
        // Text parsing constructs Node class instances. Under the source
        // tsconfig's useDefineForClassFields, Actual Rows is an own property
        // even when its value is undefined, so _.has returns true.
        need(b(&pj2, "isAnalyze") == Some(true), "text Node has Actual Rows")?;
        // JSON input has no class fields: absence still means false.
        let json_plain = get_plan_content(&json!({"Plan": {"Node Type": "Seq Scan"}}))?;
        let json_plan = to_val(&create_plan("", json_plain, "")?);
        need(b(&json_plan, "isAnalyze") == Some(false), "JSON object lacks Actual Rows")?;

        // A text Node has own undefined class fields; its JSON serialization
        // does not. Equal serialized content must never let one input borrow
        // another input's hidden fields. Keep both inputs alive and interleave
        // all three public JSON/text construction paths before consuming them.
        let text_content = from_text(SEQ)?;
        let serialized = to_val(&text_content);
        need(
            serialized["Plan"].get("Actual Rows").is_none(),
            "undefined text class fields must be omitted from JSON",
        )?;
        let object_content = get_plan_content(&serialized)?;
        let object_plan = to_val(&create_plan("", object_content, "")?);
        need(
            b(&object_plan, "isAnalyze") == Some(false),
            "get_plan_content must not inherit a previous text Node's fields",
        )?;
        let json_source = serde_json::to_string_pretty(&vec![&serialized])
            .map_err(|e| e.to_string())?;
        let json_content = from_json(&json_source)?;
        let text_plan = to_val(&create_plan("", text_content, "")?);
        need(
            b(&text_plan, "isAnalyze") == Some(true),
            "parsing identical JSON must not change an existing text Node",
        )?;
        let json_plan = to_val(&create_plan("", json_content, "")?);
        need(
            b(&json_plan, "isAnalyze") == Some(false),
            "JSON content must retain its own fields after text create_plan",
        )?;

        // A retained parse result remains usable after unrelated parses. This
        // exercises ordinary library use without depending on any cache size.
        let retained = from_text(SEQ)?;
        for index in 0..96 {
            let source = format!(
                "Seq Scan on unrelated_{index}  (cost=0.00..2.00 rows=2 width=8)"
            );
            let _ = from_text(&source)?;
        }
        let retained_plan = to_val(&create_plan("", retained, "")?);
        need(
            b(&retained_plan, "isAnalyze") == Some(true),
            "unrelated parsing must not change a retained text Node",
        )?;

        // PlanContent is a public mutable map. Editing unrelated global or
        // node fields must not discard the own undefined fields of a text
        // Node, and caller-provided values must not be overwritten.
        for edit_node in [false, true] {
            let mut edited = from_text(SEQ)?;
            if edit_node {
                let root = edited.get_mut("Plan").and_then(Value::as_object_mut)
                    .ok_or("missing mutable text root")?;
                root.insert("Relation Name".into(), json!("renamed"));
                root.insert("Plan Rows".into(), json!(9));
            } else {
                edited.insert("Query Text".into(), json!("select * from t"));
            }
            let edited_plan = to_val(&create_plan("", edited, "")?);
            need(b(&edited_plan, "isAnalyze") == Some(true),
                "editing parsed content must retain text Node own fields")?;
            if edit_node {
                need(edited_plan["content"]["Plan"]["Relation Name"] == json!("renamed"),
                    "node edits must be applied")?;
                need(edited_plan["content"]["Plan"]["Plan Rows"] == json!(9),
                    "numeric node edits must be applied")?;
            } else {
                need(edited_plan["content"]["Query Text"] == json!("select * from t"),
                    "global edits must be applied")?;
            }
        }
        let mut replaced = from_text(SEQ)?;
        let plain_node = replaced.get("Plan").ok_or("no text root")?.clone();
        replaced.insert("Plan".into(), plain_node);
        need(b(&to_val(&create_plan("", replaced, "")?), "isAnalyze") == Some(false),
            "a replacement JSON node must not inherit text Node fields")?;

        let mut removed = from_text(ANALYZED)?;
        removed.get_mut("Plan").and_then(Value::as_object_mut)
            .ok_or("no analyzed text root")?.remove("Actual Rows");
        need(b(&to_val(&create_plan("", removed, "")?), "isAnalyze") == Some(false),
            "explicitly removed fields must stay removed")?;
        Ok(())
    });

    run("DIFF-026", || {
        let content = from_source(CTE_TEXT)?;
        let plan = create_plan("", content, "")?;
        let pj = to_val(&plan);
        let ctes = pj.get("ctes").and_then(|v| v.as_array()).cloned().unwrap_or_default();
        need(ctes.len() == 1, format!("expected 1 cte, got {}", ctes.len()))?;
        need(find_node_by_id(&plan, 1).is_some(), "node id 1")?;
        need(find_node_by_id(&plan, 3).is_some(), "node id 3")?;
        Ok(())
    });

    run("DIFF-027", || {
        let content = from_source(ANALYZED)?;
        let plan = create_plan("", content, "")?;
        let pj = to_val(&plan);
        let c = pj.get("content").ok_or("no content")?;
        need(close(f(c, "maxTotalCost"), 1.0), "maxTotalCost")?;
        need(close(f(c, "maxCost"), 1.0), "maxCost")?;
        need(close(f(c, "maxRows"), 100.0), "maxRows")?;
        need(close(f(c, "maxDuration"), 0.2), "maxDuration")?;
        Ok(())
    });

    run("DIFF-028", || {
        let source = "\
     Update on a  (cost=0.00..266690.72 rows=0 width=0) (actual time=10.00..11.00 rows=0 loops=1)
         Buffers: shared hit=2 read=4 dirtied=6 written=8
         I/O Timings: read=2 write=4
         ->  Seq Scan on a  (cost=0.00..0.00 rows=10 width=1) (actual time=9.00..10.00 rows=10000000 loops=1)
               Buffers: shared hit=1 read=2 written=4
               I/O Timings: read=1 write=2
";
        let content = from_source(source)?;
        let plan = create_plan("", content, "")?;
        let pj = to_val(&plan);
        let c = pj.get("content").ok_or("no content")?;
        need(close(f(c, "maxIo"), 3.0), "maxIo")?;
        let blocks = c.get("maxBlocks").ok_or("no maxBlocks")?;
        need(close(f(blocks, "Shared"), 13.0), "maxBlocks.Shared")?;
        Ok(())
    });

    run("DIFF-029", || {
        let content = from_source(ANALYZED)?;
        let plan = create_plan("", content, "")?;
        let c = to_val(&plan);
        let cc = c.get("content").ok_or("no content")?;
        need(close(f(cc, "maxEstimateFactor"), 20.0), "analyzed factor")?;
        let content2 = from_source(SEQ)?;
        let plan2 = create_plan("", content2, "")?;
        let c2 = to_val(&plan2);
        let cc2 = c2.get("content").ok_or("no content")?;
        need(close(f(cc2, "maxEstimateFactor"), 1.0), "default factor")?;
        Ok(())
    });

    run("DIFF-030", || {
        let content = from_source(NESTED_LOOP)?;
        let plan = create_plan("", content, "")?;
        let pj = to_val(&plan);
        let root = pj
            .get("content")
            .and_then(|c| c.get("Plan"))
            .ok_or("no root")?;
        let plans = root.get("Plans").and_then(|v| v.as_array()).ok_or("no plans")?;
        let m = 0.008 * 402.0 - 0.015;
        need(
            close(f(&plans[1], "*Duration (exclusive)"), m),
            "materialize exclusive duration",
        )?;
        let nl = 10.198 - (m + 0.015) - 0.058;
        need(
            close(f(root, "*Duration (exclusive)"), nl),
            "root exclusive duration",
        )?;
        need(
            root.get("*Actual Rows Revised").is_some(),
            "revised field",
        )?;
        Ok(())
    });

    run("DIFF-031", || {
        let content = from_source(CTE_TEXT)?;
        let plan = create_plan("", content, "")?;
        let pj = to_val(&plan);
        let ctes = pj.get("ctes").and_then(|v| v.as_array()).cloned().unwrap_or_default();
        need(!ctes.is_empty(), "cte present")?;
        let root = pj.get("content").and_then(|c| c.get("Plan")).ok_or("no root")?;
        let plans = root.get("Plans").and_then(|v| v.as_array()).ok_or("no plans")?;
        let cte_scan = plans
            .iter()
            .find(|n| s(n, "Node Type") == Some("CTE Scan"))
            .ok_or("no CTE Scan")?;
        need(
            cte_scan.get("*Duration (exclusive)").is_some(),
            "CTE Scan exclusive duration",
        )?;
        let plain_text = CTE_TEXT
            .replace(" (actual time=0.010..0.100 rows=11 loops=1)", "")
            .replace(" (actual time=0.050..0.150 rows=4 loops=1)", "")
            .replace(" (actual time=0.100..0.200 rows=1 loops=1)", "");
        let plain = create_plan("", from_source(&plain_text)?, "")?;
        let plain_pj = to_val(&plain);
        need(b(&plain_pj, "isAnalyze") == Some(true), "text CTE Node has Actual Rows")?;
        Ok(())
    });

    run("DIFF-032", || {
        let text = from_source(
            "Insert on foo  (cost=0.00..0.01 rows=1 width=24) (actual time=0.504..0.505 rows=0 loops=1)\n  ->  Result  (cost=0.00..0.01 rows=1 width=24) (actual time=0.019..0.020 rows=1 loops=1)\n",
        )?;
        let tj = to_val(&text);
        need(
            s(tj.get("Plan").ok_or("no plan")?, "Node Type") == Some("Insert"),
            "text Insert",
        )?;
        let content = from_source(
            "[ { \"Plan\": { \"Node Type\": \"ModifyTable\", \"Operation\": \"Insert\", \"Relation Name\": \"foo\", \"Alias\": \"foo\", \"Startup Cost\": 0.00, \"Total Cost\": 0.01, \"Plan Rows\": 1, \"Plan Width\": 24, \"Actual Startup Time\": 0.568, \"Actual Total Time\": 0.568, \"Actual Rows\": 0, \"Actual Loops\": 1, \"Plans\": [ { \"Node Type\": \"Result\", \"Parent Relationship\": \"Member\" } ] } } ]",
        )?;
        let content = create_plan("", content, "")?;
        let cj = to_val(&content);
        let root = cj.get("content").and_then(|c| c.get("Plan")).ok_or("no root")?;
        need(s(root, "Node Type") == Some("Insert"), "json Insert")?;
        Ok(())
    });
}
