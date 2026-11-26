'use strict';

// Target-side (Fastify 4.28.1) P2P probe for BATCH-P2P-0001.
//
// The probe MUST be executed against a real /input/target copy before any PASS is
// declared. It uses the exact same shared projection module as the source probe so
// that a normalized source value equals the normalized target value for every
// preserved observation.

const requests = require('../../../fixtures/batches/BATCH-P2P-0001/requests.json');
const { runObservations } = require('../../../shared/batches/BATCH-P2P-0001/p2p-checks');

async function probe(port) {
  const observations = await runObservations({ side: 'target', port, fixtures: requests });
  return {
    side: 'target',
    framework: 'fastify@4.28.1',
    port,
    batch_id: 'BATCH-P2P-0001',
    observations,
  };
}

module.exports = { probe };
