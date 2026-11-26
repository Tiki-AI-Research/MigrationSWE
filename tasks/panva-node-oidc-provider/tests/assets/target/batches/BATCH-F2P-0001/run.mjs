// Target-side runner for BATCH-F2P-0001.
//
// Runs every observation of this batch against the migrated repository and
// requires the target postcondition to hold *and* the request path to be owned
// by Fastify. Every observation must observe PASS here; a FAIL is a genuine
// missing target capability.

import { writeFile } from 'node:fs/promises';
import { run, OBSERVATION_IDS } from '../../../shared/batches/BATCH-F2P-0001/harness.mjs';

const repo = process.env.REPO_ROOT || process.argv[2] || '/workspace/repo';
const reportPath = process.env.REPORT || process.argv[3];

const { results } = await run(repo);
const observations = results.map((result) => ({
  observation_id: result.id,
  requirement_ids: [result.requirement],
  entry: result.entry,
  behavior: result.behavior,
  fastify: result.fastify,
  status: result.behavior && result.fastify ? 'PASS' : 'FAIL',
  detail: result.detail,
}));

const report = { side: 'target', repo, observation_ids: OBSERVATION_IDS, observations };
if (reportPath) await writeFile(reportPath, JSON.stringify(report, null, 2));

const failed = observations.filter((observation) => observation.status !== 'PASS');
for (const observation of observations) {
  console.log(`${observation.status} ${observation.observation_id} behavior=${observation.behavior} fastify=${observation.fastify}${observation.detail ? ` (${observation.detail})` : ''}`);
}
process.exit(failed.length ? 1 : 0);
