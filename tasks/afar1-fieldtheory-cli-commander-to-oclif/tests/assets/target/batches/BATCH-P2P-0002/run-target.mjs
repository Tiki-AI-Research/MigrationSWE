#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { runHarness } from '../../../shared/batches/BATCH-P2P-0002/p2p-harness.mjs';

function firstValue(...values) {
  return values.find((value) => typeof value === 'string' && value.length > 0) ?? '';
}

let repo = firstValue(
  process.env.MIGRATION_REPO,
  process.env.MIGRATION_REPO_ROOT,
  process.env.REPO,
  process.env.REPO_ROOT,
);
let report = firstValue(process.env.REPORT_PATH, process.env.MIGRATION_REPORT);
let observation = firstValue(process.env.OBSERVATION_ID);
const positional = [];

for (let index = 2; index < process.argv.length; index += 1) {
  const arg = process.argv[index];
  if (arg === '--repo') repo = firstValue(process.argv[++index], repo);
  else if (arg.startsWith('--repo=')) repo = arg.slice('--repo='.length);
  else if (arg === '--report') report = firstValue(process.argv[++index], report);
  else if (arg.startsWith('--report=')) report = arg.slice('--report='.length);
  else if (arg === '--observation' || arg === '--observation-id') observation = firstValue(process.argv[++index], observation);
  else if (arg.startsWith('--observation=')) observation = arg.slice('--observation='.length);
  else if (arg.startsWith('--observation-id=')) observation = arg.slice('--observation-id='.length);
  else positional.push(arg);
}

if (!repo) {
  repo = positional.find((value) => fs.existsSync(path.join(value, 'package.json'))) ?? '';
}
if (!report) {
  report = positional.find((value) => value.endsWith('.json')) ?? '';
}
if (!repo) {
  const cwd = process.cwd();
  repo = fs.existsSync(path.join(cwd, 'package.json')) ? cwd : '/workspace/repo';
}

const result = await runHarness({ repo: path.resolve(repo), side: 'target', onlyObservation: observation || undefined });
const text = JSON.stringify(result, null, 2);
if (report) {
  fs.mkdirSync(path.dirname(path.resolve(report)), { recursive: true });
  fs.writeFileSync(report, `${text}\n`);
}
console.log(text);
process.exit(result.summary.fail === 0 ? 0 : 1);
