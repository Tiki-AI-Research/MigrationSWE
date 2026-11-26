import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const assetsRoot = path.resolve(here, '..', '..');
const requireFromHere = createRequire(import.meta.url);

const REPO_ENV_KEYS = ['MIGRATION_REPO_ROOT', 'MIGRATION_REPO', 'REPO_ROOT', 'REPO'];
const REPORT_DEFAULT = '/logs/result.json';

const inventory = JSON.parse(
  fs.readFileSync(path.join(here, 'observations.json'), 'utf8')
);
if (!Array.isArray(inventory)) {
  throw new Error('shared/integration/observations.json must contain an array');
}

const idsForBatch = batchId =>
  inventory.filter(entry => entry.batch_id === batchId).map(entry => entry.id);

const P2P_0001 = idsForBatch('BATCH-P2P-0001');
const P2P_0002 = idsForBatch('BATCH-P2P-0002');
const F2P_0001 = idsForBatch('BATCH-F2P-0001');
const F2P_0002 = idsForBatch('BATCH-F2P-0002');
const F2P_0003 = idsForBatch('BATCH-F2P-0003');
const DIFF_0001 = idsForBatch('BATCH-DIFFERENTIAL-0001');
const DIFF_0002 = idsForBatch('BATCH-DIFFERENTIAL-0002');

const KIND_BY_ID = Object.fromEntries(inventory.map(entry => [entry.id, entry.kind]));
const OWNER_BY_ID = Object.fromEntries(inventory.map(entry => [entry.id, entry.batch_id]));
const ALL_IDS = inventory.map(entry => entry.id);

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function valueOrNull(value) {
  return value === undefined ? null : value;
}

function makeTemporaryFile(prefix) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  return path.join(directory, 'report.json');
}

function sanitize(value, repoRoot) {
  if (value === undefined) {
    return null;
  }
  if (typeof value === 'bigint') {
    return value.toString() + 'n';
  }
  if (typeof value === 'string') {
    if (repoRoot) {
      return value.split(repoRoot).join('<repo>');
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(item => sanitize(item, repoRoot));
  }
  if (value && typeof value === 'object') {
    if (value instanceof Error) {
      return { name: value.constructor.name, message: sanitize(value.message, repoRoot) };
    }
    const result = {};
    for (const key of Object.keys(value)) {
      if (
        key === 'duration_ms' ||
        key === 'durationMs' ||
        key === 'elapsed_ms' ||
        key === 'repo' ||
        key === 'repo_root' ||
        key === 'baseline' ||
        key === 'dependencies'
      ) {
        continue;
      }
      if (value[key] !== undefined) {
        result[key] = sanitize(value[key], repoRoot);
      }
    }
    return result;
  }
  return value;
}

function resolveRepoRoot(explicit) {
  const candidates = [
    explicit,
    ...REPO_ENV_KEYS.map(key => process.env[key]),
    '/workspace/repo'
  ];
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(path.join(candidate, 'package.json'))) {
      return path.resolve(candidate);
    }
  }
  return path.resolve(explicit || process.env.MIGRATION_REPO_ROOT || '/workspace/repo');
}

function resolveDepsDir(repoRoot) {
  const candidates = [
    path.join(repoRoot, 'node_modules'),
    process.env.MIGRATION_DEPENDENCIES,
    '/opt/migration/dependencies/node_modules'
  ];
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(path.join(candidate, 'zod', 'package.json'))) {
      return path.resolve(candidate);
    }
  }
  return path.join(repoRoot, 'node_modules');
}

function readZodVersion(repoRoot, depsDir) {
  const candidates = [
    path.join(repoRoot, 'node_modules', 'zod', 'package.json'),
    path.join(depsDir, 'zod', 'package.json')
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      try {
        return readJson(candidate).version;
      } catch (error) {
        void error;
      }
    }
  }
  return 'unknown';
}

function detectTechnology(repoRoot, depsDir) {
  const zodVersion = readZodVersion(repoRoot, depsDir);
  let safeExtend = false;
  let meta = false;
  let z = null;
  try {
    const zodRoot = fs.existsSync(path.join(repoRoot, 'node_modules', 'zod', 'package.json'))
      ? path.join(repoRoot, 'node_modules', 'zod')
      : path.join(depsDir, 'zod');
    const requireRepo = createRequire(path.join(repoRoot, 'package.json'));
    z = requireRepo(zodRoot);
    safeExtend = typeof z.object({}).safeExtend === 'function';
    meta = typeof z.string().meta === 'function';
  } catch (error) {
    void error;
  }
  return {
    name: 'Zod',
    loaded_version: zodVersion,
    required_version: '4.1.0',
    satisfied: zodVersion === '4.1.0' && safeExtend && meta,
    safeExtend,
    meta
  };
}

