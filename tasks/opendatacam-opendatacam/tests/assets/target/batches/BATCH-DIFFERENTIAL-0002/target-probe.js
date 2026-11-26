'use strict';

// Target-side (Fastify 4.28.1) differential probe for BATCH-DIFFERENTIAL-0002.
//
// MUST be executed against the private reference target at /input/target (with
// its node_modules materialized) before any target PASS is declared. It runs the
// same shared case corpus and projects each observation with the shared
// normalization so the result can be compared with the Express source
// projection under `equal_after_normalization`.

const path = require('path');
const projections = require('../../../shared/batches/BATCH-DIFFERENTIAL-0002/projections');

const DEFAULT_REPO = '/input/target';

async function probe(repoRoot, options) {
  const opts = Object.assign({ side: 'target' }, options || {});
  return projections.run(repoRoot || DEFAULT_REPO, opts);
}

if (require.main === module) {
  const repoRoot = process.argv[2] ? path.resolve(process.argv[2]) : DEFAULT_REPO;
  const port = process.argv[3] ? parseInt(process.argv[3], 10) : 18120;
  probe(repoRoot, { port }).then(
    (result) => { process.stdout.write(`${JSON.stringify(result)}\n`); },
    (err) => { process.stderr.write(`${err.stack || err}\n`); process.exit(1); },
  );
}

module.exports = { probe, DEFAULT_REPO };
