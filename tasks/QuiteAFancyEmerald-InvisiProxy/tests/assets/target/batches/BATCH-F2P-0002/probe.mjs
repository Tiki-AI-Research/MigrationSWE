// Target-side F2P probe for BATCH-F2P-0002.
//
// Runs each atomic observation entry against the Express reference target and
// records the raw observation for the target side. Each case is expected to
// observe PASS: the concrete entry is served and the handling path is proven to
// use Express 5.1.0 for that specific entry. A boot/infra failure is reported
// as INCONCLUSIVE and is never asserted as a valid PASS.

import {
  loadCases,
  resolveRepo,
  bootServer,
  stopServer,
  repoSource,
} from '../../../shared/batches/BATCH-F2P-0002/harness.mjs';
import { CHECKS } from '../../../shared/batches/BATCH-F2P-0002/harness.mjs';

const side = 'target';
const repo = resolveRepo(side);

const results = [];
const boot = await bootServer(repo);

if (!boot.ok) {
  for (const c of loadCases().cases)
    results.push({ observation_id: c.id, status: 'INCONCLUSIVE', detail: boot.reason });
} else {
  const src = repoSource(repo);
  for (const c of loadCases().cases) {
    try {
      const outcome = await CHECKS[c.check]({ repo, port: boot.port, src });
      results.push({
        observation_id: c.id,
        status: outcome.pass ? 'PASS' : 'FAIL',
        detail: outcome.detail,
      });
    } catch (error) {
      results.push({ observation_id: c.id, status: 'INCONCLUSIVE', detail: String(error) });
    }
  }
  stopServer(boot);
}

process.stdout.write(JSON.stringify({ batch_id: 'BATCH-F2P-0002', side, results }, null, 2) + '\n');
