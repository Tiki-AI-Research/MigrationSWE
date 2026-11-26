#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const SIDE = 'target';

function registerBabel() {
  try {
    require('@babel/register')({
      only: [new RegExp(process.env.REPO_ROOT ? process.env.REPO_ROOT.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') : '')],
      babelrc: false,
      configFile: false,
      presets: [['@babel/preset-env', { targets: { node: 'current' } }]],
    });
  } catch (error) {
    if (error && error.code !== 'MODULE_NOT_FOUND') {
      throw error;
    }
  }
}

function loadProbes() {
  const candidates = [
    path.join(__dirname, '..', '..', '..', 'shared', 'batches', 'BATCH-F2P-0001', 'f2p-probes.js'),
    path.join(__dirname, 'f2p-probes.js'),
  ];
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (error) {
      if (!error || error.code !== 'MODULE_NOT_FOUND') {
        throw error;
      }
    }
  }
  throw new Error('unable to locate f2p-probes.js');
}

function resolveRepoRoot(argv) {
  const candidate = argv[2] || process.env.REPO_ROOT || path.join(__dirname, '..', '..', '..');
  return path.resolve(candidate);
}

function resolveReportPath(argv) {
  return argv[3] || process.env.F2P_REPORT_PATH || null;
}

function run(argv) {
  const repoRoot = resolveRepoRoot(argv);
  process.env.REPO_ROOT = repoRoot;
  registerBabel();
  const probes = loadProbes();
  const observations = probes.evaluateAll(repoRoot);
  const report = {
    schema_version: 'migration-harness-report:v1',
    batch_id: 'BATCH-F2P-0001',
    side: SIDE,
    repo_root: repoRoot,
    observations,
  };
  const serialized = JSON.stringify(report, null, 2);
  const reportPath = resolveReportPath(argv);
  if (reportPath) {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, serialized + '\n');
  }
  process.stdout.write(serialized + '\n');
  return 0;
}

if (require.main === module) {
  process.exitCode = run(process.argv);
}

module.exports = { run, loadProbes };
