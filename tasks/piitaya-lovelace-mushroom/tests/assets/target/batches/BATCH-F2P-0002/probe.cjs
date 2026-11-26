"use strict";

// Target-side F2P probe for BATCH-F2P-0002 (Zod 3.23.8 migration).
//
// Executed against a migrated working tree (default /workspace/repo). During
// authoring this same file is executed against the read-only reference target
// /input/target with:
//
//   DEPS_NODE_MODULES=/opt/migration/dependencies/node_modules \
//   node target/batches/BATCH-F2P-0002/probe.cjs /input/target /logs/result.json
//
// It exits 0 only when every F2P predicate holds (target expectation PASS).

const fs = require("fs");
const path = require("path");

const { loadSymbols } = require("../../../shared/batches/BATCH-F2P-0002/load-repo.cjs");
const { runProbes, summarise } = require("../../../shared/batches/BATCH-F2P-0002/predicates.cjs");

const manifest = require("../../../shared/batches/BATCH-F2P-0002/manifest.json");

async function main() {
  const repoRoot = process.argv[2] || process.env.REPO_ROOT || "/workspace/repo";
  const reportPath = process.argv[3] || "/logs/result.json";

  let symbols;
  try {
    symbols = await loadSymbols(repoRoot);
  } catch (err) {
    const report = {
      batch_id: manifest.batch_id,
      side: "target",
      repo_root: repoRoot,
      status: "INFRA_ERROR",
      error: String((err && err.stack) || err),
      results: [],
    };
    fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
    process.stderr.write("INFRA_ERROR: " + report.error + "\n");
    process.exit(2);
  }

  const predicates = runProbes(symbols);
  const results = manifest.observations.map((observation_id) => {
    const predicate_id = manifest.predicate_by_observation[observation_id];
    const result = predicates[predicate_id] || { pass: false, error: "missing predicate" };
    return {
      observation_id,
      predicate_id,
      status: result.pass === true ? "PASS" : "FAIL",
      evidence: result,
    };
  });

  const summary = summarise(predicates);
  const report = {
    batch_id: manifest.batch_id,
    side: "target",
    repo_root: repoRoot,
    status: summary.pass ? "PASS" : "FAIL",
    failed_predicates: summary.failed,
    predicates,
    results,
  };
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
  process.exit(0);
}

void main();
