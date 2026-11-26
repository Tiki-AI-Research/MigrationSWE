#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const BATCH = 'BATCH-DIFFERENTIAL-0001';

const isRepoRoot = candidate =>
  Boolean(candidate) &&
  fs.existsSync(path.join(candidate, 'package.json')) &&
  fs.existsSync(path.join(candidate, 'src'));

const findRepoRoot = () => {
  const candidates = [
    process.env.REPO_ROOT,
    path.resolve(__dirname, '../../..'),
    path.resolve(__dirname, '../..'),
    process.cwd(),
  ];

  for (const candidate of candidates) {
    if (isRepoRoot(candidate)) {
      return candidate;
    }
  }

  throw new Error(`Unable to locate the repository root for ${BATCH}`);
};

const findHarness = repoRoot => {
  const candidates = [
    path.resolve(__dirname, `../../../shared/batches/${BATCH}/harness.js`),
    path.resolve(__dirname, `../../shared/batches/${BATCH}/harness.js`),
    path.resolve(__dirname, `../shared/batches/${BATCH}/harness.js`),
    path.resolve(repoRoot, `shared/batches/${BATCH}/harness.js`),
    path.resolve(repoRoot, `../shared/batches/${BATCH}/harness.js`),
  ];

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  throw new Error(`Unable to locate the shared harness for ${BATCH}`);
};

const loadPlugin = repoRoot => {
  const candidates = [
    path.join(repoRoot, 'lib'),
    repoRoot,
  ];

  let lastError;

  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError;
};

const main = async () => {
  const repoRoot = findRepoRoot();
  const { runAll } = require(findHarness(repoRoot));
  const plugin = loadPlugin(repoRoot);
  const eslintModule = require(require.resolve('eslint', { paths: [repoRoot] }));
  const result = await runAll({ repoRoot, plugin, eslintModule });

  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);

  if (!result.pass) {
    process.exitCode = 1;
  }
};

main().catch(error => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
