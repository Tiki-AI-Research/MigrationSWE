#!/usr/bin/env node
'use strict';

/*
 * BATCH-CONTRACT-0001 target-side executable contract probe.
 *
 * Usage: node probe.js <repoRoot> [outFile]
 * Executes the public-contract observations against the reference target
 * js-joda checkout and emits the normalized value observed on the target side.
 */

const path = require('path');

const SHARED_CANDIDATES = [
  path.join(__dirname, '..', '..', '..', 'shared', 'batches', 'BATCH-CONTRACT-0001', 'contract-probe.js'),
  path.join(__dirname, '..', '..', 'shared', 'batches', 'BATCH-CONTRACT-0001', 'contract-probe.js'),
  path.join(__dirname, '..', 'shared', 'batches', 'BATCH-CONTRACT-0001', 'contract-probe.js'),
];

function loadShared() {
  let lastError = null;
  for (let i = 0; i < SHARED_CANDIDATES.length; i += 1) {
    try {
      return require(SHARED_CANDIDATES[i]);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error('unable to locate BATCH-CONTRACT-0001 shared contract probe');
}

const probe = loadShared();
const argv = [process.argv[0], process.argv[1], 'target'].concat(process.argv.slice(2));
process.exitCode = probe.main(argv);
