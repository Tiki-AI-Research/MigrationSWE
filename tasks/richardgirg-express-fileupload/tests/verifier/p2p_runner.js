'use strict';

/**
 * P2P runner: executes the repository's original mocha suite and reports one
 * record per real test event (pass / fail / pending) without aggregating them.
 *
 * Usage:
 *   node p2p_runner.js --repo <dir> --out <file>
 *
 * The JSON report is the authoritative identity source for the verifier.
 * The runner deliberately has no single-node mode: the frozen suite is executed
 * exactly once and every failure it reports is final, so neither an
 * order-dependent regression nor a candidate that behaves differently when it
 * sees a selective argv can be turned into a pass (QC ADV-P2P-QUARANTINE).
 */

const fs = require('fs');
const path = require('path');

function parseArgs(argv) {
  const args = { repo: process.cwd(), out: null };
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (key === '--repo') args.repo = path.resolve(argv[++i]);
    else if (key === '--out') args.out = path.resolve(argv[++i]);
    else if (key === '--list') args.list = true;
  }
  return args;
}

const args = parseArgs(process.argv);
const repo = args.repo;

function repoRequire(request) {
  return require(require.resolve(request, { paths: [repo, path.join(repo, 'node_modules')] }));
}

function findSpecFiles(dir, prefix) {
  const found = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (err) {
    return found;
  }
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      found.push(...findSpecFiles(path.join(dir, entry.name), rel));
    } else if (entry.isFile() && entry.name.endsWith('.spec.js')) {
      found.push(rel);
    }
  }
  return found;
}

const records = [];
const uncaught = [];
let files = [];
let wroteReport = false;

function emitReport(extra) {
  if (wroteReport) return;
  wroteReport = true;
  const report = Object.assign({
    schema_version: 'migration-p2p-report/v1',
    repo,
    grep: null,
    files,
    uncaught,
    total: records.length,
    records
  }, extra);
  const payload = JSON.stringify(report, null, 2);
  try {
    if (args.out) fs.writeFileSync(args.out, payload + '\n', 'utf8');
    else process.stdout.write(payload + '\n');
  } catch (err) {
    process.stderr.write('p2p_runner: cannot write report: ' + String(err) + '\n');
  }
}

function classify(test) {
  return {
    fullTitle: test.fullTitle(),
    title: test.title,
    file: test.file ? path.relative(repo, test.file).split(path.sep).join('/') : null,
    duration: typeof test.duration === 'number' ? test.duration : null
  };
}

class Collector {
  constructor(runner) {
    runner.on('pass', (test) => {
      records.push(Object.assign(classify(test), { state: 'passed', error: null }));
    });
    runner.on('fail', (test, err) => {
      const base = test && typeof test.fullTitle === 'function'
        ? classify(test)
        : { fullTitle: `<uncaught:${(err && err.message) || 'error'}>`, title: null, file: null, duration: null };
      records.push(Object.assign(base, {
        state: 'failed',
        error: err && err.message ? String(err.message) : String(err)
      }));
    });
    runner.on('pending', (test) => {
      records.push(Object.assign(classify(test), { state: 'skipped', error: null }));
    });
  }
}

function main() {
  let Mocha;
  try {
    Mocha = repoRequire('mocha');
  } catch (err) {
    // The evaluator's own runner dependencies are missing: this is an
    // environment fault, not candidate behaviour, so it is flagged explicitly
    // instead of being reported as a regression.
    emitReport({
      started_at: new Date().toISOString(),
      duration_ms: 0,
      failures: 0,
      infrastructure_error: 'mocha is unavailable: ' + String(err && err.message ? err.message : err)
    });
    process.exit(2);
  }

  const mocha = new Mocha({ reporter: Collector, timeout: 10000, bail: false });

  files = findSpecFiles(path.join(repo, 'test'), 'test');
  for (const file of files) {
    mocha.addFile(path.join(repo, file));
  }

  process.on('uncaughtException', (err) => { uncaught.push(String(err && err.message)); });
  process.on('unhandledRejection', (err) => { uncaught.push('unhandledRejection: ' + String(err && err.message)); });

  const started = Date.now();
  mocha.run((failures) => {
    emitReport({
      started_at: new Date(started).toISOString(),
      duration_ms: Date.now() - started,
      failures
    });
    // Non-zero exit mirrors the mocha outcome; the report is still authoritative
    // and the verifier reconciles the two.
    process.exit(failures > 0 || uncaught.length > 0 ? 1 : 0);
  });
}

// A candidate tree can abort the suite before mocha's callback runs (a syntax
// error or a synchronous throw while loading the spec files exits the process
// silently). Without this guard the run would leave no report at all and the
// gate could not tell a broken candidate from a broken evaluator. The report
// written here carries ``run_error`` (not ``infrastructure_error``), so the gate
// records it as a regression.
process.on('exit', (code) => {
  emitReport({
    started_at: new Date().toISOString(),
    duration_ms: 0,
    failures: 1,
    run_error: 'p2p suite exited (code ' + code + ') before reporting results: '
      + (uncaught.length ? uncaught.join('; ') : 'no runner state')
  });
});

main();
