#!/usr/bin/env node
"use strict";

// Target-side entry point for the BATCH-F2P-0003 probes.
// The target probe must run against the reference target checkout.
// Usage: node run.cjs [repoRoot] [reportPath]

const path = require("path");
const {spawnSync} = require("child_process");

const runner = path.resolve(__dirname, "../../../shared/batches/BATCH-F2P-0003/run.cjs");
const repoRoot = process.argv[2] || "/input/target";
const reportPath = process.argv[3] || path.join("/logs", "BATCH-F2P-0003-target.json");

// Explicit bounded execution: never allow the harness to wait forever.
const result = spawnSync(
  process.execPath,
  [runner, "target", repoRoot, reportPath],
  {stdio: "inherit", timeout: 60000, killSignal: "SIGKILL"},
);

if (result.error || result.status !== 0) {
  process.stderr.write(
    "target probe failed to complete: " + String((result.error && result.error.message) || result.status) + "\n",
  );
  process.exit(result.status === null ? 124 : result.status || 1);
}
process.exit(0);
