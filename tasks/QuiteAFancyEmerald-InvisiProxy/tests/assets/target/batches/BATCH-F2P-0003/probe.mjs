// Target-side F2P probe for BATCH-F2P-0003.
//
// Runs the shared predicate set against the reference target (the Express 5
// repository). Every observation in this batch is expected to PASS here, because
// F2P observations are target-only postconditions.
//
// Usage:
//   PROBE_REPO=/input/target PROBE_BASE=http://127.0.0.1:8080 \
//     node target/batches/BATCH-F2P-0003/probe.mjs

import { runAll } from '../../../shared/batches/BATCH-F2P-0003/predicates.mjs';

const results = await runAll();
for (const result of results)
  console.log(
    `${result.ok ? 'PASS' : 'FAIL'} ${result.observation_id} :: ${result.detail}`
  );

const failed = results.filter((result) => !result.ok);
console.log(
  `target F2P summary: ${results.length - failed.length}/${results.length} passed`
);
if (failed.length) process.exitCode = 1;
