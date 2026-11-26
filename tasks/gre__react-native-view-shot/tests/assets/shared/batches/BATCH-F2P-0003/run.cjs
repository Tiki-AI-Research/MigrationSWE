"use strict";

// Bounded CLI for the BATCH-F2P-0003 F2P probes.
//
// Usage: node run.cjs <side> <repoRoot> <reportPath>
// The probe evaluates only instantaneous filesystem/dependency projections, so
// no long-running child process is spawned. A hard watchdog still bounds the
// whole run so the harness can never wait forever.

const fs = require("fs");
const path = require("path");

const lib = require("./probe_lib.cjs");

const WATCHDOG_MS = 30000;

const side = process.argv[2] || "target";
const repoRoot = process.argv[3] || (side === "source" ? "/input/source" : "/input/target");
const reportPath = process.argv[4] || path.join("/logs", "BATCH-F2P-0003-" + side + ".json");

const watchdog = setTimeout(function () {
  process.stderr.write("BATCH-F2P-0003 probe watchdog expired\n");
  process.exit(124);
}, WATCHDOG_MS);
if (watchdog.unref) watchdog.unref();

let results = [];
let error = null;
try {
  results = lib.runAll(repoRoot);
} catch (e) {
  error = String((e && e.message) || e);
}

const report = {
  schema_version: "migration-asset-fragment:v2",
  batch_id: "BATCH-F2P-0003",
  side: side,
  repo_root: repoRoot,
  resolved_react_native: lib.resolvedRnVersion(repoRoot),
  declared_react_native_range: lib.declaredRnRange(repoRoot),
  results: results,
};

try {
  fs.mkdirSync(path.dirname(reportPath), {recursive: true});
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2) + "\n");
} catch (e) {
  process.stderr.write("failed to write report: " + String((e && e.message) || e) + "\n");
  process.exit(2);
}

clearTimeout(watchdog);

if (error) {
  process.stderr.write("probe error: " + error + "\n");
  process.exit(1);
}

process.stdout.write(JSON.stringify(report) + "\n");
process.exit(0);
