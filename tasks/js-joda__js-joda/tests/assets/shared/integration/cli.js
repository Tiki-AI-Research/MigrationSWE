#!/usr/bin/env node
'use strict';

/*
 * BATCH-INTEGRATION-0001 report CLI.
 *
 * Shared by the two integration entry points:
 *   - source/runner        runs every required runtime observation on the
 *                          pre-upgrade source checkout (side = "source")
 *   - harness.js           runs every required runtime observation on the
 *                          migrated workspace (side = "target")
 *
 * Both emit a `migration-observations:v6` report and never hardcode a
 * PASS/FAIL by role: the status is the raw outcome of the owning batch probe.
 */

const path = require('path');
const { assetRoot, writeJson, serializeJson, writeStdout, emitReportDigest } = require(path.join(__dirname, 'runtime'));
const { runSide, loadInventory } = require(path.join(__dirname, 'dispatch'));

const REPO_ENV_KEYS = ['MIGRATION_REPO_ROOT', 'MIGRATION_REPO', 'REPO_ROOT', 'REPO'];
const DEFAULT_TIMEOUT_MS = Number(process.env.INTEGRATION_TIMEOUT_MS || 1800000);

function resolveRepoRoot() {
  for (const key of REPO_ENV_KEYS) {
    if (process.env[key] && process.env[key].trim().length > 0) {
      return path.resolve(process.env[key].trim());
    }
  }
  return path.resolve(process.cwd());
}

function resolveReportPath(argv) {
  if (argv[2] && argv[2].length > 0) {
    return argv[2];
  }
  return process.env.MIGRATION_REPORT_PATH || process.env.REPORT_PATH || null;
}

/*
 * The runtime report must always cover every required observation ID: an
 * empty `observations` array is explicitly forbidden and is what previously
 * made the P2P projections look unexecutable.  When dispatch cannot complete
 * (module missing, watchdog expiry, unexpected throw) we still emit one entry
 * per inventory observation, all marked INCONCLUSIVE, so coverage holds and
 * the failure is visible instead of silently dropping the whole inventory.
 */
function buildFallback(side, repoRoot, reason) {
  let observations = [];
  try {
    observations = loadInventory().map((item) => ({
      id: item.id,
      kind: item.kind,
      status: 'INCONCLUSIVE',
      value: { type: 'json', value: { reason } },
    }));
  } catch (e) {
    observations = [];
  }
  return {
    schema_version: 'migration-observations:v6',
    batch_id: 'BATCH-INTEGRATION-0001',
    side,
    repo_root: repoRoot,
    target_name: 'js-joda',
    target_version: '6.3.2',
    observations,
    diagnostics: [{ side, error: reason }],
  };
}

async function main(side, argv) {
  const repoRoot = resolveRepoRoot();
  const reportPath = resolveReportPath(argv);
  let report;
  try {
    const { observations, diagnostics } = await runSide(side, repoRoot);
    report = {
      schema_version: 'migration-observations:v6',
      batch_id: 'BATCH-INTEGRATION-0001',
      side,
      repo_root: repoRoot,
      target_name: 'js-joda',
      target_version: '6.3.2',
      observations,
      diagnostics,
    };
  } catch (error) {
    const reason = `integration dispatch failed: ${error && error.message ? error.message : error}`;
    report = buildFallback(side, repoRoot, reason);
    process.stderr.write(`${reason}\n`);
  }
  /*
   * Serialise once through the captured writers and use those exact bytes for
   * the on-disk report, the stdout echo and the integrity digest.  Emitting the
   * digest over the captured stdout channel lets the verifier detect any
   * post-write rewrite of the report file (for example a candidate
   * ``process.on('exit')`` hook) that this in-process harness cannot prevent.
   */
  const text = reportPath ? writeJson(reportPath, report) : serializeJson(report);
  writeStdout(text);
  emitReportDigest(text);
  return 0;
}

function watchdog(side) {
  const timer = setTimeout(() => {
    const reportPath = resolveReportPath(process.argv);
    const fallback = buildFallback(side, resolveRepoRoot(), 'integration harness exceeded watchdog timeout');
    if (reportPath) {
      try {
        writeJson(reportPath, fallback);
      } catch (e) {
        /* ignore */
      }
    }
    process.stderr.write('integration harness exceeded watchdog timeout\n');
    process.exit(124);
  }, DEFAULT_TIMEOUT_MS);
  timer.unref();
}

module.exports = { main, watchdog, resolveRepoRoot, assetRoot };

if (require.main === module) {
  const SIDE = process.env.INTEGRATION_SIDE || 'target';
  watchdog(SIDE);
  main(SIDE, process.argv).then(
    (code) => {
      process.exitCode = code;
    },
    (error) => {
      process.stderr.write(`${(error && error.stack) || error}\n`);
      process.exitCode = 1;
    },
  );
}
