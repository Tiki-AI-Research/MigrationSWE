'use strict';

const path = require('path');
const {
  OBSERVATION_IDS,
  runProbes,
} = require(path.join(
  __dirname,
  '..',
  '..',
  '..',
  'shared',
  'batches',
  'BATCH-F2P-0001',
  'probes.js',
));

async function run(repoRoot) {
  const root = path.resolve(
    repoRoot || process.env.MIGRATION_REPO_ROOT || process.cwd(),
  );
  let results;

  try {
    results = await runProbes(root, 'target');
  } catch (error) {
    results = OBSERVATION_IDS.map(observationId => ({
      observation_id: observationId,
      status: 'INFRA_ERROR',
      detail: error && error.message ? error.message : String(error),
    }));
  }

  return {
    side: 'target',
    root,
    ok: results.every(result => result.status === 'PASS'),
    results,
  };
}

if (require.main === module) {
  run(process.argv[2]).then(result => {
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    process.exitCode = result.ok ? 0 : 1;
  });
}

module.exports = { run };
