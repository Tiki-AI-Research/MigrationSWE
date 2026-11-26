// P2P executable state-preservation probe for the Fastify -> Express migration.
//
// This probe is side-local: it boots the server built from the repository it
// lives in and records an implementation-neutral projection for every P2P
// observation. Fastify and Express agree on the public behavior required here
// but differ in volatile details (exact body bytes, `application/javascript`
// vs `text/javascript`, charset parameters, and the body of framework-generated
// redirects). The recorded projection therefore keeps only fields that a
// behavior-preserving migration must keep equal: HTTP status, a normalized
// content-type family, contract response headers, redirect targets, and an
// empty-body marker. The integration runner compares the source and target
// projections with `equal_after_normalization`.
//
// The probe never hardcodes PASS/FAIL per side. A side PASSes an observation
// only when that observation's probe actually executes and yields a well-formed
// projection; otherwise the observation is reported as FAIL.
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';
import net from 'node:net';

const SIDE = 'target';
const HERE = dirname(fileURLToPath(import.meta.url));
// target/batches/<batch>/probe.mjs -> repository root
const REPO = resolve(HERE, '..', '..', '..');
const FIXTURE = JSON.parse(
  readFileSync(
    join(REPO, 'fixtures/batches/BATCH-P2P-0001/http-probes.json'),
    'utf8'
  )
);

const PORT = 8080;
const PROBE_HOST = '127.0.0.1';
const sha256 = (value) =>
  'sha256:' + createHash('sha256').update(value).digest('hex');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const waitForPort = async (port, timeoutMs = 20000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const open = await new Promise((ok) => {
      const socket = net.connect(port, PROBE_HOST);
      socket.once('connect', () => (socket.destroy(), ok(true)));
      socket.once('error', () => (socket.destroy(), ok(false)));
    });
    if (open) return true;
    await sleep(250);
  }
  return false;
};

const startServer = async () => {
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: REPO,
    env: { ...process.env, NODE_ENV: 'development' },
    stdio: 'ignore',
    detached: true,
  });
  const up = await waitForPort(PORT);
  return { child, up };
};

const stopServer = async (child) => {
  if (!child || child.killed) return;
  try {
    process.kill(-child.pid, 'SIGKILL');
  } catch {
    try { child.kill('SIGKILL'); } catch {}
  }
  await Promise.race([once(child, 'exit'), sleep(5000)]);
};

const request = async (path, { headers = {}, method = 'GET' } = {}) =>
  fetch(`http://${PROBE_HOST}:${PORT}${path}`, {
    method,
    headers,
    redirect: 'manual',
    signal: AbortSignal.timeout(8000),
  });

const headerMap = (res) => {
  const out = {};
  res.headers.forEach((v, k) => (out[k.toLowerCase()] = v));
  return out;
};

// Collapse framework-specific content-type spellings (and charset parameters)
// to a family that must be shared by a behavior-preserving migration.
const normalizeType = (value) => {
  if (!value) return null;
  const base = String(value).split(';')[0].trim().toLowerCase();
  if (base === 'application/javascript' || base === 'text/javascript' ||
      base === 'application/x-javascript')
    return 'javascript';
  if (base === 'image/vnd.microsoft.icon' || base === 'image/x-icon')
    return 'icon';
  if (base === 'text/html') return 'html';
  if (base === 'text/plain') return 'text';
  if (base === 'application/xml' || base === 'text/xml') return 'xml';
  return base;
};

const isRedirect = (status) => status >= 300 && status < 400;

const runHttpProbe = async (probe) => {
  const paths = probe.request.paths || [probe.request.path];
  const records = [];
  for (const p of paths) {
    const res = await request(p, {
      headers: probe.request.headers || {},
      method: 'GET',
    });
    const body = Buffer.from(await res.arrayBuffer());
    const h = headerMap(res);
    records.push({
      path: p,
      status: res.status,
      type: isRedirect(res.status) ? null : normalizeType(h['content-type']),
      location: h['location'] || null,
      'service-worker-allowed': h['service-worker-allowed'] || null,
      'access-control-allow-origin': h['access-control-allow-origin'] || null,
      'x-content-type-options': h['x-content-type-options'] || null,
      'content-security-policy': h['content-security-policy'] || null,
      'x-powered-by': h['x-powered-by'] || null,
      'body-empty': isRedirect(res.status) ? null : body.length === 0,
    });
  }
  return { paths: records };
};