function localBin(repoRoot, depsDir, name) {
  const candidates = [
    path.join(depsDir, '.bin', name),
    path.join(repoRoot, 'node_modules', '.bin', name)
  ];
  return candidates.find(candidate => fs.existsSync(candidate)) || null;
}

function runCommand(repoRoot, depsDir, command, args, options = {}) {
  return spawnSync(command, args, {
    cwd: repoRoot,
    encoding: 'utf8',
    timeout: options.timeout || 1200000,
    maxBuffer: 64 * 1024 * 1024,
    env: Object.assign({}, process.env, {
      CI: '1',
      NODE_PATH: [depsDir, path.join(repoRoot, 'node_modules')].join(path.delimiter)
    }, options.env || {})
  });
}

function runNode(repoRoot, depsDir, script, args, options = {}) {
  return runCommand(repoRoot, depsDir, process.execPath, [script, ...args], options);
}

function ensureBuild(repoRoot, depsDir) {
  const cjs = path.join(repoRoot, 'dist', 'index.cjs');
  const mjs = path.join(repoRoot, 'dist', 'index.mjs');
  if (fs.existsSync(cjs) || fs.existsSync(mjs)) {
    return { ok: true, error: null };
  }
  const rollup = localBin(repoRoot, depsDir, 'rollup');
  const result = rollup
    ? runCommand(repoRoot, depsDir, rollup, ['-c'], { timeout: 300000 })
    : runCommand(repoRoot, depsDir, 'npm', ['run', 'build'], { timeout: 300000 });
  const ok = !result.error && (fs.existsSync(cjs) || fs.existsSync(mjs));
  return {
    ok,
    error: ok
      ? null
      : 'build failed: ' +
        String((result.error && result.error.message) || result.stderr || result.stdout || '').slice(-2000)
  };
}

function fill(map, ids, status, valueBuilder) {
  for (const id of ids) {
    map[id] = {
      id,
      kind: KIND_BY_ID[id],
      status,
      value: {
        type: 'json',
        value: valueBuilder(id)
      }
    };
  }
}

function record(map, id, status, value) {
  map[id] = {
    id,
    kind: KIND_BY_ID[id],
    status,
    value: {
      type: 'json',
      value: valueOrNull(value)
    }
  };
}

function collectP2P0001(context) {
  const script = path.join(assetsRoot, context.side, 'batches', 'BATCH-P2P-0001', 'run-p2p.mjs');
  const output = makeTemporaryFile('integration-p2p-0001-');
  const result = runNode(context.repoRoot, context.depsDir, script, [output], {
    env: {
      MIGRATION_REPO_ROOT: context.repoRoot,
      REPO_ROOT: context.repoRoot,
      ASSETS_ROOT: assetsRoot
    }
  });
  let report = null;
  try {
    report = readJson(output);
  } catch (error) {
    return { report: null, error: String((result.error && result.error.message) || result.stderr || error) };
  }
  return { report, error: null };
}

function collectP2P0002(context) {
  const script = path.join(assetsRoot, 'shared', 'batches', 'BATCH-P2P-0002', 'run-p2p.mjs');
  const output = makeTemporaryFile('integration-p2p-0002-');
  const baseline = process.env.MIGRATION_BASELINE_ROOT || context.repoRoot;
  const result = runNode(
    context.repoRoot,
    context.depsDir,
    script,
    ['--repo', context.repoRoot, '--deps', context.depsDir, '--baseline', baseline, '--out', output],
    { timeout: 1200000 }
  );
  let report = null;
  try {
    report = readJson(output);
  } catch (error) {
    return { report: null, error: String((result.error && result.error.message) || result.stderr || error) };
  }
  return { report, error: null };
}

function collectF2P0002(context) {
  const script = path.join(
    assetsRoot,
    context.side,
    'batches',
    'BATCH-F2P-0002',
    'probe.cjs'
  );
  const output = makeTemporaryFile('integration-f2p-0002-');
  const result = runNode(
    context.repoRoot,
    context.depsDir,
    script,
    ['--repo', context.repoRoot, '--out', output],
    { timeout: 1800000 }
  );
  let report = null;
  try {
    report = readJson(output);
  } catch (error) {
    return { report: null, error: String((result.error && result.error.message) || result.stderr || error) };
  }
  return { report, error: null };
}

