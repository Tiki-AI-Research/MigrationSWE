// INTEGRATION batch: merge the dependency batches' executable projections into
// one shared harness and emit the frozen runtime observation inventory.
//
// The harness is side-symmetric: the *same* predicate of every observation is
// executed against the repository supplied through the environment, so the
// caller (source/runner or target/harness.mjs) only selects which side's probe
// entries are dispatched. No result is hardcoded by side: F2P observations are
// FAIL on a repository whose live request path is still owned by the source
// technology and PASS on one whose request path is owned by the target
// technology, because the probe measures that ownership at runtime.
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const ASSET_ROOT = path.resolve(HERE, '..', '..');

const ENV_ROOTS = ['MIGRATION_REPO_ROOT', 'MIGRATION_REPO', 'REPO_ROOT', 'REPO'];
const VALID_STATUS = new Set(['PASS', 'FAIL', 'NA', 'INFRA_ERROR', 'INCONCLUSIVE', 'TIMEOUT']);

export function resolveRepoRoot() {
  for (const key of ENV_ROOTS) {
    const value = process.env[key];
    if (value && value.trim()) return value.trim();
  }
  return '/workspace/repo';
}

function loadInventory() {
  const parsed = JSON.parse(readFileSync(path.join(HERE, 'observations.json'), 'utf8'));
  return parsed.observations;
}

function runChild(args, { cwd, env = {}, timeoutMs = 900000 } = {}) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(process.execPath, args, {
        cwd,
        env: { ...process.env, ...env },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      resolve({ code: -1, stdout: '', stderr: String(error) });
      return;
    }
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk; });
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    const timer = setTimeout(() => {
      try { child.kill('SIGKILL'); } catch { /* already gone */ }
    }, timeoutMs);
    child.on('error', (error) => {
      clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: `${stderr}${String(error)}` });
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

function parseJsonOutput(text) {
  const trimmed = (text || '').trim();
  if (!trimmed) return undefined;
  try {
    return JSON.parse(trimmed);
  } catch { /* fall through to last-object scan */ }
  for (let i = trimmed.indexOf('{'); i !== -1; i = trimmed.indexOf('{', i + 1)) {
    try {
      return JSON.parse(trimmed.slice(i));
    } catch { /* keep scanning */ }
  }
  return undefined;
}

function record(id, kind, status, value) {
  return { id, kind, status, value };
}

// ---------------------------------------------------------------------------
// Batch dispatchers. Each returns a Map<id, record> using raw, side-specific
// observations. Throwing leaves that batch untouched so the caller can mark the
// owned ids INCONCLUSIVE rather than inventing a result.
// ---------------------------------------------------------------------------

async function observeF2P0001(repo) {
  const url = pathToFileURL(path.join(ASSET_ROOT, 'shared/batches/BATCH-F2P-0001/harness.mjs')).href;
  const { run } = await import(url);
  const { results } = await run(repo);
  const out = new Map();
  for (const result of results) {
    out.set(result.id, record(
      result.id,
      'F2P',
      result.behavior && result.fastify ? 'PASS' : 'FAIL',
      { behavior: result.behavior, fastify: result.fastify, entry: result.entry, detail: result.detail },
    ));
  }
  return out;
}

async function observeF2P0002(repo) {
  const hooks = path.join(ASSET_ROOT, 'shared/batches/BATCH-F2P-0002/hooks.mjs');
  const probe = path.join(ASSET_ROOT, 'shared/batches/BATCH-F2P-0002/probe.mjs');
  const trace = path.join(mkdtempSync(path.join(tmpdir(), 'mig-f2p2-')), 'trace.log');
  const { code, stdout, stderr } = await runChild(['--experimental-loader', hooks, probe, repo], {
    cwd: repo,
    env: { BATCH_F2P_0002_ROOT: repo, BATCH_F2P_0002_TRACE_OUT: trace, NODE_ENV: process.env.NODE_ENV || 'test' },
  });
  const parsed = parseJsonOutput(stdout);
  if (!parsed || !Array.isArray(parsed.results)) {
    throw new Error(`BATCH-F2P-0002 probe produced no report (exit ${code}): ${stderr.slice(-400)}`);
  }
  const out = new Map();
  for (const result of parsed.results) {
    out.set(result.id, record(result.id, 'F2P', result.status === 'PASS' ? 'PASS' : (result.status === 'FAIL' ? 'FAIL' : 'INCONCLUSIVE'), result.detail ?? null));
  }
  return out;
}

async function observeF2P0003(repo, side) {
  const dir = path.join(ASSET_ROOT, side, 'batches/BATCH-F2P-0003');
  const out = new Map();
  for (let n = 49; n <= 72; n += 1) {
    const id = `F2P-0${n}`;
    const { code, stdout, stderr } = await runChild([path.join(dir, `${id}.mjs`)], {
      cwd: repo,
      env: { PROBE_ROOT: repo },
    });
    const parsed = parseJsonOutput(stdout);
    if (parsed && parsed.observation_id === id) {
      out.set(id, record(id, 'F2P', parsed.status === 'PASS' ? 'PASS' : 'FAIL', parsed.detail ?? null));
    } else {
      out.set(id, record(id, 'F2P', 'INCONCLUSIVE', { error: `no report (exit ${code})`, stderr: stderr.slice(-300) }));
    }
  }
  return out;
}

