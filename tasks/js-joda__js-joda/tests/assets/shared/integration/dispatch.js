'use strict';

/*
 * BATCH-INTEGRATION-0001 observation dispatcher.
 *
 * The dependency batches each own a part of the frozen runtime observation
 * inventory and expose an executable projection (a shared module with a
 * documented entry function, or a self-contained harness).  This module
 * dispatches every required_runtime_observation to its owning batch exactly
 * once, on the requested side, and returns the raw observed status.  No status
 * is derived from the side label: the very same probe code runs for source and
 * target and naturally yields FAIL on the pre-upgrade source.
 */

const fs = require('fs');
const path = require('path');
const { assetRoot, setupRuntime, resetModuleState, runBounded } = require('./runtime');

const INVENTORY_PATH = path.join(__dirname, 'observations.json');

const ALLOWED_STATUS = new Set([
  'PASS',
  'FAIL',
  'NA',
  'INFRA_ERROR',
  'INCONCLUSIVE',
  'TIMEOUT',
]);

const BATCH_DIRS = {
  'BATCH-P2P-0001': path.join('shared', 'batches', 'BATCH-P2P-0001'),
  'BATCH-P2P-0002': path.join('shared', 'batches', 'BATCH-P2P-0002'),
  'BATCH-F2P-0001': path.join('shared', 'batches', 'BATCH-F2P-0001'),
  'BATCH-F2P-0002': path.join('shared', 'batches', 'BATCH-F2P-0002'),
  'BATCH-F2P-0003': path.join('shared', 'batches', 'BATCH-F2P-0003'),
  'BATCH-DIFFERENTIAL-0001': path.join('shared', 'batches', 'BATCH-DIFFERENTIAL-0001'),
  'BATCH-CONTRACT-0001': path.join('shared', 'batches', 'BATCH-CONTRACT-0001'),
};

function loadInventory() {
  return JSON.parse(fs.readFileSync(INVENTORY_PATH, 'utf8')).observations;
}

function statusOf(value) {
  if (typeof value === 'string' && ALLOWED_STATUS.has(value)) {
    return value;
  }
  if (value === true) {
    return 'PASS';
  }
  if (value === false) {
    return 'FAIL';
  }
  return 'INCONCLUSIVE';
}

function sanitize(value) {
  if (value === undefined) {
    return null;
  }
  try {
    return JSON.parse(JSON.stringify(value));
  } catch (e) {
    return String(value);
  }
}

function loadBatchModule(batchId, relative) {
  const file = path.join(assetRoot(), BATCH_DIRS[batchId], relative);
  return require(file);
}

function inconclusiveAll(ids, reason, error) {
  const map = {};
  for (const id of ids) {
    map[id] = {
      status: 'INCONCLUSIVE',
      value: { reason, error: error ? String(error) : undefined },
    };
  }
  return map;
}

function mapResults(records, idOf, statusOfRecord, valueOfRecord) {
  const map = {};
  for (const record of records || []) {
    const id = idOf(record);
    if (!id) {
      continue;
    }
    map[id] = {
      status: statusOf(statusOfRecord(record)),
      value: sanitize(valueOfRecord(record)),
    };
  }
  return map;
}

// --- per-batch executable projections ---------------------------------------

function runP2P0001(side, repoRoot, ids) {
  const harness = path.join(assetRoot(), BATCH_DIRS['BATCH-P2P-0001'], 'p2p_harness.js');
  if (!fs.existsSync(harness)) {
    return inconclusiveAll(ids, 'BATCH-P2P-0001 harness unavailable');
  }
  const out = path.join('/tmp', `integration-p2p0001-${side}-${process.pid}.json`);
  const bootstrap = path.join(__dirname, 'bootstrap.js');
  const nodeOptions = [process.env.NODE_OPTIONS || '', `--require ${bootstrap}`].join(' ').trim();
  const env = {
    REPO_ROOT: repoRoot,
    NODE_OPTIONS: nodeOptions,
    NODE_ENV: 'test',
  };
  const result = runBounded(
    ['node', harness, '--side', side, '--repo', repoRoot, '--out', out],
    { cwd: repoRoot, env },
    1200,
  );
  let report = null;
  try {
    report = JSON.parse(fs.readFileSync(out, 'utf8'));
  } catch (e) {
    report = require('./runtime').extractJson(result.stdout);
  }
  try {
    fs.unlinkSync(out);
  } catch (e) {
    /* ignore */
  }
  if (!report || !Array.isArray(report.checks)) {
    return inconclusiveAll(
      ids,
      result.status === 124 || result.signal ? 'BATCH-P2P-0001 timed out' : 'BATCH-P2P-0001 produced no report',
      result.error ? result.error.message : null,
    );
  }
  return mapResults(
    report.checks,
    (record) => record.id,
    (record) => record.status,
    (record) => record.value,
  );
}