function collectF2P0003(context) {
  const script = path.join(
    assetsRoot,
    context.side,
    'batches',
    'BATCH-F2P-0003',
    'runner.mjs'
  );
  const output = makeTemporaryFile('integration-f2p-0003-');
  const result = runNode(
    context.repoRoot,
    context.depsDir,
    script,
    ['--repo', context.repoRoot, '--report', output],
    { timeout: 1800000 }
  );
  let report = null;
  try {
    report = readJson(output);
  } catch (error) {
    return { report: null, error: String((result.error && result.error.message) || result.stderr || error) };
  }
  return { report, error: null };
}

function collectDiff0001(context) {
  const script = path.join(
    assetsRoot,
    context.side,
    'batches',
    'BATCH-DIFFERENTIAL-0001',
    'probe.mjs'
  );
  const output = makeTemporaryFile('integration-diff-0001-');
  const result = runNode(context.repoRoot, context.depsDir, script, [output], {
    env: {
      MIGRATION_REPO_ROOT: context.repoRoot,
      REPO_ROOT: context.repoRoot,
      ASSETS_ROOT: assetsRoot
    }
  });
  let report = null;
  try {
    report = readJson(output);
  } catch (error) {
    return { report: null, error: String((result.error && result.error.message) || result.stderr || error) };
  }
  return { report, error: null };
}

function collectDiff0002(context) {
  const script = path.join(
    assetsRoot,
    context.side,
    'batches',
    'BATCH-DIFFERENTIAL-0002',
    'probe.mjs'
  );
  const result = runNode(
    context.repoRoot,
    context.depsDir,
    script,
    [context.repoRoot, assetsRoot],
    { timeout: 600000 }
  );
  try {
    return { report: JSON.parse(result.stdout), error: null };
  } catch (error) {
    return {
      report: null,
      error: String((result.error && result.error.message) || result.stderr || error)
    };
  }
}

function collectF2P0001(context) {
  const module = requireFromHere('./f2p-0001.cjs');
  return module.run({
    repoRoot: context.repoRoot,
    depsDir: context.depsDir,
    assetsRoot,
    side: context.side,
    technology: context.technology
  });
}

