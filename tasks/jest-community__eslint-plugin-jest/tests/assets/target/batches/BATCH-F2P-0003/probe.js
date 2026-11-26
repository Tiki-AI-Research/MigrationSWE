#!/usr/bin/env node
'use strict';

const path = require('path');

const { runCli } = require(
  path.resolve(
    __dirname,
    '../../../shared/batches/BATCH-F2P-0003/probe-lib.js',
  ),
);

const repoRoot = process.env.PROBE_REPO_ROOT || '/input/target';

runCli(repoRoot).then(
  code => {
    process.exitCode = code;
  },
  error => {
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exitCode = 2;
  },
);