function runP2P0002(side, repoRoot) {
  const checks = loadBatchModule('BATCH-P2P-0002', 'p2p-checks.js');
  const report = checks.runAll(repoRoot, side);
  return mapResults(
    report.observations,
    (record) => record.observation_id,
    (record) => record.result,
    (record) => record.value,
  );
}

function runF2P0001(side, repoRoot) {
  const probes = loadBatchModule('BATCH-F2P-0001', 'f2p-probes.js');
  const observations = probes.evaluateAll(repoRoot);
  return mapResults(
    observations,
    (record) => record.observation_id,
    (record) => record.status,
    (record) => ({ status: record.status, detail: sanitize(record.detail) }),
  );
}

function runF2P0002(side, repoRoot) {
  const batch = require(path.join(assetRoot(), BATCH_DIRS['BATCH-F2P-0002']));
  const report = batch.createSide(side).buildChecks(repoRoot);
  return mapResults(
    report,
    (record) => record.observation_id,
    (record) => record.status,
    (record) => ({ status: record.status }),
  );
}

async function runF2P0003(side, repoRoot) {
  const support = loadBatchModule('BATCH-F2P-0003', 'support.js');
  const probes = loadBatchModule('BATCH-F2P-0003', 'probes.js');
  const ctx = support.createContext(repoRoot);
  const observations = await support.runAll(ctx, probes.OBSERVATIONS);
  return mapResults(
    observations,
    (record) => record.observation_id,
    (record) => record.status,
    (record) => ({ status: record.status, detail: sanitize(record.detail) }),
  );
}

function runDifferential(side, repoRoot) {
  const corpus = loadBatchModule('BATCH-DIFFERENTIAL-0001', 'corpus.js');
  const execute = side === 'target' && process.env.INTEGRATION_VERIFY_COMMANDS === '1';
  const report = corpus.runCorpus(repoRoot, { execute });
  const map = {};
  for (const id of Object.keys(report.observations)) {
    const record = report.observations[id];
    map[id] = {
      status: statusOf(record.passed === false ? 'FAIL' : 'PASS'),
      value: sanitize(record.value),
    };
  }
  return map;
}

function runContract(side, repoRoot) {
  const probe = loadBatchModule('BATCH-CONTRACT-0001', 'contract-probe.js');
  const report = probe.runProbe(side, repoRoot);
  const map = {};
  for (const id of Object.keys(report.results)) {
    const record = report.results[id];
    map[id] = {
      status: statusOf(record.status),
      value: sanitize(record.value),
    };
  }
  return map;
}

const RUNNERS = {
  'BATCH-P2P-0001': (side, repoRoot, ids) => runP2P0001(side, repoRoot, ids),
  'BATCH-P2P-0002': (side, repoRoot) => runP2P0002(side, repoRoot),
  'BATCH-F2P-0001': (side, repoRoot) => runF2P0001(side, repoRoot),
  'BATCH-F2P-0002': (side, repoRoot) => runF2P0002(side, repoRoot),
  'BATCH-F2P-0003': (side, repoRoot) => runF2P0003(side, repoRoot),
  'BATCH-DIFFERENTIAL-0001': (side, repoRoot) => runDifferential(side, repoRoot),
  'BATCH-CONTRACT-0001': (side, repoRoot) => runContract(side, repoRoot),
};

/*
 * Runs the complete inventory for one side and returns one entry per
 * observation, in inventory order.
 */
async function runSide(side, repoRoot) {
  setupRuntime(repoRoot);
  const inventory = loadInventory();
  const byBatch = {};
  for (const item of inventory) {
    if (!byBatch[item.batch_id]) {
      byBatch[item.batch_id] = [];
    }
    byBatch[item.batch_id].push(item.id);
  }

  const results = Object.create(null);
  const diagnostics = [];

  for (const batchId of Object.keys(RUNNERS)) {
    const ids = byBatch[batchId];
    if (!ids || ids.length === 0) {
      continue;
    }
    resetModuleState(repoRoot);
    try {
      const map = await Promise.resolve(RUNNERS[batchId](side, repoRoot, ids));
      for (const id of ids) {
        results[id] = map[id] || {
          status: 'INCONCLUSIVE',
          value: { reason: `observation not reported by ${batchId}` },
        };
      }
    } catch (error) {
      diagnostics.push({ batch_id: batchId, error: String(error && error.message ? error.message : error) });
      const fallback = inconclusiveAll(
        ids,
        `batch dispatch failed: ${batchId}`,
        error && error.message ? error.message : error,
      );
      for (const id of ids) {
        results[id] = fallback[id];
      }
    }
  }

  return {
    observations: inventory.map((item) => {
      const result = results[item.id] || { status: 'INCONCLUSIVE', value: null };
      return {
        id: item.id,
        kind: item.kind,
        status: statusOf(result.status),
        value: { type: 'json', value: sanitize(result.value) },
      };
    }),
    diagnostics,
  };
}

module.exports = {
  loadInventory,
  runSide,
  ALLOWED_STATUS,
};
