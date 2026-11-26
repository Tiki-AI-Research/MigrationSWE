import { main } from '../../../shared/batches/BATCH-DIFFERENTIAL-0001/runner.mjs';

const repo = process.argv[2] ?? process.env.MIGRATION_REPO ?? process.env.REPO_ROOT ?? process.env.REPO ??
  process.env.MIGRATION_REPO_ROOT ?? '/workspace/repo';
const out = process.argv[3] ?? process.env.MIGRATION_OUT ?? process.env.OUT;

await main({ repo, side: 'target', out });
