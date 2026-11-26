#!/usr/bin/env node
// Target-side integration entry point for BATCH-INTEGRATION-0001.
// Usage: node {assets}/harness.mjs <reportPath>
import { runSide } from "./shared/integration/run.mjs";

runSide("target", process.argv[2] ?? process.env.MIGRATION_REPORT ?? "/logs/result.json");
