// Target-side F2P probe runner for BATCH-F2P-0003.
//
// Executes every atomic target-postcondition probe against the Express target
// checkout. The checkout must already have its workspace dependencies
// materialized (node_modules) by the integration harness. Because server.ts is
// TypeScript, this runner is meant to be executed with a TS-aware loader such
// as tsx, e.g.:
//
//   tsx target/batches/BATCH-F2P-0003/run-probes.mjs --root /workspace/repo
//
// Exit code is 0 only when every probe reports PASS.

import { pathToFileURL } from "node:url";
import { join, resolve } from "node:path";

import { runProbes } from "../../../shared/batches/BATCH-F2P-0003/probes.mjs";

function parseArgs(argv) {
  const args = { root: process.env.TARGET_ROOT ?? process.cwd(), out: undefined };
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--root") args.root = argv[++index];
    else if (argv[index] === "--out") args.out = argv[++index];
  }
  return args;
}

export async function run({ root, out } = {}) {
  const resolvedRoot = resolve(root ?? process.cwd());
  const context = {
    mode: "target",
    root: resolvedRoot,
    importServer: () =>
      import(pathToFileURL(join(resolvedRoot, "javascript-app/src/server.ts")).href),
    importContracts: () =>
      import(pathToFileURL(join(resolvedRoot, "contracts/index.ts")).href),
  };
  context.contracts = await context.importContracts();

  const report = await runProbes(context);
  report.mode = "target";
  report.expectation = "PASS";

  const failed = report.results.filter((result) => result.status !== "PASS");
  report.summary = {
    total: report.results.length,
    passed: report.results.length - failed.length,
    failed: failed.length,
  };

  if (out) {
    const { writeFile } = await import("node:fs/promises");
    await writeFile(out, `${JSON.stringify(report, null, 2)}\n`);
  }
  return report;
}

const invokedDirectly = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invokedDirectly) {
  const args = parseArgs(process.argv.slice(2));
  const report = await run(args);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.exit(report.summary.failed === 0 ? 0 : 1);
}
