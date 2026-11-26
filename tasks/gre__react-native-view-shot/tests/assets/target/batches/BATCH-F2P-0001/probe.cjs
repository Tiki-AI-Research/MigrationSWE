#!/usr/bin/env node
"use strict";

// Target-side probe for BATCH-F2P-0001.
//
// On the migrated target (0.85.0) worktree every F2P observation MUST be
// reported PASS. The probe reuses the shared, side-agnostic evaluator so the
// exact same semantic predicate is applied on both sides.
//
// Usage: node probe.cjs [repoRoot] [reportPath]

const fs = require("fs");
const path = require("path");

const evaluate = require("../../../shared/batches/BATCH-F2P-0001/evaluate.cjs");
const SIDE = "target";
const EXPECTED = "PASS";

function main() {
  const repoRoot = path.resolve(process.argv[2] || process.env.REPO_ROOT || "/workspace/repo");
  const reportPath = process.argv[3] || process.env.REPORT_PATH || null;

  const results = evaluate.evaluateObservations(repoRoot);
  const mismatches = results.filter(function (r) {
    return r.status !== EXPECTED;
  });
  const infraErrors = results.filter(function (r) {
    return r.status === "INFRA_ERROR";
  });

  const report = {
    batch_id: "BATCH-F2P-0001",
    side: SIDE,
    repo_root: repoRoot,
    expected: EXPECTED,
    passed: mismatches.length === 0 && infraErrors.length === 0,
    observations: results,
  };

  const serialized = JSON.stringify(report, null, 2);
  if (reportPath) {
    try {
      fs.mkdirSync(path.dirname(reportPath), { recursive: true });
      fs.writeFileSync(reportPath, serialized + "\n");
    } catch (e) {
      // fall through: the report is still emitted on stdout
    }
  }
  process.stdout.write(serialized + "\n");

  if (infraErrors.length > 0) return 2;
  return mismatches.length === 0 ? 0 : 1;
}

process.exit(main());
