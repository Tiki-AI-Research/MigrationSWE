import process from 'node:process';
import path from 'node:path';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { runReport } from '../../../shared/batches/BATCH-DIFFERENTIAL-0001/cases.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));

function looksLikeRepo(candidate) {
  return Boolean(candidate) && fs.existsSync(path.join(candidate, 'package.json')) &&
    (fs.existsSync(path.join(candidate, 'src', 'index.ts')) || fs.existsSync(path.join(candidate, 'dist')));
}

function resolveRepoRoot() {
  const candidates = [
    process.env.MIGRATION_REPO_ROOT,
    process.env.REPO_ROOT,
    process.cwd(),
    path.resolve(here, '../../..'),
    '/workspace/repo'
  ];
  return candidates.find(looksLikeRepo) ?? path.resolve(here, '../../..');
}

const repoRoot = resolveRepoRoot();
const require = createRequire(path.join(repoRoot, 'package.json'));
const zod = require('zod');
const cjs = path.join(repoRoot, 'dist/index.cjs');
const esm = path.join(repoRoot, 'dist/index.mjs');
const lib = fs.existsSync(cjs) ? require(cjs) : await import(esm);

export function run() {
  return runReport({ z: zod, lib, repoRoot });
}

export default run;

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const report = run();
  if (process.argv[2]) {
    fs.writeFileSync(process.argv[2], JSON.stringify(report, null, 2));
  } else {
    process.stdout.write(JSON.stringify(report, null, 2));
  }
}
