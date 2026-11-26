"use strict";

/*
 * BATCH-INTEGRATION-0001 target harness entry.
 *
 * Invoked as: node {assets}/target/runner.js {report}
 *
 * Runs every required runtime observation against the repository the target
 * environment mounts (the migrated Zod 3.23.8 working tree) and writes the
 * migration-observations:v6 report. All logic lives in the shared
 * integration core so the source and target sides evaluate the exact same
 * predicates; only the repository under test differs.
 */

const path = require("path");
const { main } = require(
  path.join(__dirname, "..", "shared", "integration", "runner-core.cjs")
);

main("target", process.argv[2] || "/logs/result.json");
