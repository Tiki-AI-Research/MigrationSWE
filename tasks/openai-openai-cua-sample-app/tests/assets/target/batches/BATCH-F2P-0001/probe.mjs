import { pathToFileURL } from "node:url";

import {
  batchId,
  observations,
  runAll,
  runObservation,
  targetTechnology,
} from "../../../shared/batches/BATCH-F2P-0001/predicates.mjs";

export { batchId, observations, runAll, runObservation, targetTechnology };
export const side = "target";

export async function run(repoRoot) {
  return runAll(repoRoot);
}

async function main() {
  const args = process.argv.slice(2);
  const repoRoot = args[0] ?? process.env.CUA_REPO_ROOT ?? process.cwd();
  const results = await runAll(repoRoot);
  const report = {
    batch_id: batchId,
    side,
    target_technology: targetTechnology,
    repo_root: repoRoot,
    results,
  };
  const reportIndex = args.indexOf("--report");
  const payload = JSON.stringify(report, null, 2);
  if (reportIndex !== -1 && args[reportIndex + 1]) {
    const { writeFileSync } = await import("node:fs");
    writeFileSync(args[reportIndex + 1], payload);
  } else {
    process.stdout.write(`${payload}\n`);
  }
  if (results.some((entry) => entry.status !== "PASS")) {
    process.exitCode = 1;
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
