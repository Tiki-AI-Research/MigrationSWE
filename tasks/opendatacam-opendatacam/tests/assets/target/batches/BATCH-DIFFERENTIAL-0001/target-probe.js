'use strict';

// Target-side DIFFERENTIAL probe (Fastify 4.28.1 semantics).
//
// Executed against the read-only reference target mounted at /input/target.
// The probe inspects the migrated Fastify server entry point and its dependency
// manifest and emits the normalized fingerprint that is compared with the
// source probe through `equalAfterNormalization`. INTEGRATION owns
// `target/runner.js`; this file is a batch-scoped probe and never a runner.

const path = require('path');
const {
  inspectModuleGraph,
  REQUIRED_TARGET_IMPORTS,
} = require('../../../shared/batches/BATCH-DIFFERENTIAL-0001/diff-harness');

const DEFAULT_ROOT = '/input/target';

function probe(root) {
  const targetRoot = root || DEFAULT_ROOT;
  const graph = inspectModuleGraph(targetRoot, 'server.js');
  return {
    side: 'target',
    root: targetRoot,
    entry: path.join(targetRoot, 'server.js'),
    // Fastify must own every migrated concern on the target side.
    fastifyOwnsConcerns: REQUIRED_TARGET_IMPORTS.filter((name) => graph.requires.includes(name)),
    expressPresent: graph.forbiddenImports,
    forbiddenDependencies: graph.forbiddenDependencies,
    requires: graph.requires,
  };
}

module.exports = { probe };

if (require.main === module) {
  const output = probe(process.argv[2]);
  process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
}
