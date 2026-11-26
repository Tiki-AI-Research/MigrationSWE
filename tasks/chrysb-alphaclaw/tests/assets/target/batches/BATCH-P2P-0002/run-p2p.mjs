import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const kBatchId = "BATCH-P2P-0002";
const kRunnerPath = fileURLToPath(import.meta.url);
const kBatchDir = path.dirname(kRunnerPath);
const kSide = path.basename(path.dirname(path.dirname(kBatchDir)));
const kAssetRoot = path.resolve(kBatchDir, "..", "..", "..");
const kPlanPath = path.join(
  kAssetRoot,
  "shared",
  "batches",
  kBatchId,
  "test-plan.json",
);
const kPlan = JSON.parse(readFileSync(kPlanPath, "utf8"));

const kRepoRoot = path.resolve(
  process.env.MIGRATION_REPO_ROOT ||
    process.env.MIGRATION_REPO ||
    process.env.REPO_ROOT ||
    process.env.REPO ||
    process.argv.slice(2).find((arg) => !arg.startsWith("-")) ||
    process.cwd(),
);

const escapeRegExp = (value) =>
  String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const runCase = (testCase) => {
  const env = {
    ...process.env,
    MIGRATION_REPO_ROOT: kRepoRoot,
    MIGRATION_REPO: kRepoRoot,
    REPO_ROOT: kRepoRoot,
    REPO: kRepoRoot,
  };
  let command;
  let args;

  if (testCase.kind === "vitest") {
    command = path.join(kRepoRoot, "node_modules", ".bin", "vitest");
    args = [
      "run",
      testCase.file,
      "-t",
      escapeRegExp(testCase.name),
    ];
  } else if (testCase.kind === "node-test") {
    command = process.execPath;
    args = [
      "--test",
      "--test-name-pattern",
      escapeRegExp(testCase.name),
      path.join(kAssetRoot, kSide, "batches", kBatchId, testCase.file),
    ];
  } else {
    return { ...testCase, status: "FAIL", reason: "unknown case kind" };
  }

  const result = spawnSync(command, args, {
    cwd: kRepoRoot,
    env,
    encoding: "utf8",
    timeout: 120000,
  });
  return {
    ...testCase,
    status: result.status === 0 ? "PASS" : "FAIL",
    exit_code: result.status,
    stdout_tail: String(result.stdout || "").split("\n").slice(-8).join("\n"),
    stderr_tail: String(result.stderr || "").split("\n").slice(-8).join("\n"),
  };
};

const observations = {};
for (const [observationId, entry] of Object.entries(kPlan.observations)) {
  const cases = entry[kSide] || [];
  const results = cases.map(runCase);
  const passed = results.length > 0 && results.every((item) => item.status === "PASS");
  observations[observationId] = {
    value: { result: passed ? "PASS" : "FAIL" },
    cases: results,
  };
}

const report = {
  schema_version: "p2p-results:v1",
  batch_id: kBatchId,
  side: kSide,
  repo_root: kRepoRoot,
  projection: kPlan.projection,
  observations,
};

process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
process.exitCode = Object.values(observations).every(
  (entry) => entry.value.result === "PASS",
)
  ? 0
  : 1;