const hashTree = (abs) => {
  if (!existsSync(abs)) return null;
  const stats = statSync(abs);
  if (stats.isFile()) return sha256(readFileSync(abs));
  const entries = [];
  const walk = (dir, prefix) => {
    for (const name of readdirSync(dir).sort()) {
      const child = join(dir, name);
      const childStats = statSync(child);
      if (childStats.isDirectory()) walk(child, prefix + name + '/');
      else entries.push([prefix + name, sha256(readFileSync(child))]);
    }
  };
  walk(abs, '');
  return sha256(JSON.stringify(entries));
};

const runHashProbe = (probe) => {
  const out = {};
  for (const p of probe.request.paths) out[p] = hashTree(join(REPO, p));
  return out;
};

const runNativeProbe = (probe) => {
  if (probe.id === 'P2P-006') {
    // Integrity: the public validation commands must still be present.
    const pkg = JSON.parse(readFileSync(join(REPO, 'package.json'), 'utf8'));
    return {
      test: pkg.scripts.test,
      'proxy-validator': pkg.scripts['proxy-validator'],
      'workflow-test': pkg.scripts['workflow-test'],
      build: pkg.scripts.build,
    };
  }
  if (probe.id === 'P2P-009') {
    // Repository change scope: the out-of-scope files must be untouched. Their
    // content hashes are identical for a behavior-preserving migration, so the
    // projection stays side-neutral while still detecting edits.
    const stable = [
      'src/routes.mjs',
      'src/templates.mjs',
      'src/source-rewrites.mjs',
      'run-command.mjs',
      'config.json',
      'ecosystem.config.js',
    ];
    const mutable = ['src/server.mjs', 'package.json', 'pnpm-lock.yaml'];
    return {
      'stable-hashes': Object.fromEntries(
        stable.map((p) => [p, hashTree(join(REPO, p))])
      ),
      'mutable-present': Object.fromEntries(
        mutable.map((p) => [p, existsSync(join(REPO, p))])
      ),
    };
  }
  return { 'exit-code': 0 };
};

const importNamesFrom = (source, moduleName) => {
  // A named import block cannot contain a closing brace, so anchoring on
  // `[^}]*` keeps the match inside a single statement instead of spanning
  // several imports on the way to the requested module.
  const re = new RegExp(
    "import\\s*\\{([^}]*)\\}\\s*from\\s*['\"][^'\"]*" +
      moduleName +
      "['\"]"
  );
  const match = source.match(re);
  if (!match) return [];
  return match[1]
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .sort();
};

const runModuleProbe = () => {
  const src = readFileSync(join(REPO, 'src/server.mjs'), 'utf8');
  return {
    'routes-imports': importNamesFrom(src, 'routes.mjs'),
    'templates-imports': importNamesFrom(src, 'templates.mjs'),
  };
};

const upgradeStatus = (path, timeoutMs = 3000) =>
  new Promise((resolveStatus) => {
    let settled = false;
    const finish = (status) => {
      if (settled) return;
      settled = true;
      try { socket.destroy(); } catch {}
      resolveStatus(status);
    };
    const socket = net.connect(PORT, PROBE_HOST, () => {
      socket.write(
        `GET ${path} HTTP/1.1\r\n` +
          `Host: ${PROBE_HOST}:${PORT}\r\n` +
          'Connection: Upgrade\r\n' +
          'Upgrade: websocket\r\n' +
          'Sec-WebSocket-Version: 13\r\n' +
          'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n'
      );
    });
    let buffer = '';
    socket.setTimeout(timeoutMs);
    socket.on('data', (chunk) => {
      buffer += chunk.toString('latin1');
      const lineEnd = buffer.indexOf('\r\n');
      if (lineEnd !== -1) {
        if (buffer.includes('\r\n\r\n')) finish(buffer.slice(0, lineEnd));
      }
    });
    socket.on('timeout', () => finish('TIMEOUT'));
    socket.on('error', () => finish('ERROR'));
  });

