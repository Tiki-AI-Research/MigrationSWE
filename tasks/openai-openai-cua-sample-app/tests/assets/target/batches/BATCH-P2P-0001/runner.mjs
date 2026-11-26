#!/usr/bin/env node
// Target-side P2P runner for BATCH-P2P-0001.
// Delegates to the shared executor against the reference (post-migration) target.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

function pickRepoRoot() {
  const candidates = [
    process.env.REPO_ROOT,
    process.env.REPO_ROOT,
    "/workspace/repo",
    "/input/target",
  ];
  for (const candidate of candidates) {
    if (candidate && existsSync(join(candidate, "javascript-app", "package.json"))) return candidate;
  }
  return process.env.REPO_ROOT ?? "/input/target";
}

const argv = process.argv.slice(2);
const repoRoot = argv.find((arg) => !arg.endsWith(".json") && arg !== "target") ?? pickRepoRoot();
const report = argv.find((arg) => arg.endsWith(".json")) ?? process.env.P2P_REPORT ?? "/logs/result.json";
const shared = join(here, "..", "..", "..", "shared", "batches", "BATCH-P2P-0001", "runner.mjs");

const result = spawnSync(process.execPath, [shared, repoRoot, report, "target"], { stdio: "inherit" });
process.exit(result.status ?? 1);
