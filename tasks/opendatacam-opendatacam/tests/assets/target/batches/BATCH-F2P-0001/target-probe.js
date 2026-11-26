'use strict';

// Target-side probe for BATCH-F2P-0001 (Fastify 4.28.1 reference tree).
//
// The target probe re-evaluates the exact same predicate module as the source
// probe; on the reference target every observation MUST resolve to PASS.
// When a live server base URL is supplied the observable half of the contract
// is additionally cross-checked over real HTTP (evaluateLive).
//
// Usage: node target-probe.js [root] [base-url]
//   root      default /input/target
//   base-url  optional, e.g. http://127.0.0.1:8080

const { evaluate, evaluateLive, OBSERVATIONS } = require('../../../shared/batches/BATCH-F2P-0001/predicates');

const root = process.argv[2] || '/input/target';
const base = process.argv[3] || null;

(async () => {
  const report = {
    side: 'target',
    batch_id: 'BATCH-F2P-0001',
    technology: 'Fastify 4.28.1',
    root,
    expectation: 'PASS',
    observations: [],
  };

  const results = evaluate(root);
  let live = {};
  if (base) {
    try {
      live = await evaluateLive(base);
    } catch (err) {
      live = { _error: err.message };
    }
  }

  let violations = 0;
  for (const obs of OBSERVATIONS) {
    const entry = results[obs.id];
    const pass = entry.status === 'PASS';
    if (!pass) violations += 1;
    report.observations.push({
      id: obs.id,
      status: entry.status,
      expectation_met: pass,
      requirement_ids: obs.requirement_ids,
      live: live[obs.id] || null,
    });
  }
  report.summary = {
    total: OBSERVATIONS.length,
    passed: report.observations.filter((o) => o.status === 'PASS').length,
    expectation_violations: violations,
    live_checked: Object.keys(live).filter((k) => k !== '_error').length,
  };

  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.exit(violations === 0 ? 0 : 1);
})();
