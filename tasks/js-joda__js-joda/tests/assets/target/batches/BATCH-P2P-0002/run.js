#!/usr/bin/env node
'use strict';

/*
 * BATCH-P2P-0002 target-side executable.
 *
 * Usage: node target/batches/BATCH-P2P-0002/run.js <repoRoot>
 *
 * Runs the repository's own, timeout-bounded tests and the batch checks
 * against the target (post-upgrade) tree and prints a JSON report with the raw
 * PASS/FAIL state per observation.
 */

const path = require('path');

const checksPath = path.join(__dirname, '..', '..', '..', 'shared', 'batches', 'BATCH-P2P-0002', 'p2p-checks.js');
const { runAll } = require(checksPath);

const repoRoot = process.argv[2] || process.cwd();
const report = runAll(repoRoot, 'target');

process.stdout.write(JSON.stringify(report) + '\n');
