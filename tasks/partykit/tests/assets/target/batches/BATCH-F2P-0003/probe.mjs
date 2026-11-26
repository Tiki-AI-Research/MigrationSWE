import { run, parseArgs } from "../../../shared/batches/BATCH-F2P-0003/harness.mjs";

const { repoRoot, reportPath } = parseArgs("/input/target");
const summary = await run({ side: "target", repoRoot, reportPath });

if (summary.failed !== 0) process.exitCode = 1;
