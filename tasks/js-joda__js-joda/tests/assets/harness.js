#!/usr/bin/env node
'use strict';

/*
 * BATCH-INTEGRATION-0001 target-side entry point.
 *
 * Usage: node harness.js <report-path>
 *
 * Runs every required runtime observation on the migrated workspace and
 * writes a `migration-observations:v6` report.  The repository root is read
 * from MIGRATION_REPO_ROOT / MIGRATION_REPO / REPO_ROOT / REPO, falling back
 * to the current working directory.  The harness is bounded by a watchdog and
 * never waits indefinitely.
 */

const path = require('path');
const cli = require(path.join(__dirname, 'shared', 'integration', 'cli.js'));

cli.watchdog('target');

cli.main('target', process.argv).then(
  (code) => {
    process.exitCode = code;
  },
  (error) => {
    process.stderr.write(`${(error && error.stack) || error}\n`);
    process.exitCode = 1;
  },
);
