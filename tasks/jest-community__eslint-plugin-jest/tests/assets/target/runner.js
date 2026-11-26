#!/usr/bin/env node
'use strict';

const path = require('path');
const { run } = require(path.resolve(__dirname, '../shared/integration/run.js'));

const report = process.argv[2] || '/logs/result.json';
run({ side: 'target', assets: path.resolve(__dirname, '..'), report })
  .then(result => {
    process.stdout.write(`${JSON.stringify(result)}\n`);
  })
  .catch(error => {
    process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
    process.exitCode = 1;
  });
