import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SIDE = 'target';
const BATCH_ID = 'BATCH-P2P-0001';
const require = createRequire(import.meta.url);
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const assetsRoot = process.env.ASSETS_ROOT
  ? path.resolve(process.env.ASSETS_ROOT)
  : path.resolve(scriptDir, '../../..');
const repoRoot = process.env.REPO_ROOT
  ? path.resolve(process.env.REPO_ROOT)
  : path.resolve(process.env.MIGRATION_REPO_ROOT ?? '/workspace/repo');
const reportArg = process.argv[2];
const reportPath = process.env.REPORT_PATH
  ? path.resolve(process.env.REPORT_PATH)
  : reportArg && reportArg.endsWith('.json')
    ? path.resolve(reportArg)
    : '/logs/result.json';

const distEntry = path.join(repoRoot, 'dist/index.cjs');
if (!fs.existsSync(distEntry)) {
  execFileSync('npm', ['run', 'build'], { cwd: repoRoot, stdio: 'inherit' });
}

const lib = require(distEntry);
const z = require(path.join(repoRoot, 'node_modules/zod'));
const casesPath = path.join(
  assetsRoot,
  'shared',
  'batches',
  BATCH_ID,
  'p2p-cases.mjs'
);
const expectedPath = path.join(
  assetsRoot,
  'fixtures',
  'batches',
  BATCH_ID,
  'expected-projections.json'
);
const { runCases } = await import(pathToFileURL(casesPath).href);
const result = await runCases({ lib, z, repoRoot, fs, crypto });

const expected = fs.existsSync(expectedPath)
  ? JSON.parse(fs.readFileSync(expectedPath, 'utf8'))
  : null;
const observations = Object.fromEntries(
  Object.entries(result.observations).map(([id, entry]) => {
    const expectedEntry = expected?.observations?.[id];
    return [
      id,
      {
        ...entry,
        matches_expected:
          !expectedEntry ||
          JSON.stringify(entry.value) === JSON.stringify(expectedEntry.value),
      },
    ];
  })
);
const values = Object.values(observations);
const summary = {
  total: values.length,
  passed: values.filter(entry => entry.status === 'PASS').length,
  failed: values.filter(entry => entry.status !== 'PASS').length,
  mismatched:
    expected === null
      ? 0
      : values.filter(entry => entry.matches_expected === false).length,
};
const report = {
  schema_version: 'migration-p2p-report:v1',
  batch_id: BATCH_ID,
  side: SIDE,
  repo_root: repoRoot,
  observations,
  summary,
};

fs.mkdirSync(path.dirname(reportPath), { recursive: true });
fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
if (summary.failed > 0 || summary.mismatched > 0) {
  process.exitCode = 1;
}