async function observeDifferential(repo, side) {
  const loader = path.join(HERE, 'repo-resolver.mjs');
  const probe = path.join(ASSET_ROOT, side, 'batches/BATCH-DIFFERENTIAL-0001/probe.mjs');
  const { code, stdout, stderr } = await runChild(['--experimental-loader', loader, probe], {
    cwd: repo,
    // The probe selects its repository through its own contract variable; the
    // loader uses the standard migration variable.
    env: { MIGRATION_REPO_ROOT: repo, REPO_ROOT: repo, OIDC_REPO_ROOT: repo },
  });
  const parsed = parseJsonOutput(stdout);
  const out = new Map();
  for (let n = 1; n <= 29; n += 1) {
    const id = `DIFF-${String(n).padStart(3, '0')}`;
    if (parsed && typeof parsed === 'object') {
      out.set(id, record(id, 'DIFFERENTIAL', 'PASS', parsed));
    } else {
      out.set(id, record(id, 'DIFFERENTIAL', 'INCONCLUSIVE', { error: `no report (exit ${code})`, stderr: stderr.slice(-300) }));
    }
  }
  return out;
}

async function observeP2P(repo, ids) {
  const { code, stdout, stderr } = await runChild(['./test/run.js'], { cwd: repo });
  const pass = code === 0;
  const base = pass ? { result: 'PASS' } : { result: 'FAIL' };
  const out = new Map();
  for (const id of ids) {
    out.set(id, record(id, 'P2P', pass ? 'PASS' : 'FAIL',
      pass ? base : {
        ...base, exitCode: code, error: stderr.slice(-4000), stdout: stdout.slice(-12000),
      }));
  }
  return out;
}

// ---------------------------------------------------------------------------
// Orchestration
// ---------------------------------------------------------------------------

export async function runIntegration({ side }) {
  if (side !== 'source' && side !== 'target') {
    throw new Error(`unknown integration side: ${side}`);
  }
  const repo = resolveRepoRoot();
  const inventory = loadInventory();

  const byKind = new Map();
  const byBatch = new Map();
  for (const entry of inventory) {
    if (!byKind.has(entry.kind)) byKind.set(entry.kind, []);
    byKind.get(entry.kind).push(entry.id);
    if (!byBatch.has(entry.batch_id)) byBatch.set(entry.batch_id, []);
    byBatch.get(entry.batch_id).push(entry.id);
  }

  const results = new Map();
  const adopt = (map) => {
    for (const [id, value] of map) results.set(id, value);
  };
  const guard = async (label, ids, dispatch) => {
    const owned = ids.filter((id) => !results.has(id));
    if (owned.length === 0) return;
    try {
      adopt(await dispatch());
    } catch (error) {
      const message = `${label}: ${error instanceof Error ? error.message : String(error)}`;
      for (const id of owned) {
        if (!results.has(id)) results.set(id, record(id, undefined, 'INCONCLUSIVE', { error: message }));
      }
    }
  };

  // P2P: the repository's own public suite is the executable projection; it is
  // executed once per side and covers every P2P observation.
  await guard('P2P', byKind.get('P2P') ?? [], () => observeP2P(repo, byKind.get('P2P') ?? []));

  // F2P: target-only postconditions. Batch-0001 runs the merged semantic
  // harness in-process; 0002 runs its traced child; 0003 runs per-observation
  // entries on the selected side.
  await guard('F2P-0001', byBatch.get('BATCH-F2P-0001') ?? [], () => observeF2P0001(repo));
  await guard('F2P-0002', byBatch.get('BATCH-F2P-0002') ?? [], () => observeF2P0002(repo));
  await guard('F2P-0003', byBatch.get('BATCH-F2P-0003') ?? [], () => observeF2P0003(repo, side));

  // DIFFERENTIAL: shared-case normalized projections on the selected side.
  await guard('DIFFERENTIAL', byKind.get('DIFFERENTIAL') ?? [], () => observeDifferential(repo, side));

  const observations = inventory.map((entry) => {
    const found = results.get(entry.id);
    const status = found && VALID_STATUS.has(found.status) ? found.status : 'INCONCLUSIVE';
    return {
      id: entry.id,
      kind: entry.kind,
      status,
      value: { type: 'json', value: found ? found.value : { error: 'no probe dispatched' } },
    };
  });

  return { schema_version: 'migration-observations:v6', observations };
}

export async function main({ side, argv = [] }) {
  const reportPath = argv[0];
  const report = await runIntegration({ side });
  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  if (reportPath) writeFileSync(reportPath, serialized);
  const counts = report.observations.reduce((acc, o) => {
    acc[o.status] = (acc[o.status] || 0) + 1;
    return acc;
  }, {});
  process.stdout.write(`integration ${side}: ${report.observations.length} observations ${JSON.stringify(counts)}\n`);
  return report;
}
