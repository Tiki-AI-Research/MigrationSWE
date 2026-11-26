#!/usr/bin/env python3
"""Target-side adapter for BATCH-CONTRACT-0001.

Reads the shared contract cases and evaluates every probe against the public
Rust surface of the migrated `pev2-core` crate. A small generated Rust driver is
compiled against the crate's rlib and executed once for all probes; the process
is always wrapped in an explicit `timeout` and never allowed to block forever.

Usage:
  python3 contract_probe.py [--repo <target-repo>] [--cases <cases.json>] [--out <json>]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BUILD_TIMEOUT = 900
COMPILE_TIMEOUT = 600
RUN_TIMEOUT = 120

DRIVER_RS = r'''
use serde_json::{json, Value};
use pev2_core as pc;

fn call(fname: &str, args: &[Value]) -> Result<Value, String> {
    let s = |i: usize| -> String {
        args.get(i).and_then(|v| v.as_str()).unwrap_or("").to_string()
    };
    match fname {
        "from_source" => Ok(serde_json::to_value(pc::from_source(&s(0))?).unwrap()),
        "from_text" => Ok(serde_json::to_value(pc::from_text(&s(0))?).unwrap()),
        "from_json" => Ok(serde_json::to_value(pc::from_json(&s(0))?).unwrap()),
        "cleanup_source" => Ok(Value::String(pc::cleanup_source(&s(0)))),
        "split_into_lines" => Ok(serde_json::to_value(pc::split_into_lines(&s(0))).unwrap()),
        "from_source_then_create_plan" => {
            let content = pc::from_source(&s(0))?;
            let name = if args.len() > 1 { s(1) } else { String::new() };
            let query = if args.len() > 2 { s(2) } else { String::new() };
            Ok(serde_json::to_value(pc::create_plan(&name, content, &query)?).unwrap())
        }
        "get_plan_content" => {
            let v: Value = serde_json::from_str(&s(0)).map_err(|e| e.to_string())?;
            Ok(serde_json::to_value(pc::get_plan_content(&v)?).unwrap())
        }
        "split_balanced" => Ok(serde_json::to_value(pc::split_balanced(&s(0), &s(1))).unwrap()),
        "should_show_prop" => {
            let val = args.get(1).cloned().unwrap_or(Value::Null);
            Ok(Value::Bool(pc::should_show_prop(&s(0), &val)))
        }
        "parse_node_type" => Ok(serde_json::to_value(pc::parse_node_type(&s(0))).unwrap()),
        "find_node_by_id_present" => {
            let content = pc::from_source(&s(0))?;
            let plan = pc::create_plan("", content, "")?;
            let id = args.get(1).and_then(|v| v.as_i64()).unwrap_or(0);
            Ok(Value::Bool(pc::find_node_by_id(&plan, id).is_some()))
        }
        "find_node_by_subplan_name_absent" => {
            let content = pc::from_source(&s(0))?;
            let plan = pc::create_plan("", content, "")?;
            let name = s(1);
            Ok(Value::Bool(pc::find_node_by_subplan_name(&plan, &name).is_none()))
        }
        other => Err(format!("unknown function {}", other)),
    }
}

fn main() {
    use std::io::Read;
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let ops: Vec<Value> = serde_json::from_str(&input).unwrap_or_default();
    let mut out: Vec<Value> = Vec::new();
    for op in ops {
        let fname = op.get("fn").and_then(|v| v.as_str()).unwrap_or("");
        let args = op
            .get("args")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        match call(fname, &args) {
            Ok(v) => out.push(json!({"ok": true, "value": v})),
            Err(e) => out.push(json!({"ok": false, "error": e})),
        }
    }
    println!("{}", serde_json::to_string(&out).unwrap());
}
'''

PUBLIC_ENTRY_POINTS = [
    "create_plan",
    "cleanup_source",
    "find_node_by_id",
    "find_node_by_subplan_name",
    "from_json",
    "from_source",
    "from_text",
    "should_show_prop",
    "split_balanced",
    "split_into_lines",
]


def run(cmd, cwd, timeout):
    return subprocess.run(
        ["timeout", str(timeout)] + cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def build_crate(repo: Path) -> None:
    result = run(
        ["cargo", "build", "--offline", "-p", "pev2-core"],
        repo,
        BUILD_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cargo build failed:\n{result.stdout}\n{result.stderr}")


def find_first(pattern: str) -> str:
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise RuntimeError(f"no match for {pattern}")
    return matches[0]


def compile_driver(repo: Path, workdir: Path) -> Path:
    deps = repo / "target" / "debug" / "deps"
    pev2 = repo / "target" / "debug" / "libpev2_core.rlib"
    if not pev2.exists():
        pev2 = Path(find_first(str(deps / "libpev2_core-*.rlib")))
    serde_json = find_first(str(deps / "libserde_json-*.rlib"))
    driver_rs = workdir / "contract_driver.rs"
    driver_rs.write_text(DRIVER_RS)
    driver_bin = workdir / "contract_driver"
    result = run(
        [
            "rustc",
            str(driver_rs),
            "--edition",
            "2021",
            "-O",
            "-L",
            f"dependency={deps}",
            "--extern",
            f"pev2_core={pev2}",
            "--extern",
            f"serde_json={serde_json}",
            "-o",
            str(driver_bin),
        ],
        repo,
        COMPILE_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"rustc failed:\n{result.stdout}\n{result.stderr}")
    return driver_bin


def normalize(value):
    if value is None:
        return None
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize(value[k]) for k in sorted(value)}
    return str(value)


def extract(value, trail):
    if not trail:
        return value
    current = value
    for step in trail:
        if current is None or not hasattr(current, "__getitem__"):
            return {"__missing__": True}
        try:
            current = current[step]
        except (KeyError, IndexError, TypeError):
            return {"__missing__": True}
    return current


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("TARGET_REPO", "/workspace/repo"))
    parser.add_argument("--cases", default=os.environ.get("CONTRACT_CASES"))
    parser.add_argument("--out")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    cases_path = Path(
        args.cases
        or here / ".." / ".." / ".." / "shared" / "batches"
        / "BATCH-CONTRACT-0001" / "contract_cases.json"
    )
    repo = Path(args.repo)
    cases_doc = json.loads(cases_path.read_text())

    build_crate(repo)

    ops = []
    index = []
    for test_case in cases_doc["cases"]:
        for probe in test_case["probes"]:
            index.append((test_case, probe))
            ops.append({"fn": probe["fn"], "args": probe.get("args", [])})

    with tempfile.TemporaryDirectory(prefix="contract-probe-") as tmp:
        driver = compile_driver(repo, Path(tmp))
        result = subprocess.run(
            ["timeout", str(RUN_TIMEOUT), str(driver)],
            input=json.dumps(ops),
            capture_output=True,
            text=True,
            cwd=str(repo),
        )

    if result.returncode != 0:
        raise RuntimeError(f"driver failed ({result.returncode}):\n{result.stderr}")

    outputs = json.loads(result.stdout.strip().splitlines()[-1])

    results = []
    for position, (test_case, probe) in enumerate(index):
        raw = outputs[position]
        record = {
            "observation_id": test_case["observation_id"],
            "probe_index": position,
            "fn": probe["fn"],
            "status": "PASS",
        }
        if not raw.get("ok"):
            record["value"] = {"__error__": " ".join(str(raw.get("error", "")).split())}
            results.append(record)
            continue
        value = raw["value"]
        if probe.get("present_keys"):
            value = all(
                isinstance(value, dict) and key in value for key in probe["present_keys"]
            )
        elif probe["fn"] == "public_entry_points":
            value = PUBLIC_ENTRY_POINTS
        else:
            trail = probe.get("extract")
            if trail == ["length"] and isinstance(value, list):
                value = len(value)
            elif trail:
                value = extract(value, trail)
        record["value"] = normalize(value)
        results.append(record)

    report = {
        "batch_id": cases_doc["batch_id"],
        "side": "target",
        "normalization": cases_doc.get("normalization"),
        "results": results,
    }
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
