#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { evaluate } = require(
  path.join(__dirname, '../../../shared/batches/BATCH-F2P-0002/predicates.js'),
);

const repoRoot = process.env.MIGRATION_REPO
  ? path.resolve(process.env.MIGRATION_REPO)
  : path.resolve(__dirname, '../../..');
const side = path.basename(path.dirname(path.dirname(__dirname)));
const reportPath = process.argv[2];

evaluate(repoRoot)
  .then(report => {
    const payload = {
      batch_id: 'BATCH-F2P-0002',
      side,
      ...report,
    };
    const serialized = JSON.stringify(payload, null, 2);
    if (reportPath) {
      fs.mkdirSync(path.dirname(reportPath), { recursive: true });
      fs.writeFileSync(reportPath, serialized);
    }
    process.stdout.write(`${serialized}\n`);
  })
  .catch(error => {
    process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
    process.exitCode = 1;
  });
