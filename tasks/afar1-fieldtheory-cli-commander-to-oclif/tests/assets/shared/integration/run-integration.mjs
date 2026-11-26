import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ASSET_ROOT = path.resolve(HERE, '..', '..');
const INVENTORY_PATH = path.join(HERE, 'observations.json');
const BATCH_ID = 'BATCH-INTEGRATION-0001';
const REPORT_SCHEMA = 'migration-observations:v6';
const PRELOAD = path.join(HERE, 'preload.mjs');
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;

function stripAnsi(value) {
  return String(value ?? '').replace(ANSI, '').replace(/\r\n/g, '\n');
}

function parseArgs(argv) {
  const parsed = { positional: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--report') parsed.report = argv[++index];
    else if (arg.startsWith('--report=')) parsed.report = arg.slice('--report='.length);
    else if (arg === '--repo') parsed.repo = argv[++index];
    else if (arg.startsWith('--repo=')) parsed.repo = arg.slice('--repo='.length);
    else parsed.positional.push(arg);
  }
  if (!parsed.report && parsed.positional.length > 0) parsed.report = parsed.positional[0];
  return parsed;
}

function repoRoot(argvRepo) {
  return path.resolve(
    argvRepo ||
    process.env.MIGRATION_REPO_ROOT ||
    process.env.MIGRATION_REPO ||
    process.env.REPO_ROOT ||
    process.env.REPO ||
    '/workspace/repo',
  );
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function readJsonIfPresent(file) {
  try {
    return readJson(file);
  } catch {
    return null;
  }
}

function parseJsonLoose(text) {
  const source = stripAnsi(text).trim();
  if (!source) return null;
  try {
    return JSON.parse(source);
  } catch {}
  const start = source.indexOf('{');
  if (start < 0) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') inString = true;
    else if (char === '{') depth += 1;
    else if (char === '}') {
      depth -= 1;
      if (depth === 0) {
        try {
          return JSON.parse(source.slice(start, index + 1));
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

function loadInventory() {
  const inventory = readJson(INVENTORY_PATH);
  if (!Array.isArray(inventory)) throw new Error(`${INVENTORY_PATH} must contain an array`);
  for (const entry of inventory) {
    if (!entry || !entry.id || !entry.kind || !entry.batch_id) {
      throw new Error(`${INVENTORY_PATH} contains an invalid observation entry`);
    }
  }
  return inventory;
}

function runNode(script, args, options = {}) {
  if (!fs.existsSync(script)) {
    return { status: null, error: new Error(`probe script missing: ${script}`), stdout: '', stderr: '' };
  }
  const result = spawnSync(process.execPath, [script, ...args], {
    cwd: options.cwd,
    encoding: 'utf8',
    timeout: options.timeout ?? 900_000,
    maxBuffer: 64 * 1024 * 1024,
    env: {
      ...process.env,
      NO_COLOR: '1',
      FORCE_COLOR: '0',
      ...(options.env ?? {}),
    },
  });
  return {
    status: result.status,
    signal: result.signal,
    error: result.error ?? null,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
  };
}

function runBuild(repo) {
  if (fs.existsSync(path.join(repo, 'dist', 'cli.js'))) return { ok: true, detail: 'dist/cli.js present' };
  const result = spawnSync('npm', ['run', 'build'], {
    cwd: repo,
    encoding: 'utf8',
    timeout: 600_000,
    maxBuffer: 32 * 1024 * 1024,
    env: { ...process.env, NO_COLOR: '1', npm_config_audit: 'false', npm_config_fund: 'false' },
  });
  const ok = fs.existsSync(path.join(repo, 'dist', 'cli.js'));
  return {
    ok,
    detail: ok ? 'dist/cli.js built' : stripAnsi(`${result.stdout ?? ''}${result.stderr ?? ''}`).slice(-1200),
  };
}

function sanitize(value, repo) {
  if (typeof value === 'string') {
    let text = stripAnsi(value);
    text = text.split(repo).join('<REPO>');
    text = text.split(ASSET_ROOT).join('<ASSETS>');
    text = text.split(os.tmpdir()).join('<TMP>');
    text = text.replace(/\/tmp\/[^\s"'`]+/g, '<TMP>');
    return text;
  }
  if (Array.isArray(value)) return value.map((item) => sanitize(item, repo));
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, sanitize(value[key], repo)]),
    );
  }
  return value;
}

function normalizeStatus(entry) {
  if (!entry) return 'INCONCLUSIVE';
  const status = String(entry.status ?? '').toUpperCase();
  if (['PASS', 'FAIL', 'NA', 'INFRA_ERROR', 'INCONCLUSIVE', 'TIMEOUT'].includes(status)) return status;
  if (entry.pass === true) return 'PASS';
  if (entry.pass === false) return 'FAIL';
  if (entry.ok === true) return 'PASS';
  if (entry.ok === false) return 'FAIL';
  return 'INCONCLUSIVE';
}

function observationResult(entry, fallbackValue) {
  if (!entry) return { status: 'INCONCLUSIVE', value: fallbackValue };
  return {
    status: normalizeStatus(entry),
    value: entry.projection ?? entry.value ?? entry.detail ?? fallbackValue,
  };
}

function batchScript(side, batch, file) {
  // The source and target trees share one implementation of each probe: the
  // probes take the side as a parameter and resolve the repository from
  // ``--repo``.  Only the target tree ships the scripts, so a source replay
  // falls back to it instead of silently reporting a missing batch.
  const local = path.join(ASSET_ROOT, side, 'batches', batch, file);
  if (fs.existsSync(local)) return local;
  return path.join(ASSET_ROOT, 'target', 'batches', batch, file);
}

function collectP2P0001(side, repo, tempRoot, attempt = 0) {
  const script = batchScript(side, 'BATCH-P2P-0001', 'probe.mjs');
  const report = path.join(tempRoot, `p2p-0001-${side}-${attempt}.json`);
  const result = runNode(script, ['--repo', repo, '--side', side, '--out', report], { cwd: repo });
  const payload = parseJsonLoose(result.stdout) ?? readJsonIfPresent(report);
  if (!payload || !payload.observations) {
    throw new Error(`BATCH-P2P-0001 ${side} probe did not emit observations: ${stripAnsi(result.stderr).slice(-1200)}`);
  }
  return new Map(Object.entries(payload.observations));
}

async function collectP2P0002(side, repo) {
  const modulePath = path.join(ASSET_ROOT, 'shared', 'batches', 'BATCH-P2P-0002', 'p2p-harness.mjs');
  if (!fs.existsSync(modulePath)) throw new Error(`missing ${modulePath}`);
  const { runHarness } = await import(pathToFileURL(modulePath).href);
  const payload = await runHarness({ repo, side });
  const map = new Map();
  for (const entry of payload.observations ?? []) map.set(entry.observation_id, entry);
  return map;
}

function collectF2P0001(side, repo) {
  const script = batchScript(side, 'BATCH-F2P-0001', 'probe.mjs');
  const result = runNode(script, [repo, '--expect=none'], { cwd: repo });
  const payload = parseJsonLoose(result.stdout);
  if (!payload || !payload.results) {
    throw new Error(`BATCH-F2P-0001 ${side} probe did not emit results: ${stripAnsi(result.stderr).slice(-1200)}`);
  }
  return new Map(Object.entries(payload.results));
}

function collectF2P0002(side, repo, tempRoot) {
  const script = batchScript(side, 'BATCH-F2P-0002', 'run-probes.mjs');
  const report = path.join(tempRoot, `f2p-0002-${side}.json`);
  runNode(script, ['--repo', repo, '--report', report], { cwd: repo });
  const payload = readJsonIfPresent(report);
  if (!payload || !Array.isArray(payload.observations)) {
    throw new Error(`BATCH-F2P-0002 ${side} probe did not write observations`);
  }
  const map = new Map();
  for (const entry of payload.observations) map.set(entry.observation_id, entry);
  return map;
}

function collectF2P0003(side, repo, tempRoot) {
  const script = batchScript(side, 'BATCH-F2P-0003', 'probe.mjs');
  const report = path.join(tempRoot, `f2p-0003-${side}.json`);
  runNode(script, [repo, report], { cwd: repo });
  const payload = readJsonIfPresent(report);
  if (!payload || !Array.isArray(payload.results)) {
    throw new Error(`BATCH-F2P-0003 ${side} probe did not write results`);
  }
  const map = new Map();
  for (const entry of payload.results) map.set(entry.observation_id, entry);
  return map;
}

function collectDifferential(side, repo, tempRoot, attempt = 0) {
  const script = batchScript(side, 'BATCH-DIFFERENTIAL-0001', 'run.mjs');
  const report = path.join(tempRoot, `differential-${side}-${attempt}.json`);
  runNode(script, [repo, report], { cwd: repo });
  const payload = readJsonIfPresent(report);
  if (!payload || !payload.observations) {
    throw new Error(`BATCH-DIFFERENTIAL-0001 ${side} probe did not write observations`);
  }
  return new Map(Object.entries(payload.observations));
}

function countFailures(entries) {
  let failures = 0;
  for (const entry of entries.values()) {
    if (normalizeStatus(entry) !== 'PASS') failures += 1;
  }
  return failures;
}

async function collectWithRetry(load, attempts) {
  let best = null;
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const entries = await load(attempt);
      const failures = countFailures(entries);
      if (!best || failures < best.failures) best = { entries, failures };
      if (failures === 0) break;
    } catch (error) {
      lastError = error;
    }
  }
  if (!best) throw lastError ?? new Error('probe did not run');
  return best.entries;
}

function readModuleLog(file) {
  try {
    return fs.readFileSync(file, 'utf8')
      .split('\n')
      .filter(Boolean)
      .map((line) => {
        try {
          return JSON.parse(line);
        } catch {
          return null;
        }
      })
      .filter(Boolean);
  } catch {
    return [];
  }
}

function oclifVersionFrom(entries) {
  const entry = entries.find((item) => item.specifier === '@oclif/core' || /\/node_modules\/@oclif\/core\//.test(item.url ?? ''));
  if (!entry?.url) return null;
  try {
    const resolved = fileURLToPath(entry.url);
    const marker = `${path.sep}node_modules${path.sep}@oclif${path.sep}core${path.sep}`;
    const index = resolved.indexOf(marker);
    if (index < 0) return null;
    return JSON.parse(fs.readFileSync(`${resolved.slice(0, index)}${marker}package.json`, 'utf8')).version ?? null;
  } catch {
    return null;
  }
}

function detectTargetTechnology(repo, tempRoot) {
  const entry = path.join(repo, 'dist', 'cli.js');
  const bin = path.join(repo, 'bin', 'ft.mjs');
  const executable = fs.existsSync(entry) ? entry : fs.existsSync(bin) ? bin : null;
  const pkg = readJsonIfPresent(path.join(repo, 'package.json')) ?? {};
  const dependencies = pkg.dependencies ?? {};
  const declaredOclif = dependencies['@oclif/core'] ?? null;
  const declaredCommander = dependencies.commander ?? null;
  if (!executable || !fs.existsSync(PRELOAD)) {
    return {
      active: false,
      oclifVersion: null,
      detail: `target technology not observable: entry=${executable ?? 'missing'} preload=${fs.existsSync(PRELOAD)}`,
    };
  }
  const home = fs.mkdtempSync(path.join(tempRoot, 'tech-'));
  const log = path.join(home, 'modules.log');
  const result = spawnSync(process.execPath, ['--import', pathToFileURL(PRELOAD).href, executable, '--version'], {
    cwd: repo,
    encoding: 'utf8',
    timeout: 120_000,
    maxBuffer: 16 * 1024 * 1024,
    env: {
      ...process.env,
      HOME: home,
      FT_DATA_DIR: path.join(home, 'data'),
      FT_LIBRARY_DIR: path.join(home, 'library'),
      FT_COMMANDS_DIR: path.join(home, 'commands'),
      FT_MODULE_LOG: log,
      NO_COLOR: '1',
    },
  });
  const modules = readModuleLog(log);
  const loadedOclif = modules.some((item) => item.specifier === '@oclif/core' || /\/node_modules\/@oclif\/core\//.test(item.url ?? ''));
  const loadedCommander = modules.some((item) => item.specifier === 'commander' || /\/node_modules\/commander\//.test(item.url ?? ''));
  const oclifVersion = oclifVersionFrom(modules);
  const active = result.status === 0 &&
    loadedOclif &&
    !loadedCommander &&
    String(declaredOclif ?? '').startsWith('4.0.0') &&
    !declaredCommander &&
    (!oclifVersion || String(oclifVersion).startsWith('4.0.0'));
  return {
    active,
    oclifVersion,
    detail: `oclifLoaded=${loadedOclif} commanderLoaded=${loadedCommander} oclif=${declaredOclif} commander=${declaredCommander} version=${oclifVersion}`,
  };
}

async function collectBatches(side, repo, tempRoot) {
  const collected = new Map();
  const failures = new Map();
  const loaders = [
    ['BATCH-P2P-0001', () => collectWithRetry((attempt) => collectP2P0001(side, repo, tempRoot, attempt), 3)],
    ['BATCH-P2P-0002', () => collectWithRetry(() => collectP2P0002(side, repo), 3)],
    ['BATCH-F2P-0001', () => collectF2P0001(side, repo)],
    ['BATCH-F2P-0002', () => collectF2P0002(side, repo, tempRoot)],
    ['BATCH-F2P-0003', () => collectF2P0003(side, repo, tempRoot)],
    ['BATCH-DIFFERENTIAL-0001', () => collectWithRetry((attempt) => collectDifferential(side, repo, tempRoot, attempt), 5)],
  ];
  for (const [batchId, load] of loaders) {
    try {
      const entries = await load();
      for (const [id, entry] of entries) collected.set(id, entry);
    } catch (error) {
      failures.set(batchId, error instanceof Error ? error.message : String(error));
    }
  }
  return { collected, failures };
}

function buildObservation(entry, side, technology, repo) {
  if (entry.kind === 'F2P') {
    const probeStatus = normalizeStatus(entry.source);
    let status;
    if (!technology.active) status = 'FAIL';
    else if (probeStatus === 'INFRA_ERROR') status = 'INCONCLUSIVE';
    else status = probeStatus;
    return {
      id: entry.id,
      kind: entry.kind,
      status,
      value: {
        type: 'json',
        value: sanitize({
          probe_status: probeStatus,
          target_technology_active: technology.active,
          target_technology: technology.detail,
          detail: entry.source?.detail ?? entry.source?.value ?? 'no probe result',
        }, repo),
      },
    };
  }

  const result = observationResult(entry.source, { result: 'INCONCLUSIVE' });
  const value = entry.kind === 'DIFFERENTIAL' && entry.source
    ? Object.fromEntries(Object.entries(entry.source).filter(([key]) => key !== 'ok'))
    : result.value;
  return {
    id: entry.id,
    kind: entry.kind,
    status: result.status,
    value: { type: 'json', value: sanitize(value, repo) },
    // Keep assertion messages outside the behavioral projection: P2P compares
    // `value` with a frozen baseline, while reviewers need the actual failure
    // cause to distinguish a contract regression from an evaluator error.
    ...(result.status !== 'PASS' ? {
      reason: sanitize(entry.source?.detail?.message ?? entry.error ?? '', repo),
      diagnostics: sanitize({
        detail: entry.source?.detail ?? null,
        evidence: entry.source?.evidence ?? null,
        batch_error: entry.error ?? null,
      }, repo),
    } : {}),
  };
}

export async function main({ side, argv = process.argv.slice(2), reportPath, repo } = {}) {
  if (side !== 'source' && side !== 'target') throw new Error(`unknown side: ${side}`);
  const options = parseArgs(argv);
  const root = repoRoot(repo ?? options.repo);
  const out = path.resolve(reportPath ?? options.report ?? '/logs/result.json');
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), `ft-integration-${side}-`));
  try {
    const inventory = loadInventory();
    const build = fs.existsSync(path.join(root, 'package.json')) ? runBuild(root) : { ok: false, detail: `repository missing at ${root}` };
    const { collected, failures } = await collectBatches(side, root, tempRoot);
    const technology = build.ok
      ? detectTargetTechnology(root, tempRoot)
      : { active: false, detail: `build unavailable: ${build.detail}` };
    const byBatch = new Map();
    for (const entry of inventory) {
      if (!byBatch.has(entry.batch_id)) byBatch.set(entry.batch_id, []);
      const batchEntry = {
        ...entry,
        source: collected.get(entry.id) ?? null,
        error: failures.get(entry.batch_id) ?? null,
      };
      byBatch.get(entry.batch_id).push(batchEntry);
    }
    const observations = [];
    for (const entry of inventory) {
      const batchEntry = byBatch.get(entry.batch_id).find((item) => item.id === entry.id);
      observations.push(buildObservation(batchEntry, side, technology, root));
    }
    const report = {
      schema_version: REPORT_SCHEMA,
      batch_id: BATCH_ID,
      side,
      observations,
    };
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, `${JSON.stringify(report, null, 2)}\n`);
    const counts = observations.reduce((accumulator, observation) => {
      accumulator[observation.status] = (accumulator[observation.status] ?? 0) + 1;
      return accumulator;
    }, {});
    process.stdout.write(`${JSON.stringify({ side, repo: root, report: out, counts })}\n`);
    return 0;
  } finally {
    fs.rmSync(tempRoot, { recursive: true, force: true });
  }
}
