#!/usr/bin/env node
"use strict";

/*
 * BATCH-INTEGRATION-0001 target-side integration entry.
 *
 * Registered as the target harness entry (required_harness_entries.target =
 * ["harness.cjs"]). It pins the side to "target" and delegates to the shared
 * dispatcher, which merges every dependency batch (P2P, F2P, DIFFERENTIAL and
 * CONTRACT) into one migration-observations:v6 report.
 *
 * The delegated child is bounded twice: the shared dispatcher wraps each of its
 * own long-running child commands in `timeout`, and this entry passes an
 * explicit wall-clock guard to spawnSync so the harness can never wait forever.
 *
 * Usage: node harness.cjs [<report>]
 */

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const core = path.join(__dirname, "shared", "integration", "run-side.cjs");
const report = process.argv[2] || process.env.REPORT_PATH || "/logs/result.json";
const repo =
  process.env.MIGRATION_REPO_ROOT ||
  process.env.MIGRATION_REPO ||
  process.env.REPO_ROOT ||
  process.env.REPO ||
  "/workspace/repo";

const WALL_CLOCK_SEC = 5400;

function hasReport() {
  try {
    return fs.existsSync(report);
  } catch (e) {
    return false;
  }
}

const result = spawnSync(
  process.execPath,
  [core, "--side", "target", "--repo", repo, "--report", report],
  {
    stdio: "inherit",
    timeout: WALL_CLOCK_SEC * 1000,
    killSignal: "SIGKILL",
  },
);

let code = typeof result.status === "number" ? result.status : 1;
if (result.error && result.error.code === "ETIMEDOUT") {
  process.stderr.write(
    "harness.cjs: dispatcher exceeded " + WALL_CLOCK_SEC + "s wall clock\n",
  );
  code = 124;
}

process.exit(hasReport() ? code : code === 0 ? 2 : code);
