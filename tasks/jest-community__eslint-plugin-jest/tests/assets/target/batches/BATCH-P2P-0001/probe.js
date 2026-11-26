#!/usr/bin/env node
'use strict';

const path = require('path');
const { run } = require(path.join(
  __dirname,
  '../../../shared/batches/BATCH-P2P-0001/probe.js',
));

const root = process.argv[2] || process.env.P2P_REPO_ROOT || '/workspace/repo';
const report = run({ root, side: 'target' });

process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
process.exitCode = report.all_pass ? 0 : 1;
