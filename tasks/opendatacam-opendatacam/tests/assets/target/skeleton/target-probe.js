'use strict';

// Target-side probe skeleton (Fastify 4.28.1 semantics).
//
// INTEGRATION owns `target/runner.js`; this scaffold only fixes the shared probe
// shape so INTEGRATION and the F2P/DIFF batches agree on an interface. Target
// probes MUST be executed against a real /input/target copy before any PASS is
// declared; the SKELETON batch performs no execution.

const contracts = require('../../shared/skeleton/contracts.json');
const { jsonRequest } = require('../../shared/skeleton/harness');

async function probe(port) {
  const reachableRoutes = [];
  for (const route of contracts.routes) {
    if (route.method !== 'GET' || route.path.includes(':')) continue;
    try {
      const res = await jsonRequest(port, 'GET', route.path);
      reachableRoutes.push({ route: `GET ${route.path}`, statusCode: res.statusCode });
    } catch (err) {
      reachableRoutes.push({ route: `GET ${route.path}`, error: err.message });
    }
  }

  return {
    side: 'target',
    port,
    contracts,
    reachableRoutes,
    observations: [],
  };
}

module.exports = { probe };
