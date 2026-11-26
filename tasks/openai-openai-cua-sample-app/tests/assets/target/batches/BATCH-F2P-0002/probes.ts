import { runProbes } from "../../../shared/batches/BATCH-F2P-0002/probe-suite.ts";

async function main() {
  const repoRoot = process.env.CUA_REPO_ROOT ?? "/input/target";
  const results = await runProbes(repoRoot);
  process.stdout.write(
    `${JSON.stringify({ batch_id: "BATCH-F2P-0002", repo_root: repoRoot, side: "target", results }, null, 2)}\n`,
  );
}

void main();
