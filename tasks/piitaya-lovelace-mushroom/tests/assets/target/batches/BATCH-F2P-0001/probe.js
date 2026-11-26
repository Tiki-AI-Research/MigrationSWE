"use strict";

/*
 * BATCH-F2P-0001 target-side probe driver.
 *
 * Runs the exact same shared semantic predicates used by the source driver
 * against a Zod 3.23.8 repository (the migration target). The predicates
 * describe the target postconditions, so each F2P observation is expected to
 * PASS here. The status is the raw observation of this side, never a
 * hardcoded value.
 *
 * CLI: node probe.js [repoRoot] [reportPath]
 * API: module.exports.run(repoRoot) -> { side, results }
 */

const path = require("path");
const fs = require("fs");
const core = require("../../../shared/batches/BATCH-F2P-0001/probe-core");

const SIDE = "target";

function run(repoRoot) {
  const report = core.runAll(repoRoot);
  report.side = SIDE;
  return report;
}

if (require.main === module) {
  const repoRoot = process.argv[2] || process.env.PROBE_REPO || "/workspace/repo";
  const reportPath = process.argv[3] || process.env.PROBE_REPORT;
  const report = run(repoRoot);
  for (const r of report.results) {
    process.stdout.write(`${SIDE} ${r.observation_id} ${r.status} ${r.detail}\n`);
  }
  if (reportPath) {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
  }
}

module.exports = { run };
