#!/usr/bin/env node
"use strict";

// Target-side executable public-contract probe for BATCH-CONTRACT-0001.
// Usage: node probe.cjs [repo-root]   (defaults to /input/target)

const path = require("path");
const shared = require(
  path.join(__dirname, "..", "..", "..", "shared", "batches", "BATCH-CONTRACT-0001", "contract-snapshot.cjs"),
);

const root =
  process.argv[2] || process.env.CONTRACT_REPO_ROOT || "/input/target";
const result = shared.verify(root);
const report = {
  side: "target",
  repo_root: root,
  status: result.failing.length === 0 ? "PASS" : "FAIL",
  failing: result.failing,
  value: result.value,
};
process.stdout.write(JSON.stringify(report, null, 2) + "\n");
process.exit(result.failing.length === 0 ? 0 : 1);
