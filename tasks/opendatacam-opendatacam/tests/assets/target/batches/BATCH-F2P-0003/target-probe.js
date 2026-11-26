'use strict';

// BATCH-F2P-0003 target probe.
//
// Executes the shared F2P predicates against the read-only reference target
// mounted at /input/target. This MUST be run against the real reference target
// before any PASS is declared; nothing here is pre-baked per side.

const { evaluateAll } = require('../../../shared/batches/BATCH-F2P-0003/probe-lib');

const root = process.argv[2] || '/input/target';

const results = evaluateAll(root);

process.stdout.write(`${JSON.stringify({
  batch_id: 'BATCH-F2P-0003',
  side: 'target',
  root,
  results,
}, null, 2)}\n`);
