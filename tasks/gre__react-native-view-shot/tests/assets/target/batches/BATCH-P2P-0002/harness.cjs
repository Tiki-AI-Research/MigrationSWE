#!/usr/bin/env node
"use strict";

/*
 * Target-side P2P harness entry for BATCH-P2P-0002.
 *
 * The target runtime harness entry point is `harness.cjs` (see
 * RUNTIME_CONTEXT_JSON.required_harness_entries.target and
 * target_runtime.harness_command). This entry pins the side to "target" and
 * delegates to the shared side runner, which executes the repository's own
 * test suite under an explicit `timeout` and emits a raw PASS/FAIL projection
 * for every observation owned by this batch. The delegation itself is also
 * wrapped in an explicit `timeout` so the harness can never wait forever.
 */

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const timeoutSec = Number(process.env.P2P_TIMEOUT_SEC || 1800);
const report = process.env.P2P_REPORT || process.argv[2] || "/logs/result.json";
const repo = process.env.REPO_ROOT || null;

const candidates = [
  path.resolve(__dirname, "..", "..", "..", "shared", "batches", "BATCH-P2P-0002", "run-side.cjs"),
  path.resolve(__dirname, "..", "..", "shared", "batches", "BATCH-P2P-0002", "run-side.cjs"),
  path.resolve(__dirname, "..", "run-side.cjs"),
];

const shared = candidates.find((candidate) => fs.existsSync(candidate));
if (!shared) {
  process.stderr.write("BATCH-P2P-0002: shared runner not found\n");
  process.exit(1);
}

const argv = [shared, "--side", "target", "--report", report, "--timeout", String(timeoutSec)];
if (repo) argv.push("--repo", repo);

const result = spawnSync(
  "timeout",
  ["--signal=SIGKILL", String(timeoutSec), process.execPath, ...argv],
  { stdio: "inherit", timeout: (timeoutSec + 30) * 1000, killSignal: "SIGKILL" },
);
process.exit(typeof result.status === "number" ? result.status : 1);
