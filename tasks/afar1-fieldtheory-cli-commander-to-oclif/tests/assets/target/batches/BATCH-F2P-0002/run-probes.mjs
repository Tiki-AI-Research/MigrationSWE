import { runProbes } from '../../../shared/batches/BATCH-F2P-0002/runner.mjs';

process.exitCode = await runProbes('target', process.argv.slice(2));