// Resolve the WebSocket upgrade prefix produced by routes.mjs for any config.
// With usingSEO disabled the prefix is the alt path ('/cron/'), otherwise it is
// the literal prefix ('/wisp/'); both are attempted and the accepted one is
// recorded, so the projection never depends on the config layout.
const wispPrefixes = () => {
  const prefixes = [];
  try {
    const config = JSON.parse(readFileSync(join(REPO, 'config.json'), 'utf8'));
    if (config.usingSEO === true) prefixes.push('/wisp/');
  } catch {}
  prefixes.push('/cron/', '/wisp/');
  return [...new Set(prefixes)];
};

const firstAcceptedUpgrade = async () => {
  let last = 'NONE';
  for (const prefix of wispPrefixes()) {
    const status = await upgradeStatus(prefix);
    last = status;
    if (status.startsWith('HTTP/1.1 101')) return status;
  }
  return last;
};

const runUpgradeProbe = async () => ({
  upgrade: await firstAcceptedUpgrade(),
});

const runMixedProbe = async () => {
  const res = await request('/');
  return {
    'http-status': res.status,
    upgrade: await firstAcceptedUpgrade(),
  };
};

const runShutdownProbe = async () => {
  const shutdown = join(REPO, 'src/.shutdown');
  try { rmSync(shutdown, { force: true }); } catch {}
  writeFileSync(shutdown, '');
  const { child, up } = await startServer();
  try {
    if (!up) throw new Error('shutdown server did not start');
    await request('/test-shutdown').catch(() => {});
    const deadline = Date.now() + 8000;
    while (existsSync(shutdown) && Date.now() < deadline) await sleep(200);
    const removed = !existsSync(shutdown);
    return {
      side: SIDE,
      result: removed ? 'PASS' : 'FAIL',
      projection: { 'shutdown-file-removed': removed },
    };
  } finally {
    try { rmSync(shutdown, { force: true }); } catch {}
    await stopServer(child);
    await sleep(300);
  }
};

const PROJECT = {
  'P2P-003': 'hash',
  'P2P-006': 'native',
  'P2P-009': 'native',
  'P2P-014': 'hash',
  'P2P-018': 'module',
  'P2P-021': 'upgrade',
  'P2P-024': 'mixed',
};

const main = async () => {
  const results = {};
  let started = false;
  const { child, up } = await startServer();
  try {
    started = up;
    if (!up) throw new Error('server did not start');
    for (const probe of FIXTURE.probes) {
      if (probe.id === 'P2P-004') continue;
      try {
        const kind = PROJECT[probe.id] || 'http';
        let projection;
        if (kind === 'hash') projection = runHashProbe(probe);
        else if (kind === 'native') projection = runNativeProbe(probe);
        else if (kind === 'module') projection = runModuleProbe();
        else if (kind === 'upgrade') projection = await runUpgradeProbe();
        else if (kind === 'mixed') projection = await runMixedProbe();
        else projection = await runHttpProbe(probe);
        results[probe.id] = { side: SIDE, result: 'PASS', projection };
      } catch (err) {
        results[probe.id] = {
          side: SIDE,
          result: 'FAIL',
          projection: { error: String((err && err.message) || err) },
        };
      }
    }
  } catch (err) {
    for (const probe of FIXTURE.probes)
      if (!(probe.id in results))
        results[probe.id] = {
          side: SIDE,
          result: 'FAIL',
          projection: { error: String((err && err.message) || err) },
        };
  } finally {
    await stopServer(child);
    await sleep(400);
  }

  if (started && !('P2P-004' in results)) {
    try {
      results['P2P-004'] = await runShutdownProbe();
    } catch (err) {
      results['P2P-004'] = {
        side: SIDE,
        result: 'FAIL',
        projection: { error: String((err && err.message) || err) },
      };
    }
  }

  const ordered = {};
  for (const probe of FIXTURE.probes) ordered[probe.id] = results[probe.id];
  const report = {
    side: SIDE,
    batch_id: 'BATCH-P2P-0001',
    observations: ordered,
  };
  process.stdout.write(JSON.stringify(report), () => process.exit(0));
};

main().catch((err) => {
  process.stderr.write(String((err && err.stack) || err) + '\n');
  process.exit(1);
});
