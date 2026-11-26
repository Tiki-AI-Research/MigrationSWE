import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { OBSERVATIONS } from './observations.mjs';
import { makeContext } from './cli-probe-lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const defaultFixtures = path.resolve(here, '../../..', 'fixtures', 'batches', 'BATCH-F2P-0002');

function parseArgs(argv) {
  const parsed = { positional: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--repo') parsed.repo = argv[++index];
    else if (arg === '--report') parsed.report = argv[++index];
    else if (arg === '--fixtures') parsed.fixtures = argv[++index];
    else if (arg === '--help' || arg === '-h') parsed.help = true;
    else parsed.positional.push(arg);
  }
  if (!parsed.repo && parsed.positional.length > 0) parsed.repo = parsed.positional[0];
  if (!parsed.report && parsed.positional.length > 1) parsed.report = parsed.positional[1];
  return parsed;
}

export async function runProbes(side, argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (options.help || !options.repo) {
    console.log(`Usage: node run-probes.mjs --repo <repository-root> [--report <report.json>] [--fixtures <fixture-dir>]`);
    return options.help ? 0 : 2;
  }

  const ctx = makeContext(options.repo, options.fixtures ?? defaultFixtures);
  const startedAt = new Date().toISOString();
  const results = [];

  for (const observation of OBSERVATIONS) {
    const started = Date.now();
    try {
      await observation.run(ctx);
      results.push({
        observation_id: observation.id,
        requirement_ids: observation.requirement_ids,
        status: 'PASS',
        duration_ms: Date.now() - started,
        detail: observation.description,
      });
    } catch (error) {
      const infra = error?.name === 'ProbeInfraError';
      results.push({
        observation_id: observation.id,
        requirement_ids: observation.requirement_ids,
        status: infra ? 'INFRA_ERROR' : 'FAIL',
        duration_ms: Date.now() - started,
        detail: error?.message ?? String(error),
        error_name: error?.name ?? 'Error',
      });
    }
  }

  const report = {
    schema_version: 'migration-f2p-probe-report:v1',
    batch_id: 'BATCH-F2P-0002',
    side,
    repo_root: ctx.repoRoot,
    started_at: startedAt,
    finished_at: new Date().toISOString(),
    observations: results,
  };

  if (options.report) {
    fs.mkdirSync(path.dirname(path.resolve(options.report)), { recursive: true });
    fs.writeFileSync(options.report, `${JSON.stringify(report, null, 2)}\n`);
  }

  const expected = side === 'target' ? 'PASS' : 'FAIL';
  const mismatches = results.filter((entry) => entry.status !== expected);
  for (const entry of results) {
    console.log(`${entry.observation_id}\t${entry.status}\t${entry.detail}`);
  }
  if (mismatches.length > 0) {
    console.error(`\n${mismatches.length} observation(s) did not produce the expected ${expected} status on the ${side} side`);
    return 1;
  }
  return 0;
}