export function collectObservations(options) {
  const repoRoot = resolveRepoRoot(options.repoRoot);
  const depsDir = resolveDepsDir(repoRoot);
  const technology = detectTechnology(repoRoot, depsDir);
  const context = {
    side: options.side,
    repoRoot,
    depsDir,
    technology
  };
  const observations = {};

  fill(observations, ALL_IDS, 'INFRA_ERROR', id => ({
    error: 'observation not executed',
    owner: OWNER_BY_ID[id]
  }));

  const build = ensureBuild(repoRoot, depsDir);
  if (!build.ok) {
    fill(observations, ALL_IDS, 'INFRA_ERROR', id => ({
      error: build.error,
      owner: OWNER_BY_ID[id]
    }));
  }

  const p2p1 = collectP2P0001(context);
  if (p2p1.report && p2p1.report.observations) {
    for (const id of P2P_0001) {
      const entry = p2p1.report.observations[id];
      if (entry) {
        record(
          observations,
          id,
          entry.status === 'PASS' ? 'PASS' : entry.status || 'FAIL',
          sanitize(entry.projection ?? entry.value, repoRoot)
        );
      } else {
        record(observations, id, 'INFRA_ERROR', { error: p2p1.error || 'observation missing from P2P-0001 report' });
      }
    }
  } else {
    fill(observations, P2P_0001, 'INFRA_ERROR', id => ({
      error: p2p1.error || 'P2P-0001 report unavailable',
      owner: OWNER_BY_ID[id]
    }));
  }

  const p2p2 = collectP2P0002(context);
  if (p2p2.report && p2p2.report.observations) {
    for (const id of P2P_0002) {
      const entry = p2p2.report.observations[id];
      if (entry) {
        record(observations, id, entry.status === 'PASS' ? 'PASS' : entry.status || 'FAIL', sanitize(entry.projection, repoRoot));
      } else {
        record(observations, id, 'INFRA_ERROR', { error: p2p2.error || 'observation missing from P2P-0002 report' });
      }
    }
  } else {
    fill(observations, P2P_0002, 'INFRA_ERROR', id => ({
      error: p2p2.error || 'P2P-0002 report unavailable',
      owner: OWNER_BY_ID[id]
    }));
  }

  const f2p1 = collectF2P0001(context);
  for (const entry of f2p1) {
    record(
      observations,
      entry.id,
      entry.status,
      sanitize(entry.value, repoRoot)
    );
  }

  if (!technology.satisfied) {
    fill(observations, [...F2P_0002, ...F2P_0003], 'FAIL', id => ({
      error: 'required target technology Zod 4.1.0 is not loaded; loaded Zod ' + technology.loaded_version,
      technology
    }));
  } else {
    const f2p2 = collectF2P0002(context);
    if (f2p2.report && Array.isArray(f2p2.report.observations)) {
      const byId = new Map(f2p2.report.observations.map(entry => [entry.observation_id, entry]));
      for (const id of F2P_0002) {
        const entry = byId.get(id);
        if (entry) {
          record(observations, id, entry.status, sanitize({
            behavior: entry.behavior,
            entry: entry.entry,
            technology: entry.technology,
            detail: entry.detail
          }, repoRoot));
        } else {
          record(observations, id, 'INFRA_ERROR', { error: 'observation missing from F2P-0002 report' });
        }
      }
    } else {
      fill(observations, F2P_0002, 'INFRA_ERROR', id => ({
        error: f2p2.error || 'F2P-0002 report unavailable',
        owner: OWNER_BY_ID[id]
      }));
    }

    const f2p3 = collectF2P0003(context);
    if (f2p3.report && Array.isArray(f2p3.report.results)) {
      const byId = new Map(f2p3.report.results.map(entry => [entry.observation_id, entry]));
      for (const id of F2P_0003) {
        const entry = byId.get(id);
        if (entry) {
          record(observations, id, entry.status, sanitize({
            evidence: entry.evidence,
            message: entry.message
          }, repoRoot));
        } else {
          record(observations, id, 'INFRA_ERROR', { error: 'observation missing from F2P-0003 report' });
        }
      }
    } else {
      fill(observations, F2P_0003, 'INFRA_ERROR', id => ({
        error: f2p3.error || 'F2P-0003 report unavailable',
        owner: OWNER_BY_ID[id]
      }));
    }
  }

  const diff1 = collectDiff0001(context);
  if (diff1.report && diff1.report.results) {
    for (const id of DIFF_0001) {
      const entry = diff1.report.results[id];
      if (entry) {
        record(
          observations,
          id,
          entry.pass ? 'PASS' : 'FAIL',
          sanitize(entry.pass ? entry.normalized : { error: entry.error }, repoRoot)
        );
      } else {
        record(observations, id, 'INFRA_ERROR', { error: diff1.error || 'observation missing from DIFF-0001 report' });
      }
    }
  } else {
    fill(observations, DIFF_0001, 'INFRA_ERROR', id => ({
      error: diff1.error || 'DIFF-0001 report unavailable',
      owner: OWNER_BY_ID[id]
    }));
  }

  const diff2 = collectDiff0002(context);
  if (diff2.report && diff2.report.observations) {
    for (const id of DIFF_0002) {
      const entry = diff2.report.observations[id];
      if (entry) {
        record(observations, id, entry.ok ? 'PASS' : 'FAIL', sanitize(entry, repoRoot));
      } else {
        record(observations, id, 'INFRA_ERROR', { error: diff2.error || 'observation missing from DIFF-0002 report' });
      }
    }
  } else {
    fill(observations, DIFF_0002, 'INFRA_ERROR', id => ({
      error: diff2.error || 'DIFF-0002 report unavailable',
      owner: OWNER_BY_ID[id]
    }));
  }

  return {
    schema_version: 'migration-observations:v6',
    observations: ALL_IDS.map(id => observations[id])
  };
}

export async function runMain(options = {}) {
  const side = options.side || 'target';
  const argv = process.argv.slice(2);
  let repoArg = null;
  let reportArg = null;
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--repo') {
      repoArg = argv[index + 1];
      index += 1;
    } else if (arg === '--report') {
      reportArg = argv[index + 1];
      index += 1;
    } else if (!reportArg && /\.json$/i.test(arg)) {
      reportArg = arg;
    }
  }

  const reportPath = path.resolve(
    process.env.REPORT_PATH ||
      reportArg ||
      REPORT_DEFAULT
  );
  const repoRoot = resolveRepoRoot(repoArg);

  let report;
  try {
    report = collectObservations({ repoRoot, side });
  } catch (error) {
    report = {
      schema_version: 'migration-observations:v6',
      observations: ALL_IDS.map(id => ({
        id,
        kind: KIND_BY_ID[id],
        status: 'INFRA_ERROR',
        value: {
          type: 'json',
          value: {
            error: String((error && error.stack) || error),
            owner: OWNER_BY_ID[id]
          }
        }
      }))
    };
  }

  fs.mkdirSync(path.dirname(reportPath), { recursive: true });
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2) + '\n');
  process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  return report;
}
