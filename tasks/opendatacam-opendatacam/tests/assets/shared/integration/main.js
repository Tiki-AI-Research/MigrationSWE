'use strict';

// INTEGRATION runner core.
//
// `run(side)` executes every dependency batch's shared projection against the
// repository under test and emits one `migration-observations:v6` report that
// contains *all* required observation ids for that side. It never reads the
// authoring-only `dependencies/` or `fragments/` trees: the observation
// inventory is the self-contained `shared/integration/observations.json`.

const fs = require('fs');
const path = require('path');
const {
  assetPath,
  resolveRepoRoot,
  resolveBaselineRoot,
  readJson,
} = require('./lib');
const {
  collectP2P,
  collectF2P1,
  collectF2P2,
  collectF2P3,
  collectDIFF1,
  collectDIFF2,
  scopeLimited,
} = require('./collect');

function loadInventory() {
  const inventory = readJson(assetPath('shared', 'integration', 'observations.json'));
  return inventory.observations;
}

function makeCollector(state) {
  return (id, status, value) => {
    state.set(id, { status: status, value: value === undefined ? null : value });
  };
}

// Synchronous, EAGAIN-safe write on fd 1 so the report cannot interleave with
// late asynchronous server output.
function writeAll(fd, text) {
  const buffer = Buffer.from(text, 'utf8');
  let offset = 0;
  while (offset < buffer.length) {
    try {
      offset += fs.writeSync(fd, buffer, offset, buffer.length - offset);
    } catch (err) {
      if (err.code !== 'EAGAIN') throw err;
    }
  }
}

// The harness command is `source/runner {report}` / `node target/runner.js
// {report}`: the first positional argument is the report *destination*, never
// the repo root. The repository under test is resolved only from the documented
// environment aliases. If the argument happens to be an existing directory it is
// treated as an explicit repo root (used only for manual authoring runs).
function describeArgument(arg) {
  if (!arg) return {};
  try {
    if (fs.statSync(arg).isDirectory()) return { repoRoot: arg };
  } catch (err) {
    // not an existing path -> it is a report destination
  }
  return { reportPath: arg };
}

function writeReport(reportPath, text) {
  if (!reportPath) return;
  const dir = path.dirname(reportPath);
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch (err) {
    // the destination directory usually already exists
  }
  fs.writeFileSync(reportPath, text, 'utf8');
}

function run(side, options) {
  const opts = options || {};
  const inventory = loadInventory();
  const arg = describeArgument(opts.reportPath || process.argv[2]);
  const root = resolveRepoRoot(opts.repoRoot || arg.repoRoot);
  const reportPath = opts.reportPath || arg.reportPath || process.env.MIGRATION_REPORT;
  const baseline = resolveBaselineRoot(opts.baselineRoot);
  // Standalone runs also need to capture scope before the in-process probes.
  const authoredScope = scopeLimited(root, baseline);
  const results = new Map();
  const put = makeCollector(results);

  // The booted framework writes banners to stdout; keep the report channel
  // clean by swallowing in-process stdout for the duration of the run. The
  // report itself is emitted synchronously on fd 1 (bypassing the swallow).
  process.stdout.write = function swallow() { return true; };

  const steps = [
    function p2p() { return Promise.resolve().then(() => collectP2P(side, root, put)); },
    function f2p1() { return Promise.resolve().then(() => collectF2P1(side, root, baseline, put, authoredScope)); },
    function f2p2() { return Promise.resolve().then(() => collectF2P2(side, root, baseline, put, authoredScope)); },
    function f2p3() { return Promise.resolve().then(() => collectF2P3(side, root, put)); },
    function diff1() { return Promise.resolve().then(() => collectDIFF1(side, root, put)); },
    function diff2() { return Promise.resolve().then(() => collectDIFF2(side, root, put)); },
  ];

  return steps
    .reduce((chain, step) => chain.then(step).catch((err) => {
      // A collector that throws before touching an id leaves it to the default
      // INCONCLUSIVE below; failures are surfaced per observation, never fatal.
      process.stderr.write('integration collector error: ' + String((err && err.stack) || err) + '\n');
    }), Promise.resolve())
    .then(() => {
      const observations = inventory.map((entry) => {
        const res = results.get(entry.id);
        const status = res ? res.status : 'INCONCLUSIVE';
        const raw = res ? res.value : { reason: 'collector not executed' };
        return {
          id: entry.id,
          kind: entry.kind,
          status: status,
          value: { type: 'json', value: raw === undefined ? null : raw },
        };
      });
      const report = {
        schema_version: 'migration-observations:v6',
        side: side,
        repo_root: root,
        baseline_root: baseline,
        observations: observations,
      };
      const text = JSON.stringify(report, null, 2) + '\n';
      if (opts.emit !== false) {
        try {
          writeReport(reportPath, text);
        } catch (err) {
          process.stderr.write('could not write report to ' + reportPath + ': '
            + String((err && err.message) || err) + '\n');
        }
        // Also emit on fd 1 (bypassing the swallowed process.stdout) so a
        // stdout-capturing harness observes the report even without {report}.
        writeAll(1, text);
      }
      return report;
    });
}

module.exports = { run, loadInventory };
