#!/usr/bin/env node
'use strict';

/**
 * BATCH-F2P-0003 target-side probe runner.
 *
 * Uses the exact same shared probes as the source runner; only the reported
 * side label differs.  No outcome is derived from the runner role.
 *
 * Usage: node run.js [repoRoot] [reportPath]
 * The process is bounded by an explicit watchdog timeout so the harness can
 * never block forever.
 */

const fs = require('fs');
const support = require('../../../shared/batches/BATCH-F2P-0003/support');
const { OBSERVATIONS } = require('../../../shared/batches/BATCH-F2P-0003/probes');

const SIDE = 'target';
const TIMEOUT_MS = Number(process.env.F2P_RUNNER_TIMEOUT_MS || 120000);

async function main(argv) {
  const repoRoot = argv[2] || process.env.REPO_ROOT || '/workspace/repo';
  const reportPath = argv[3];
  const ctx = support.createContext(repoRoot);
  const checks = await support.runAll(ctx, OBSERVATIONS);
  const report = {
    schema_version: 'migration-probe-report:v1',
    batch_id: 'BATCH-F2P-0003',
    side: SIDE,
    repo_root: ctx.repoRoot,
    target_name: support.MIGRATION.name,
    target_version: support.TARGET_VERSION,
    checks,
  };
  const text = JSON.stringify(report, null, 2);
  if (reportPath) {
    fs.writeFileSync(reportPath, text);
  } else {
    process.stdout.write(`${text}\n`);
  }
  return 0;
}

const watchdog = setTimeout(() => {
  process.stderr.write('F2P target runner exceeded watchdog timeout\n');
  process.exit(124);
}, TIMEOUT_MS);
watchdog.unref();

main(process.argv).then(
  (code) => {
    process.exitCode = code;
  },
  (err) => {
    process.stderr.write(`${(err && err.stack) || err}\n`);
    process.exitCode = 1;
  },
);
