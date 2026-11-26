// Shared integration harness for the Fastify 5.12.3 -> Express 5.1.0 migration.
//
// This module is consumed by both `source/runner` and `target/runner.mjs`. It
// merges the executable projections that were authored by the dependency
// batches (P2P, F2P and DIFFERENTIAL) into a single self-contained run:
//
//   * P2P observations record a stable behavioral projection of the running
//     repository. The integration grader compares the source projection with
//     the target projection using `equal_after_normalization`.
//   * DIFFERENTIAL observations record the same kind of normalized projection
//     for the shared case corpus.
//   * F2P observations are target-only postconditions. Each predicate both
//     executes the bound entry point and proves that the declared target
//     technology (Express 5.1.0) owns the path for that entry. Under the source
//     technology (Fastify) the predicate is false, so the source side observes
//     FAIL and the target side observes PASS.
//
// No status is hard coded from the declared expectation: every observation is
// produced by actually running the repository under test. When a probe cannot
// be executed the raw observation is INCONCLUSIVE.
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
  mkdirSync,
  writeFileSync,
} from 'node:fs';
import { join, resolve, dirname, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { once } from 'node:events';
import net from 'node:net';
import http from 'node:http';

const HERE = dirname(fileURLToPath(import.meta.url));
const ASSETS = resolve(HERE, '..', '..');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sha256 = (buf) => 'sha256:' + createHash('sha256').update(buf).digest('hex');
const has = (src, marker) => src.includes(marker);
// "normalized content type": collapse framework-specific spellings and charset
// parameters to a media-type family that a behavior-preserving migration must
// keep equal (Fastify replies `application/javascript`, Express serves
// `text/javascript`, both are the same contract).
const normType = (value) => {
  if (!value) return null;
  const base = String(value).split(';')[0].trim().toLowerCase();
  if (
    base === 'application/javascript' ||
    base === 'text/javascript' ||
    base === 'application/x-javascript'
  )
    return 'javascript';
  if (base === 'image/vnd.microsoft.icon' || base === 'image/x-icon') return 'icon';
  if (base === 'text/html') return 'html';
  if (base === 'text/plain') return 'text';
  if (base === 'application/xml' || base === 'text/xml') return 'xml';
  return base;
};

// Blank JavaScript comments while preserving string/regular-expression content.
// Source-technology judgments must not treat prose in a comment as real usage
// (a comment that merely mentions `onSend` or `fastify` does not prove the
// Fastify runtime is still in effect), yet genuine dynamic imports such as
// `import('fastify')` live in string literals and must still be detected.
// Comments are replaced with spaces (newlines kept) so line structure is
// stable; strings and template literals are left untouched.
export const stripComments = (code) => {
  const characters = code.split('');
  const length = characters.length;
  let index = 0;
  let state = 'code';
  let quote = '';
  const blank = (position) => {
    if (position >= 0 && position < length && characters[position] !== '\n')
      characters[position] = ' ';
  };
  while (index < length) {
    const character = code[index];
    if (state === 'code') {
      if (character === '/' && code[index + 1] === '/') {
        while (index < length && code[index] !== '\n') {
          blank(index);
          index += 1;
        }
        continue;
      }
      if (character === '/' && code[index + 1] === '*') {
        blank(index);
        blank(index + 1);
        index += 2;
        while (
          index < length &&
          !(code[index] === '*' && code[index + 1] === '/')
        ) {
          blank(index);
          index += 1;
        }
        if (index < length) {
          blank(index);
          blank(index + 1);
          index += 2;
        }
        continue;
      }
      if (character === "'" || character === '"' || character === '`') {
        state = 'string';
        quote = character;
      }
      index += 1;
      continue;
    }
    if (character === '\\') {
      index += 2;
      continue;
    }
    if (character === quote) {
      state = 'code';
      quote = '';
    }
    index += 1;
  }
  return characters.join('');
};

const OUT_OF_SCOPE = [
  'views/sw.js',
  'views/sw-blacklist.js',
  'views/uv',
  'views/scram',
  'src/templates.mjs',
  'src/source-rewrites.mjs',
  'run-command.mjs',
  'config.json',
  'ecosystem.config.js',
];

const CLIENT_ASSETS = [
  'views/sw.js',
  'views/sw-blacklist.js',
  'views/uv',
  'views/scram',
  'src/templates.mjs',
  'src/source-rewrites.mjs',
  'config.json',
  'ecosystem.config.js',
];

/* ------------------------------------------------------------------ *
 * Repository / inventory discovery
 * ------------------------------------------------------------------ */

export function resolveRepo() {
  for (const key of ['MIGRATION_REPO_ROOT', 'MIGRATION_REPO', 'REPO_ROOT', 'REPO'])
    if (process.env[key]) return resolve(process.env[key]);
  return '/workspace/repo';
}

export function loadInventory() {
  return JSON.parse(
    readFileSync(join(ASSETS, 'shared', 'integration', 'observations.json'), 'utf8')
  );
}

/* ------------------------------------------------------------------ *
 * Low level probes
 * ------------------------------------------------------------------ */

function request(port, reqPath, { method = 'GET', headers = {}, timeoutMs = 10000 } = {}) {
  return new Promise((res, rej) => {
    const req = http.request(
      { host: '127.0.0.1', port, path: reqPath, method, headers },
      (response) => {
        const chunks = [];
        response.on('data', (c) => chunks.push(c));
        response.on('end', () =>
          res({ status: response.statusCode, headers: response.headers, body: Buffer.concat(chunks) })
        );
      }
    );
    req.setTimeout(timeoutMs, () => req.destroy(new Error('request-timeout')));
    req.on('error', rej);
    req.end();
  });
}

function upgrade(port, reqPath, timeoutMs = 3500) {
  return new Promise((res) => {
    const socket = net.connect(port, '127.0.0.1');
    let data = '';
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      socket.destroy();
      res(data);
    };
    const timer = setTimeout(finish, timeoutMs);
    socket.on('connect', () =>
      socket.write(
        [
          `GET ${reqPath} HTTP/1.1`,
          `Host: 127.0.0.1:${port}`,
          'Upgrade: websocket',
          'Connection: Upgrade',
          'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
          'Sec-WebSocket-Version: 13',
          'Sec-WebSocket-Protocol: wisp',
          '',
          '',
        ].join('\r\n')
      )
    );
    socket.on('data', (chunk) => {
      data += chunk.toString('latin1');
      if (/HTTP\/1\.1 101/.test(data)) finish();
    });
    socket.on('error', finish);
    socket.on('close', finish);
  });
}

async function portOpen(port) {
  return new Promise((res) => {
    const socket = net.connect(port, '127.0.0.1');
    socket.once('connect', () => (socket.destroy(), res(true)));
    socket.once('error', () => (socket.destroy(), res(false)));
  });
}

export async function bootServer(repo, { port = 8080, timeoutMs = 45000 } = {}) {
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: repo,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env },
  });
  let log = '';
  child.stdout.on('data', (d) => (log += d));
  child.stderr.on('data', (d) => (log += d));
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null)
      return { ok: false, child, port, log, reason: 'server-exited:' + log.slice(-400) };
    if (await portOpen(port)) {
      await sleep(300);
      return { ok: true, child, port, log };
    }
    await sleep(250);
  }
  return { ok: false, child, port, log, reason: 'boot-timeout' };
}

export function stopServer(handle) {
  try {
    if (handle && handle.child) handle.child.kill('SIGKILL');
  } catch {
    /* ignore */
  }
}

/* ------------------------------------------------------------------ *
 * Stable behavioral projections (P2P / DIFFERENTIAL)
 * ------------------------------------------------------------------ */

async function httpProj(ctx, paths, opts = {}) {
  const out = [];
  for (const p of paths) {
    try {
      const r = await request(ctx.port, p, opts);
      // Only implementation-neutral fields are recorded. Exact body bytes are
      // intentionally excluded: the built `views/dist` artifacts are randomized
      // and gzip-compressed per build, so body digests are not a stable
      // behavioral projection. Status, content-type family, contract headers
      // and an empty-body marker are what a behavior-preserving migration must
      // keep equal.
      const redirect = r.status >= 300 && r.status < 400;
      out.push({
        path: p,
        status: r.status,
        type: redirect ? null : normType(r.headers['content-type']),
        location: r.headers['location'] || null,
        'service-worker-allowed': r.headers['service-worker-allowed'] || null,
        'access-control-allow-origin': r.headers['access-control-allow-origin'] || null,
        'x-content-type-options': r.headers['x-content-type-options'] || null,
        'content-security-policy': r.headers['content-security-policy'] || null,
        'x-powered-by': r.headers['x-powered-by'] || null,
        'body-empty': redirect ? null : r.body.length === 0,
      });
    } catch (err) {
      out.push({ path: p, error: String((err && err.message) || err) });
    }
  }
  return { paths: out };
}

function walkFiles(dir, acc) {
  for (const entry of readdirSync(dir)) {
    const abs = join(dir, entry);
    const st = statSync(abs);
    if (st.isDirectory()) walkFiles(abs, acc);
    else acc.push(abs);
  }
  return acc;
}

function hashPath(repo, rel) {
  const abs = join(repo, rel);
  if (!existsSync(abs)) return null;
  const st = statSync(abs);
  if (st.isDirectory()) {
    const files = walkFiles(abs, []).sort();
    const h = createHash('sha256');
    for (const f of files) {
      h.update(relative(abs, f));
      h.update(readFileSync(f));
    }
    return 'sha256:' + h.digest('hex');
  }
  return sha256(readFileSync(abs));
}

function hashProj(ctx, paths) {
  const out = {};
  for (const p of paths) out[p] = hashPath(ctx.repo, p);
  return out;
}

function moduleProj(ctx) {
  const fromRoutes = ['config', 'serverUrl', 'pages', 'externalPages', 'flatAltPaths', 'getAltPrefix'];
  const fromTemplates = ['tryReadFile', 'preloaded404'];
  return {
    'routes-imports': fromRoutes.filter((n) => has(ctx.src, n)),
    'templates-imports': fromTemplates.filter((n) => has(ctx.src, n)),
  };
}

function nativeIntegrity(ctx) {
  try {
    const pkg = JSON.parse(readFileSync(join(ctx.repo, 'package.json'), 'utf8'));
    return {
      test: pkg.scripts && pkg.scripts.test,
      'proxy-validator': pkg.scripts && pkg.scripts['proxy-validator'],
      'workflow-test': pkg.scripts && pkg.scripts['workflow-test'],
      build: pkg.scripts && pkg.scripts.build,
    };
  } catch (err) {
    return { error: String((err && err.message) || err) };
  }
}

function changeScope(ctx) {
  const mutable = ['src/server.mjs', 'package.json', 'pnpm-lock.yaml'];
  const stable = [
    'src/routes.mjs',
    'src/templates.mjs',
    'src/source-rewrites.mjs',
    'run-command.mjs',
    'config.json',
    'ecosystem.config.js',
  ];
  return {
    'mutable-set': mutable,
    'stable-set': stable,
    'mutable-present': mutable.map((p) => existsSync(join(ctx.repo, p))),
    'stable-present': stable.map((p) => existsSync(join(ctx.repo, p))),
  };
}

function shutdownInfo(ctx) {
  return {
    'shutdown-route': has(ctx.src, 'test-shutdown'),
    'shutdown-file': has(ctx.src, '.shutdown'),
    'stop-call': has(ctx.src, 'wisp.stop()'),
    'delete-call': has(ctx.src, 'unlinkSync(shutdown)'),
  };
}

function integrityProj(ctx) {
  let scripts = {};
  try {
    const pkg = JSON.parse(readFileSync(join(ctx.repo, 'package.json'), 'utf8'));
    scripts = Object.keys(pkg.scripts || {}).sort();
  } catch {
    scripts = [];
  }
  return {
    'scripts-present': scripts,
    'validator-present': existsSync(join(ctx.repo, 'proxyServiceValidator.js')),
    'assertions-removed': false,
  };
}

function listenInfo(ctx) {
  return {
    host: ctx.meta.host,
    port: ctx.meta.port,
    pathname: ctx.meta.pathname,
  };
}

async function upgradeProj(ctx) {
  const candidates = [ctx.meta.wisp, '/wisp/', '/cron/'].filter(Boolean);
  let raw = '';
  let used = null;
  for (const candidate of candidates) {
    raw = await upgrade(ctx.port, candidate);
    if (/HTTP\/1\.1 101/.test(raw)) {
      used = candidate;
      break;
    }
  }
  return { 'switching-protocols': /HTTP\/1\.1 101/.test(raw), path: used };
}

async function mixedProj(ctx) {
  const r = await request(ctx.port, '/');
  const up = await upgradeProj(ctx);
  return { 'http-status': r.status, 'server-count': 1, 'upgrade-routed': up['switching-protocols'] };
}

/* ------------------------------------------------------------------ *
 * P2P projections
 * ------------------------------------------------------------------ */

async function p2pProjection(id, ctx) {
  switch (id) {
    case 'P2P-001':
      return httpProj(ctx, ['/proxy/']);
    case 'P2P-002':
      return httpProj(ctx, ['/assets/js/card.js', '/uv/uv.sw.js', '/scram/', '/epoxy/', '/libcurl/', '/baremux/']);
    case 'P2P-003':
      return hashProj(ctx, OUT_OF_SCOPE);
    case 'P2P-004':
      return httpProj(ctx, ['/test-shutdown']);
    case 'P2P-005':
      return httpProj(ctx, ['/documentation', '/robots.txt', '/manifest.json']);
    case 'P2P-006':
      return nativeIntegrity(ctx);
    case 'P2P-007':
      return httpProj(ctx, ['/documentation.ico']);
    case 'P2P-008':
      return httpProj(ctx, [ctx.meta.service, ctx.meta.serviceBlacklist]);
    case 'P2P-009':
      return changeScope(ctx);
    case 'P2P-010':
      return httpProj(ctx, ['/documentation', '/', '/not-a-real-page']);
    case 'P2P-011':
      return httpProj(ctx, ['/robots.txt', '/assets/js/card.js', '/login', '/github']);
    case 'P2P-012':
      return httpProj(ctx, ['/github.ico']);
    case 'P2P-013':
      return faviconProjection(ctx);
    case 'P2P-014':
      return hashProj(ctx, CLIENT_ASSETS);
    case 'P2P-015':
      return { listen: listenInfo(ctx), request: await httpProj(ctx, ['/']) };
    case 'P2P-016':
      return httpProj(ctx, ['/documentation'], {
        headers: { 'sec-fetch-dest': 'document', cookie: 'HistoryHide=true' },
      });
    case 'P2P-017':
      return httpProj(ctx, ['/not-a-real-page']);
    case 'P2P-018':
      return moduleProj(ctx);
    case 'P2P-019':
      return httpProj(ctx, ['/github/wisp', '/github/does-not-exist']);
    case 'P2P-020':
      return httpProj(ctx, ['/']);
    case 'P2P-021':
      return upgradeProj(ctx);
    case 'P2P-022':
      return httpProj(ctx, ['//documentation//', '/documentation/', '/documentation']);
    case 'P2P-023':
      return httpProj(ctx, ['/', '/documentation', '/browsing', '/robots.txt', ctx.meta.service, '/assets/js/card.js']);
    case 'P2P-024':
      return mixedProj(ctx);
    case 'P2P-025':
      return httpProj(ctx, ['/documentation']);
    default:
      return undefined;
  }
}

/* ------------------------------------------------------------------ *
 * DIFFERENTIAL projections
 * ------------------------------------------------------------------ */

async function diffProjection(id, ctx) {
  switch (id) {
    case 'DIFF-001':
      return { http: await httpProj(ctx, ['/']), upgrade: await upgradeProj(ctx), 'distinct_listen_ports': 1 };
    case 'DIFF-002':
      return hashProj(ctx, OUT_OF_SCOPE);
    case 'DIFF-003':
      return { listen: listenInfo(ctx), request: await httpProj(ctx, ['/']) };
    case 'DIFF-004':
      return httpProj(ctx, ['/documentation', '/robots.txt']);
    case 'DIFF-005':
      return httpProj(ctx, ['/documentation', '/', '/not-a-real-page']);
    case 'DIFF-006':
      return httpProj(ctx, ['/not-a-real-page']);
    case 'DIFF-007':
      return httpProj(ctx, ['/github/' + ctx.meta.githubWisp, '/github/does-not-exist']);
    case 'DIFF-008':
      return httpProj(
        ctx,
        ctx.meta.staticPrefixes.length
          ? ctx.meta.staticPrefixes
          : ['/assets/', '/uv/', '/scram/', '/epoxy/', '/libcurl/', '/baremux/']
      );
    case 'DIFF-009':
      return httpProj(ctx, [ctx.meta.service, ctx.meta.serviceBlacklist]);
    case 'DIFF-010':
      return upgradeProj(ctx);
    case 'DIFF-011':
      return shutdownInfo(ctx);
    case 'DIFF-012':
      return httpProj(ctx, ['/']);
    case 'DIFF-013':
      return httpProj(ctx, ['//documentation//', '/documentation/', '/documentation']);
    case 'DIFF-014':
      return faviconProjection(ctx);
    case 'DIFF-015':
      return httpProj(ctx, ['/documentation']);
    case 'DIFF-016':
      return httpProj(ctx, ['/robots.txt', '/assets/js/card.js', '/login', '/github']);
    case 'DIFF-017':
      return httpProj(ctx, ['/documentation.ico']);
    case 'DIFF-018':
      return httpProj(ctx, ['/documentation'], {
        headers: { 'sec-fetch-dest': 'document', cookie: 'HistoryHide=true' },
      });
    case 'DIFF-019':
      return httpProj(ctx, ['/github.ico']);
    case 'DIFF-020':
      return {
        pathname: ctx.meta.pathname,
        request: await httpProj(ctx, [ctx.meta.pathname === '/' ? '/' : ctx.meta.pathname + '/']),
      };
    case 'DIFF-021':
      return httpProj(ctx, ['/', '/documentation', '/browsing', '/robots.txt', ctx.meta.service, '/assets/js/card.js']);
    case 'DIFF-022':
      return changeScope(ctx);
    case 'DIFF-023':
      return moduleProj(ctx);
    case 'DIFF-024':
      return hashProj(ctx, CLIENT_ASSETS);
    case 'DIFF-025':
      return integrityProj(ctx);
    default:
      return undefined;
  }
}

/* ------------------------------------------------------------------ *
 * F2P target-technology predicates
 * ------------------------------------------------------------------ */

// The contract is that the Express application is in effect, not how the local
// binding happens to be spelled: `import express from 'express'` and the
// equivalent `import expressFramework from 'express'` are the same migration.
// Match the module specifier only, never a particular local variable name.
const usesExpress = (s) =>
  /(?:from\s*|require\s*\(\s*|import\s*\(\s*|import\s+)['"]express(?:\/[^'"]*)?['"]/.test(s);
const importsFastify = (s) =>
  /from\s*['"]fastify['"]/.test(s) ||
  /require\(\s*['"]fastify['"]\s*\)/.test(s) ||
  /import\(\s*['"]fastify['"]\s*\)/.test(s) ||
  /['"]@fastify\//.test(s);
const expressInEffect = (s) => usesExpress(s) && !importsFastify(s);

// A Wisp upgrade listener may be attached in any equivalent spelling: on its
// own statement (`server.on('upgrade', ...)`, `server.addListener('upgrade',
// ...)`) or chained onto the HTTP server factory
// (`createServer(app).on('upgrade', ...)`).  The spelling is not part of the
// contract, only that some upgrade listener exists.
const upgradeListenerWired = (s) => /(?:\.on|\.addListener)\(\s*['"]upgrade['"]/.test(s);

// Pure postcondition for the "Wisp upgrade stays on the same HTTP server as the
// Express application" requirement.  Exported so it can be regression-tested
// without booting a server.  `src` is expected to be comment-stripped.
export const upgradePostcondition = (src, { status, upgraded }) =>
  status === 200 &&
  upgraded &&
  expressInEffect(src) &&
  upgradeListenerWired(src);

async function pRouterPage(ctx) {
  const r = await request(ctx.port, '/documentation');
  const target = expressInEffect(ctx.src);
  return {
    pass: r.status === 200 && /text\/html/i.test(r.headers['content-type'] || '') && target,
    value: { status: r.status, 'content-type': r.headers['content-type'] || null, target },
  };
}

async function pNoXPoweredBy(ctx) {
  const r = await request(ctx.port, '/');
  return {
    // Emitting no X-Powered-By header is the contract; how the server achieves
    // it (Express's own switch, a hardening middleware, or removing the header)
    // is not part of the observable behavior.
    pass: !r.headers['x-powered-by'] && expressInEffect(ctx.src),
    value: { status: r.status, 'x-powered-by': r.headers['x-powered-by'] || null, target: expressInEffect(ctx.src) },
  };
}

async function pRootRoute(ctx) {
  const r = await request(ctx.port, '/');
  const type = normType(r.headers['content-type']);
  return {
    // The contract is that the Express-owned root route serves the default page
    // with a real HTML body.  How the handler is registered (an inline arrow, a
    // named handler, `app.get(serverUrl.pathname, ...)`, or an equivalent) is an
    // implementation choice, so only the observable response is asserted.
    pass:
      r.status === 200 && type === 'html' && r.body.length > 0 && expressInEffect(ctx.src),
    value: { status: r.status, type, bytes: r.body.length },
  };
}

async function pStaticPrecedence(ctx) {
  const r = await request(ctx.port, '/assets/js/card.js');
  const contentType = String(r.headers['content-type'] || '');
  return {
    // Behavior, not text layout: the prefixed static asset must be served by
    // the Express-owned server.  How the static handler is mounted (before or
    // after the parametric `:path` route, `express.static` or an equivalent
    // handler) is an implementation choice, so this predicate asserts the
    // observable response rather than the textual index of `express.static`.
    pass:
      r.status === 200 &&
      r.body.length > 0 &&
      normType(contentType) === 'javascript' &&
      expressInEffect(ctx.src),
    value: { status: r.status, bytes: r.body.length, 'content-type': contentType || null },
  };
}

async function pHelmet(ctx) {
  const r = await request(ctx.port, '/');
  return {
    // The contract is the observable header set, not the library that emits
    // it: any Express-owned implementation that preserves nosniff, disables
    // content security policy and omits X-Powered-By satisfies the security
    // postcondition.
    pass:
      r.headers['x-content-type-options'] === 'nosniff' &&
      !r.headers['content-security-policy'] &&
      !r.headers['x-powered-by'] &&
      expressInEffect(ctx.src),
    value: {
      nosniff: r.headers['x-content-type-options'] || null,
      csp: r.headers['content-security-policy'] || null,
      'x-powered-by': r.headers['x-powered-by'] || null,
    },
  };
}

async function pUpgrade(ctx) {
  const r = await request(ctx.port, '/');
  const candidates = [ctx.meta.wisp, '/wisp/', '/cron/'].filter(Boolean);
  let upgraded = false;
  let used = null;
  for (const c of candidates) {
    const raw = await upgrade(ctx.port, c);
    if (/HTTP\/1\.1 101/.test(raw)) {
      upgraded = true;
      used = c;
      break;
    }
  }
  return {
    // The contract is the shared-server upgrade behavior: HTTP 200 plus a 101
    // upgrade on the same port, under Express ownership. A local HTTP helper
    // may retain its old name. How the upgrade listener is spelled is not part
    // of the contract (see `upgradePostcondition`).
    pass: upgradePostcondition(ctx.src, { status: r.status, upgraded }),
    value: { status: r.status, upgraded, used },
  };
}

async function pGithub(ctx) {
  const keys = [ctx.meta.githubWisp, 'wisp', 'router'].filter(Boolean);
  let hit = null;
  for (const key of keys) {
    const r = await request(ctx.port, '/github/' + key);
    if ([301, 302, 303, 307, 308].includes(r.status)) {
      hit = r;
      break;
    }
  }
  const miss = await request(ctx.port, '/github/does-not-exist');
  return {
    // Behaviour only: a known key redirects, an unknown key is a 404, and the
    // Express application owns the path.  The route declaration spelling (for
    // example the literal `github/:redirect`) is not part of the contract.
    pass:
      !!hit &&
      hit.status >= 300 &&
      hit.status < 400 &&
      miss.status === 404 &&
      expressInEffect(ctx.src),
    value: { hit: hit ? hit.status : null, miss: miss.status },
  };
}

async function pSlash(ctx) {
  const canonical = await request(ctx.port, '/documentation');
  const mutants = ['///documentation//', '/documentation/'];
  const values = [];
  let same = true;
  for (const m of mutants) {
    const r = await request(ctx.port, m);
    const eq = r.status === canonical.status && r.body.equals(canonical.body);
    same = same && eq;
    values.push({ path: m, status: r.status, equal: eq });
  }
  return {
    // Duplicate/trailing slash normalization is a behavioral contract: the
    // mutant paths must resolve to the same page as the canonical path.  The
    // exact regular-expression spelling used to normalize is not part of the
    // contract, so only the observed equivalence (and Express ownership) is
    // asserted here.
    pass: canonical.status === 200 && same && expressInEffect(ctx.src),
    value: { canonical: canonical.status, mutants: values },
  };
}

async function pHistoryHide(ctx) {
  const headers = { 'sec-fetch-dest': 'document', cookie: 'HistoryHide=true' };
  const results = [];
  for (const p of ['/', '/documentation', '/assets/js/card.js'])
    results.push({ path: p, status: (await request(ctx.port, p, { headers })).status });
  return {
    // The observable contract is that a document request carrying the cookie is
    // answered with 404 for dynamic, static and root responses alike.  How the
    // status is forced (a `writeHead` override, `res.statusCode`, or middleware)
    // is an implementation detail.
    pass: results.every((r) => r.status === 404) && expressInEffect(ctx.src),
    value: { results },
  };
}

async function pContentType(ctx) {
  const target = expressInEffect(ctx.src);
  // Content type is resolved from the file extension with the configured
  // default fallback.  With the frozen configuration these are the observable
  // cases: an `.html` page served through the pages map is `text/html`, while
  // an extension absent from the map (`.json`) falls back to the disguised
  // default.  The result must come from the migration's own type resolution,
  // not from Express's automatic extension guessing.
  const login = await request(ctx.port, '/login');
  const manifest = await request(ctx.port, '/manifest.json');
  const loginType = normType(login.headers['content-type']);
  const manifestType = normType(manifest.headers['content-type']);
  return {
    pass:
      login.status === 200 &&
      login.body.length > 0 &&
      loginType === 'html' &&
      manifest.status === 200 &&
      manifestType === 'icon' &&
      target,
    value: {
      login: loginType,
      loginStatus: login.status,
      manifest: manifestType,
      manifestStatus: manifest.status,
      target,
    },
  };
}

async function pEsm(ctx) {
  const r = await request(ctx.port, '/');
  const expressDefault = usesExpress(ctx.src);
  const staticCall = /express\.static\(/.test(ctx.src);
  return {
    // ESM interop contract: the Express application must be default-imported
    // and the server must boot and answer.  Whether static assets are served by
    // `express.static` or an equivalent handler is covered behaviorally by the
    // static-serving predicate, so it is recorded here but not required.
    pass: expressDefault && r.status === 200,
    value: { expressDefault, staticCall, status: r.status },
  };
}

async function pDepPolicy(ctx) {
  let manifest = { express: false, fastify: true };
  try {
    const pkg = JSON.parse(readFileSync(join(ctx.repo, 'package.json'), 'utf8'));
    const deps = { ...(pkg.dependencies || {}), ...(pkg.devDependencies || {}) };
    manifest = {
      express: 'express' in deps,
      fastify: 'fastify' in deps || '@fastify/static' in deps || '@fastify/helmet' in deps,
    };
  } catch {
    /* manifest unreadable */
  }
  let bootStatus = null;
  if (ctx.bootOk) {
    try {
      bootStatus = (await request(ctx.port, '/')).status;
    } catch {
      bootStatus = null;
    }
  }
  const bootOk = ctx.bootOk ? bootStatus === 200 : true;
  return {
    // The target technology must be declared and the source technology removed.
    // The header-hardening helper is an implementation choice and is verified
    // behaviorally, not by its package name.
    pass: manifest.express && !manifest.fastify && bootOk,
    value: { ...manifest, bootStatus },
  };
}

function pExpressInProcess(ctx) {
  const s = ctx.src;
  const importFastify =
    /(?:^|\n)\s*import[^\n]*from\s*['"]fastify['"]/.test(s) ||
    /require\(\s*['"]fastify['"]\s*\)/.test(s) ||
    /import\(\s*['"]fastify['"]\s*\)/.test(s);
  const plugins = /['"]@fastify\/(?:static|helmet)['"]/.test(s);
  const express = usesExpress(s);
  const delegates = /(?:child_process|fork\(|spawn\(|execFile\()/.test(s) && /fastify/i.test(s);
  return {
    pass: express && !importFastify && !plugins && !delegates,
    value: { express, importFastify, plugins, delegates },
  };
}

const faviconResponseValid = (response) =>
  response.status >= 200 && response.status < 300 && response.body.length === 0;

export async function faviconProjection(ctx) {
  const projection = await httpProj(ctx, ['/favicon.ico']);
  for (const row of projection.paths) {
    // The task specifies an empty successful favicon response, not a MIME type
    // or a particular successful status. Preserve errors, body and security
    // fields; normalize only these incidental values on a valid empty reply.
    if (row.status >= 200 && row.status < 300 && row['body-empty'] === true) {
      row.status = 200;
      row.type = null;
    }
  }
  return projection;
}

export async function pFavicon(ctx) {
  const r = await request(ctx.port, '/favicon.ico');
  return {
    pass: faviconResponseValid(r) && expressInEffect(ctx.src),
    value: { status: r.status, bytes: r.body.length },
  };
}

export async function pIcoStrip(ctx) {
  const r = await request(ctx.port, '/documentation.ico');
  // Only the exact favicon.ico page is exempt. An unrelated page whose name
  // ends in favicon.ico must still be displaced and carry the CORS header,
  // including when the underlying page does not exist.
  const suffixPath = '/notfavicon.ico';
  const suffix = await request(ctx.port, suffixPath);
  const suffixPage = {
    path: suffixPath, status: suffix.status,
    type: normType(suffix.headers['content-type']),
    acao: suffix.headers['access-control-allow-origin'] || null,
  };
  // Stripping a suffix must keep page dispatch semantics. A service worker
  // is not a pages-map key; rerouting its displaced path into the worker
  // handler would expose JavaScript with the wrong MIME and cache contract.
  const workerPaths = [ctx.meta.service, ctx.meta.serviceBlacklist].filter(Boolean);
  const workers = [];
  for (const path of workerPaths) {
    const response = await request(ctx.port, path + '.ico');
    workers.push({
      path: path + '.ico', status: response.status,
      type: normType(response.headers['content-type']),
      acao: response.headers['access-control-allow-origin'] || null,
    });
  }
  return {
    pass:
      r.status === 200 &&
      /image\/vnd\.microsoft\.icon/.test(r.headers['content-type'] || '') &&
      r.headers['access-control-allow-origin'] === 'null' &&
      suffixPage.status === 404 && suffixPage.type === 'icon' && suffixPage.acao === 'null' &&
      workers.length === 2 && workers.every((row) =>
        row.status === 404 && row.type === 'icon' && row.acao === 'null') &&
      expressInEffect(ctx.src),
    value: {
      status: r.status,
      'content-type': r.headers['content-type'] || null,
      'access-control-allow-origin': r.headers['access-control-allow-origin'] || null,
      suffixPage,
      workers,
    },
  };
}

async function pGithubIco(ctx) {
  const r = await request(ctx.port, '/github.ico');
  return {
    // A displaced external-page key is an error page, so the explicit task
    // contract requires the disguised type even though Fastify sent HTML.
    pass:
      expressInEffect(ctx.src) && r.status === 404 &&
      normType(r.headers['content-type']) === 'icon' &&
      r.headers['access-control-allow-origin'] === 'null' && r.body.length > 0,
    value: {
      status: r.status, 'content-type': r.headers['content-type'] || null,
      'access-control-allow-origin': r.headers['access-control-allow-origin'] || null,
    },
  };
}

async function pPrefixed(ctx) {
  const pathname = (ctx.config && ctx.config.pathname) || '/';
  const target = pathname === '/' ? '/' : pathname.replace(/\/+$/, '') + '/';
  const r = await request(ctx.port, target);
  return {
    // Serving the prefixed root must be a real Express response, not a textual
    // route spelling.  When the deployment has no prefix this collapses to the
    // same root-page contract as `pRootRoute`.
    pass: r.status === 200 && r.body.length > 0 && expressInEffect(ctx.src),
    value: { pathname, path: target, status: r.status, bytes: r.body.length },
  };
}

/* ------------------------------------------------------------------ *
 * BATCH-F2P-0004: higher-order behaviour predicates
 *
 * These predicates extend the graded set with a continuous difficulty
 * gradient.  Each one asserts a runtime-observable postcondition of the frozen
 * instruction contract and is gated on the Express application owning the entry
 * path; none of them inspect implementation spelling.  `F2P_EXTENSION_TIERS` is
 * the single source of truth for which nodes are graded and at which tier; the
 * derived observation inventory, config lists, source baseline, task counts and
 * public integrity manifest are regenerated by
 * tools/sync_invisiproxy_f2p.py.
 * ------------------------------------------------------------------ */

async function pExtServiceWorker(ctx) {
  const expected = (ctx.meta && ctx.meta.pathname) || '/';
  const paths = [ctx.meta.service, ctx.meta.serviceBlacklist].filter(Boolean);
  const rows = [];
  let ok = expressInEffect(ctx.src);
  for (const p of paths) {
    const r = await request(ctx.port, p);
    const type = normType(r.headers['content-type']);
    const allowed = r.headers['service-worker-allowed'] || null;
    const pass = r.status === 200 && type === 'javascript' && allowed === expected;
    ok = ok && pass;
    rows.push({ path: p, status: r.status, type, 'service-worker-allowed': allowed, pass });
  }
  return { pass: ok, value: { expected, rows } };
}

async function pExtIcoDisplacementPage(ctx) {
  const r = await request(ctx.port, '/documentation.ico');
  const type = normType(r.headers['content-type']);
  const acao = r.headers['access-control-allow-origin'] || null;
  return {
    pass:
      r.status === 200 &&
      type === 'icon' &&
      acao === 'null' &&
      r.body.length > 0 &&
      expressInEffect(ctx.src),
    value: { status: r.status, type, 'access-control-allow-origin': acao, bytes: r.body.length },
  };
}

async function pExtUnknownExtension404(ctx) {
  const r = await request(ctx.port, '/does-not-exist.xyz');
  const type = normType(r.headers['content-type']);
  return {
    pass: r.status === 404 && type === 'icon' && r.body.length > 0 && expressInEffect(ctx.src),
    value: { status: r.status, type, bytes: r.body.length },
  };
}

async function pExtGithubRedirectLocation(ctx) {
  const key = (ctx.meta && ctx.meta.githubWisp) || 'router';
  const table = (ctx.routes && ctx.routes.externalPages && ctx.routes.externalPages.github) || {};
  const expected = table[key] || null;
  const r = await request(ctx.port, '/github/' + key);
  const location = r.headers['location'] || null;
  return {
    pass:
      !!expected &&
      r.status >= 300 &&
      r.status < 400 &&
      location === expected &&
      expressInEffect(ctx.src),
    value: { key, status: r.status, location, expected },
  };
}

async function pExtExternalStringRedirect(ctx) {
  const table = (ctx.routes && ctx.routes.externalPages) || {};
  let key = 'status';
  let expected = table[key];
  if (typeof expected !== 'string') {
    const entry = Object.entries(table).find(([, v]) => typeof v === 'string');
    key = entry ? entry[0] : null;
    expected = entry ? entry[1] : null;
  }
  const r = key ? await request(ctx.port, '/' + key) : { status: 0, headers: {} };
  const location = r.headers['location'] || null;
  return {
    pass:
      typeof expected === 'string' &&
      r.status >= 300 &&
      r.status < 400 &&
      location === expected &&
      expressInEffect(ctx.src),
    value: { key, status: r.status, location, expected },
  };
}

async function pExtHistoryHideBoundary(ctx) {
  const both = { 'sec-fetch-dest': 'document', cookie: 'HistoryHide=true' };
  const cookieOnly = { cookie: 'HistoryHide=true' };
  const documentOnly = { 'sec-fetch-dest': 'document' };
  const a = await request(ctx.port, '/documentation', { headers: both });
  const b = await request(ctx.port, '/documentation', { headers: cookieOnly });
  const c = await request(ctx.port, '/documentation', { headers: documentOnly });
  return {
    // Only a document request *with* the cookie is downgraded; either condition
    // alone must leave the response untouched.
    pass: a.status === 404 && b.status === 200 && c.status === 200 && expressInEffect(ctx.src),
    value: { documentWithCookie: a.status, cookieOnly: b.status, documentOnly: c.status },
  };
}

async function pExtDisguiseClassMatrix(ctx) {
  const disguised = await request(ctx.port, '/documentation');
  const bypass = await request(ctx.port, '/documentation.js');
  const exempt = await request(ctx.port, '/login');
  const favicon = await request(ctx.port, '/favicon.ico');
  const pass =
    disguised.status === 200 &&
    normType(disguised.headers['content-type']) === 'html' &&
    bypass.status === 404 &&
    normType(bypass.headers['content-type']) === 'icon' &&
    exempt.status === 200 &&
    normType(exempt.headers['content-type']) === 'html' &&
    exempt.body.length > 0 &&
    faviconResponseValid(favicon) &&
    expressInEffect(ctx.src);
  return {
    pass,
    value: {
      disguised: { status: disguised.status, type: normType(disguised.headers['content-type']) },
      bypass: { status: bypass.status, type: normType(bypass.headers['content-type']) },
      exempt: { status: exempt.status, type: normType(exempt.headers['content-type']) },
      favicon: { status: favicon.status, bytes: favicon.body.length },
    },
  };
}

async function pExtUnknownIcoDisplaced404(ctx) {
  const r = await request(ctx.port, '/does-not-exist.ico');
  const type = normType(r.headers['content-type']);
  const acao = r.headers['access-control-allow-origin'] || null;
  return {
    pass: r.status === 404 && type === 'icon' && acao === 'null' && expressInEffect(ctx.src),
    value: { status: r.status, type, 'access-control-allow-origin': acao },
  };
}

async function pExtSlashQueryNormalization(ctx) {
  const canonical = await request(ctx.port, '/documentation?x=1');
  const mutants = ['//documentation//?x=1', '/documentation/?x=1'];
  const rows = [];
  let same = canonical.status === 200;
  for (const m of mutants) {
    const r = await request(ctx.port, m);
    const equal = r.status === canonical.status && r.body.equals(canonical.body);
    same = same && equal;
    rows.push({ path: m, status: r.status, equal });
  }
  return {
    pass: same && expressInEffect(ctx.src),
    value: { canonical: canonical.status, rows },
  };
}

async function pExtSecurityHeadersClasses(ctx) {
  const paths = ['/documentation', '/service.js', '/does-not-exist.xyz', '/github/does-not-exist'];
  const rows = [];
  let ok = expressInEffect(ctx.src);
  for (const p of paths) {
    const r = await request(ctx.port, p);
    const nosniff = r.headers['x-content-type-options'] || null;
    const csp = r.headers['content-security-policy'] || null;
    const powered = r.headers['x-powered-by'] || null;
    const pass = nosniff === 'nosniff' && !csp && !powered;
    ok = ok && pass;
    rows.push({ path: p, nosniff, csp, 'x-powered-by': powered, pass });
  }
  return { pass: ok, value: { rows } };
}

async function pExtGithubUnknownDisguised404(ctx) {
  const r = await request(ctx.port, '/github/does-not-exist');
  const type = normType(r.headers['content-type']);
  return {
    pass: r.status === 404 && type === 'icon' && expressInEffect(ctx.src),
    value: { status: r.status, type },
  };
}

async function pExtDefaultEqualsLogin(ctx) {
  const root = await request(ctx.port, '/');
  const login = await request(ctx.port, '/login');
  return {
    // With disguiseFiles enabled the default page is `login`, so the root and
    // the exempt login route must serve the same underlying page.
    pass:
      root.status === 200 &&
      login.status === 200 &&
      root.body.length > 0 &&
      root.body.equals(login.body) &&
      expressInEffect(ctx.src),
    value: {
      root: root.status,
      login: login.status,
      rootBytes: root.body.length,
      loginBytes: login.body.length,
    },
  };
}

async function pExtUpgradeConditional(ctx) {
  const http = await request(ctx.port, '/');
  const wisp = await upgrade(ctx.port, ctx.meta.wisp);
  const other = await upgrade(ctx.port, '/documentation');
  return {
    // The upgrade listener must route only the configured wisp path to the
    // mrrowisp server; an unrelated path must not be upgraded, while plain HTTP
    // keeps working on the same server.
    pass:
      http.status === 200 &&
      /HTTP\/1\.1 101/.test(wisp) &&
      !/HTTP\/1\.1 101/.test(other) &&
      expressInEffect(ctx.src),
    value: {
      http: http.status,
      wisp: /HTTP\/1\.1 101/.test(wisp),
      other: /HTTP\/1\.1 101/.test(other),
    },
  };
}

async function pExtIcoDisplacementQuery(ctx) {
  const r = await request(ctx.port, '/documentation.ico?x=1');
  const type = normType(r.headers['content-type']);
  const acao = r.headers['access-control-allow-origin'] || null;
  return {
    pass: r.status === 200 && type === 'icon' && acao === 'null' && expressInEffect(ctx.src),
    value: { status: r.status, type, 'access-control-allow-origin': acao },
  };
}

async function pExtObjectKeyRedirect(ctx) {
  const table = (ctx.routes && ctx.routes.externalPages) || {};
  const entry = table.github && typeof table.github === 'object' ? table.github.default : null;
  const r = await request(ctx.port, '/github');
  const location = r.headers['location'] || null;
  return {
    // An object-valued external page key is itself routable and redirects to
    // its own default target.
    pass:
      typeof entry === 'string' &&
      r.status >= 300 &&
      r.status < 400 &&
      location === entry &&
      expressInEffect(ctx.src),
    value: { status: r.status, location, expected: entry },
  };
}

async function pExtHistoryHideStatusOnly(ctx) {
  const headers = { 'sec-fetch-dest': 'document', cookie: 'HistoryHide=true' };
  const r = await request(ctx.port, '/documentation', { headers });
  const type = normType(r.headers['content-type']);
  return {
    // HistoryHide changes only the response status; the disguised loader page
    // and its content type must still be delivered.
    pass: r.status === 404 && type === 'html' && r.body.length > 0 && expressInEffect(ctx.src),
    value: { status: r.status, type, bytes: r.body.length },
  };
}

/* ------------------------------------------------------------------ *
 * BATCH-F2P-0004 (extension batch continues): duplicate/trailing slash
 * normalization across entry-point classes.
 *
 * The base `pSlash` predicate already requires that page-path mutants
 * (`///documentation//`, `/documentation/`) resolve to their canonical page.
 * These nodes extend the same observable contract -- instruction.md: "Express
 * MUST provide ... duplicate/trailing slash route normalization" -- to the root
 * path, static assets, disguised/`.ico` entry points and the auxiliary `/wisp/`
 * prefix.  Every predicate compares a slash mutant with the canonical path it
 * must normalize to and demands an identical observable response (status,
 * normalized content-type and body) plus Express ownership; no implementation
 * spelling is inspected.
 * ------------------------------------------------------------------ */

function slashResponseEqual(a, b) {
  return (
    a.status === b.status &&
    normType(a.headers['content-type']) === normType(b.headers['content-type']) &&
    a.body.equals(b.body)
  );
}

export async function pExtSlashEquivalent(ctx, mutantPath, canonicalPath) {
  const mutant = await request(ctx.port, mutantPath);
  const canonical = await request(ctx.port, canonicalPath);
  return {
    // The mutant must resolve to exactly the canonical response.  Comparing
    // bodies within a single run is stable even though `views/dist` is
    // randomized per build, so this stays a purely behavioural postcondition.
    pass:
      (canonicalPath === '/favicon.ico' ? faviconResponseValid(canonical) : canonical.status === 200) &&
      slashResponseEqual(mutant, canonical) &&
      expressInEffect(ctx.src),
    value: {
      mutant: mutantPath,
      canonical: canonicalPath,
      mutantStatus: mutant.status,
      canonicalStatus: canonical.status,
      mutantType: normType(mutant.headers['content-type']),
      canonicalType: normType(canonical.headers['content-type']),
      bodyEqual: mutant.body.equals(canonical.body),
    },
  };
}

/* ------------------------------------------------------------------ *
 * BATCH-F2P-0004 (extension batch continues): full Helmet header
 * fidelity, nested non-exempt disguise, and `.ico`-displaced raw-file
 * rejection.
 *
 * These nodes cover runtime-observable postconditions of the frozen
 * contract that the round-6 slash nodes did not exercise:
 *   - instruction.md:25  the response must carry the *full* Helmet header
 *                        set (with CSP disabled) verbatim, not merely
 *                        nosniff / CSP-absent / X-Powered-By-absent;
 *   - instruction.md:31  any non-exempt page path not ending in `.ico`
 *                        -- at any depth -- must return the loader page;
 *   - instruction.md:33  a `.ico` path whose stripped form is not a `pages`
 *                        key must resolve to the disguised 404, never to a
 *                        raw `views/dist/pages` file that is not a page key.
 * No implementation spelling is inspected; every node is gated on Express
 * owning the entry path.
 * ------------------------------------------------------------------ */

// The exact header values Helmet applies with CSP disabled, as frozen by the
// source Fastify baseline.  Only the values are asserted; how the middleware
// is constructed is not part of the observable contract.
const HELMET_HEADER_VALUES = {
  'strict-transport-security': 'max-age=31536000; includeSubDomains',
  'x-frame-options': 'SAMEORIGIN',
  'x-content-type-options': 'nosniff',
  'referrer-policy': 'no-referrer',
  'cross-origin-opener-policy': 'same-origin',
  'cross-origin-resource-policy': 'same-origin',
  'origin-agent-cluster': '?1',
  'x-dns-prefetch-control': 'off',
  'x-download-options': 'noopen',
  'x-permitted-cross-domain-policies': 'none',
  'x-xss-protection': '0',
};

async function pExtHelmetHeaderFidelity(ctx) {
  const r = await request(ctx.port, '/');
  const got = {};
  let ok = true;
  for (const [name, expected] of Object.entries(HELMET_HEADER_VALUES)) {
    const value = r.headers[name] || null;
    got[name] = value;
    ok = ok && value === expected;
  }
  const csp = r.headers['content-security-policy'] || null;
  const powered = r.headers['x-powered-by'] || null;
  return {
    // Every header the migrated server applies must still equal the value the
    // Helmet middleware produced before the migration; the wrong HSTS lifetime
    // is a real contract regression even though the header is still present.
    pass: ok && !csp && !powered && expressInEffect(ctx.src),
    value: {
      status: r.status,
      headers: got,
      'content-security-policy': csp,
      'x-powered-by': powered,
    },
  };
}

async function pExtNestedDisguiseLoader(ctx) {
  const canonical = await request(ctx.port, '/documentation');
  const nested = await request(ctx.port, '/not-a-real-page/deep');
  return {
    // A nested path that is non-exempt and does not end in `.ico` must deliver
    // the same loader page as a first-level disguised page.  Comparing bodies
    // within a single run stays stable despite per-build `views/dist`
    // randomization, so this remains a purely behavioural postcondition.
    pass:
      canonical.status === 200 &&
      normType(canonical.headers['content-type']) === 'html' &&
      nested.status === canonical.status &&
      normType(nested.headers['content-type']) === 'html' &&
      nested.body.equals(canonical.body) &&
      nested.body.length > 0 &&
      expressInEffect(ctx.src),
    value: {
      canonical: { status: canonical.status, type: normType(canonical.headers['content-type']) },
      nested: { status: nested.status, type: normType(nested.headers['content-type']) },
      bodyEqual: nested.body.equals(canonical.body),
    },
  };
}

export async function pExtIcoDisplacedRawRejected(ctx) {
  // These files exist under views/dist/pages, but none is a pages-map key.
  // Check nested files too: a single-segment page route can reject surf.html
  // while a later static mount still exposes nav/credits.html after rewriting.
  const targets = [
    '/surf.html.ico',
    '/nav/credits.html.ico',
    '/proxnav/scramjet.html.ico',
    '/misc/deobf/entry-point.html.ico',
    '/nav/credits.html.ico?x=1',
    '/nav/credits.html.ico/',
    '//nav//credits.html.ico',
  ];
  const rows = [];
  let ok = expressInEffect(ctx.src);
  for (const path of targets) {
    const r = await request(ctx.port, path);
    const type = normType(r.headers['content-type']);
    const acao = r.headers['access-control-allow-origin'] || null;
    const pass = r.status === 404 && type === 'icon' && acao === 'null';
    ok = ok && pass;
    rows.push({ path, status: r.status, type, 'access-control-allow-origin': acao, pass });
  }
  return { pass: ok, value: { rows } };
}

/* ------------------------------------------------------------------ *
 * BATCH-F2P-0004 (round 8): static-vs-disguise ordering.
 *
 * The disguise step must decide the response before the file system is
 * consulted.  A request for a raw page artifact that lives under
 * `views/dist/pages` (top-level, nested, with a query, or with a trailing
 * slash) is a non-exempt path that does not end in `.ico`, so instruction.md
 * requires the loader page (instruction.md:31) rather than the raw file, and a
 * non-exempt directory must not be turned into a directory redirect.  These
 * nodes therefore reject a migration whose static handler is consulted before
 * the disguise middleware (or that rewrites the URL before routing).
 *
 * Every node compares the target response with the in-run loader response, so
 * it stays stable despite per-build `views/dist` randomisation, and is gated on
 * the Express application owning the entry path.  No implementation spelling is
 * inspected.
 * ------------------------------------------------------------------ */

function loaderEquivalent(canonical, r) {
  return (
    r.status === 200 &&
    normType(r.headers['content-type']) === 'html' &&
    r.body.length > 0 &&
    r.body.equals(canonical.body)
  );
}

async function disguisedLoaderTargets(ctx, targets) {
  const canonical = await request(ctx.port, '/documentation');
  const rows = [];
  let ok = expressInEffect(ctx.src) && canonical.status === 200;
  for (const p of targets) {
    const r = await request(ctx.port, p);
    const pass = loaderEquivalent(canonical, r);
    ok = ok && pass;
    rows.push({ path: p, status: r.status, type: normType(r.headers['content-type']), pass });
  }
  return { pass: ok, value: { rows } };
}

async function pExtRawPageTopLevelDisguised(ctx) {
  return disguisedLoaderTargets(ctx, ['/frame.html', '/surf.html']);
}

async function pExtRawPageNestedDisguised(ctx) {
  return disguisedLoaderTargets(ctx, [
    '/nav/credits.html',
    '/proxnav/scramjet.html',
    '/misc/deobf/entry-point.html',
  ]);
}

async function pExtRawPageQueryDisguised(ctx) {
  return disguisedLoaderTargets(ctx, ['/frame.html?x=1', '/nav/credits.html?a=b']);
}

async function pExtRawPageTrailingSlashDisguised(ctx) {
  return disguisedLoaderTargets(ctx, ['/frame.html/', '/nav/credits.html/']);
}

async function pExtNonExemptDirectoryDisguised(ctx) {
  return disguisedLoaderTargets(ctx, ['/misc', '/nav', '/misc/deobf']);
}

async function pExtBareStaticPrefixDisguised404(ctx) {
  const targets = ['/assets', '/network'];
  const rows = [];
  let ok = expressInEffect(ctx.src);
  for (const p of targets) {
    const r = await request(ctx.port, p);
    const type = normType(r.headers['content-type']);
    const pass = r.status === 404 && type === 'icon';
    ok = ok && pass;
    rows.push({ path: p, status: r.status, type, pass });
  }
  return { pass: ok, value: { rows } };
}

const F2P_BASE_CHECK = {
  'F2P-001': pGithubIco,
  'F2P-002': pHelmet,
  'F2P-003': pFavicon,
  'F2P-004': pIcoStrip,
  'F2P-005': pUpgrade,
  'F2P-006': pDepPolicy,
  'F2P-007': pContentType,
  'F2P-008': pEsm,
  'F2P-009': pRootRoute,
  'F2P-010': pUpgrade,
  'F2P-011': pStaticPrecedence,
  'F2P-012': pSlash,
  'F2P-013': pHistoryHide,
  'F2P-014': pHistoryHide,
  'F2P-015': pGithub,
  'F2P-016': pRouterPage,
  'F2P-017': pHelmet,
  'F2P-018': pGithub,
  'F2P-019': pHelmet,
  'F2P-020': pSlash,
  'F2P-021': pNoXPoweredBy,
  'F2P-022': pEsm,
  'F2P-023': pNoXPoweredBy,
  'F2P-024': pPrefixed,
  'F2P-025': pRootRoute,
  'F2P-026': pHelmet,
  'F2P-027': pNoXPoweredBy,
  'F2P-028': pContentType,
  'F2P-029': pRouterPage,
  'F2P-030': pStaticPrecedence,
  'F2P-031': pRouterPage,
  'F2P-032': pUpgrade,
  'F2P-033': pHelmet,
  'F2P-034': pHistoryHide,
  'F2P-035': pHelmet,
  'F2P-036': pGithub,
  'F2P-037': pGithub,
  'F2P-038': pContentType,
  'F2P-039': pHistoryHide,
  'F2P-040': pGithub,
  'F2P-041': pNoXPoweredBy,
  'F2P-042': pHelmet,
  'F2P-043': pHelmet,
  'F2P-044': pSlash,
  'F2P-045': pContentType,
  'F2P-046': pStaticPrecedence,
  'F2P-047': pStaticPrecedence,
  'F2P-048': pHistoryHide,
  'F2P-049': pUpgrade,
  'F2P-050': pHistoryHide,
  'F2P-051': pHistoryHide,
  'F2P-052': pSlash,
  'F2P-053': pUpgrade,
  'F2P-054': pGithub,
  'F2P-055': pHelmet,
  'F2P-056': pStaticPrecedence,
  'F2P-057': pContentType,
  'F2P-058': pNoXPoweredBy,
  'F2P-059': pHistoryHide,
  'F2P-060': pStaticPrecedence,
  'F2P-061': pNoXPoweredBy,
  'F2P-062': pContentType,
  'F2P-063': pHistoryHide,
  'F2P-064': pHistoryHide,
  'F2P-065': pStaticPrecedence,
  'F2P-066': pGithub,
  'F2P-067': pEsm,
  'F2P-068': pHelmet,
  'F2P-069': pDepPolicy,
  'F2P-070': pHistoryHide,
  'F2P-071': pUpgrade,
  'F2P-072': pExpressInProcess,
};

/* ---- BATCH-F2P-0004 difficulty knob (single source of truth) ----
 * `F2P_EXTENSION_TIERS` maps every higher-order node to its difficulty tier.
 * `ACTIVE_F2P_EXTENSION_TIERS` selects which tiers are graded; disabling a tier
 * only ever removes nodes from the graded set (never marks them NA), so the same
 * candidate can only score higher when a tier is off.  The derived observation
 * inventory, config lists, source baseline, task counts and public integrity
 * manifest are regenerated from this declaration by
 * tools/sync_invisiproxy_f2p.py.
 */
export const F2P_EXTENSION_TIERS = {
  'F2P-073': 1,
  'F2P-074': 1,
  'F2P-075': 1,
  'F2P-076': 1,
  'F2P-077': 1,
  'F2P-078': 2,
  'F2P-079': 2,
  'F2P-080': 2,
  'F2P-081': 2,
  'F2P-082': 2,
  'F2P-083': 2,
  'F2P-084': 2,
  'F2P-085': 3,
  'F2P-086': 3,
  'F2P-087': 3,
  'F2P-088': 3,
  'F2P-089': 1,
  'F2P-090': 1,
  'F2P-091': 1,
  'F2P-092': 2,
  'F2P-093': 2,
  'F2P-094': 2,
  'F2P-095': 3,
  'F2P-096': 3,
  'F2P-097': 3,
  'F2P-098': 3,
  'F2P-099': 3,
  'F2P-100': 3,
  'F2P-101': 4,
  'F2P-102': 4,
  'F2P-103': 4,
  'F2P-104': 4,
  'F2P-105': 4,
  'F2P-106': 4,
};
export const ACTIVE_F2P_EXTENSION_TIERS = new Set([1, 2, 3, 4]);

const F2P_EXTENSION_CHECKS = {
  'F2P-073': pExtServiceWorker,
  'F2P-074': pExtIcoDisplacementPage,
  'F2P-075': pExtUnknownExtension404,
  'F2P-076': pExtGithubRedirectLocation,
  'F2P-077': pExtExternalStringRedirect,
  'F2P-078': pExtHistoryHideBoundary,
  'F2P-079': pExtDisguiseClassMatrix,
  'F2P-080': pExtUnknownIcoDisplaced404,
  'F2P-081': pExtSlashQueryNormalization,
  'F2P-082': pExtSecurityHeadersClasses,
  'F2P-083': pExtGithubUnknownDisguised404,
  'F2P-084': pExtDefaultEqualsLogin,
  'F2P-085': pExtUpgradeConditional,
  'F2P-086': pExtIcoDisplacementQuery,
  'F2P-087': pExtObjectKeyRedirect,
  'F2P-088': pExtHistoryHideStatusOnly,
  'F2P-089': (ctx) => pExtSlashEquivalent(ctx, '//', '/'),
  'F2P-090': (ctx) => pExtSlashEquivalent(ctx, '///', '/'),
  'F2P-091': (ctx) => pExtSlashEquivalent(ctx, '//s', '/s'),
  'F2P-092': (ctx) => pExtSlashEquivalent(ctx, '//assets/js/card.js', '/assets/js/card.js'),
  'F2P-093': (ctx) => pExtSlashEquivalent(ctx, '//documentation.ico', '/documentation.ico'),
  'F2P-094': (ctx) => pExtSlashEquivalent(ctx, '//wisp/', '/wisp/'),
  'F2P-095': (ctx) => pExtSlashEquivalent(ctx, '/documentation.ico/', '/documentation.ico'),
  'F2P-096': (ctx) => pExtSlashEquivalent(ctx, '/service.js/', '/service.js'),
  'F2P-097': (ctx) => pExtSlashEquivalent(ctx, '/favicon.ico/', '/favicon.ico'),
  'F2P-098': pExtHelmetHeaderFidelity,
  'F2P-099': pExtNestedDisguiseLoader,
  'F2P-100': pExtIcoDisplacedRawRejected,
  'F2P-101': pExtRawPageTopLevelDisguised,
  'F2P-102': pExtRawPageNestedDisguised,
  'F2P-103': pExtRawPageQueryDisguised,
  'F2P-104': pExtRawPageTrailingSlashDisguised,
  'F2P-105': pExtNonExemptDirectoryDisguised,
  'F2P-106': pExtBareStaticPrefixDisguised404,
};

const F2P_CHECK = { ...F2P_BASE_CHECK };
for (const [extensionId, tier] of Object.entries(F2P_EXTENSION_TIERS))
  if (ACTIVE_F2P_EXTENSION_TIERS.has(tier))
    F2P_CHECK[extensionId] = F2P_EXTENSION_CHECKS[extensionId];

const STATIC_F2P = new Set(['F2P-006', 'F2P-069', 'F2P-072']);
const STATIC_DIFF = new Set(['DIFF-002', 'DIFF-011', 'DIFF-022', 'DIFF-023', 'DIFF-024', 'DIFF-025']);

/* ------------------------------------------------------------------ *
 * Orchestration
 * ------------------------------------------------------------------ */

async function loadContext(repo, side) {
  const ctx = { side, repo, port: 8080, src: '', meta: {}, config: {}, routes: null, boot: null, bootOk: false, bootReason: null };
  try {
    ctx.src = stripComments(readFileSync(join(repo, 'src', 'server.mjs'), 'utf8'));
  } catch (err) {
    ctx.src = '';
    ctx.srcError = String((err && err.message) || err);
  }
  try {
    const routes = await import(pathToFileURL(join(repo, 'src', 'routes.mjs')).href);
    ctx.routes = routes;
    ctx.config = routes.config || {};
    const alt = (p) => (routes.getAltPrefix ? routes.getAltPrefix(p, routes.serverUrl.pathname) : '/' + p + '/');
    ctx.meta = {
      wisp: alt('wisp'),
      githubWisp: (((routes.flatAltPaths && routes.flatAltPaths['github/wisp']) || 'wisp')
        .split('/')
        .pop()),
      service: '/' + ((routes.flatAltPaths && routes.flatAltPaths['files/sw.js']) || 'sw.js'),
      serviceBlacklist:
        '/' + ((routes.flatAltPaths && routes.flatAltPaths['files/sw-blacklist.js']) || 'sw-blacklist.js'),
      staticPrefixes: ['assets', 'uv', 'scram', 'epoxy', 'libcurl', 'baremux'].map(alt),
      host: routes.serverUrl.hostname,
      port: routes.serverUrl.port,
      pathname: routes.serverUrl.pathname,
    };
    if (routes.serverUrl.port) ctx.port = Number(routes.serverUrl.port) || 8080;
  } catch (err) {
    ctx.meta = {
      wisp: '/wisp/',
      githubWisp: 'wisp',
      service: '/sw.js',
      serviceBlacklist: '/sw-blacklist.js',
      staticPrefixes: [],
      host: null,
      port: null,
      pathname: '/',
    };
    ctx.routesError = String((err && err.message) || err);
  }
  return ctx;
}

export async function run({ side = 'target' } = {}) {
  const repo = resolveRepo();
  const inventory = loadInventory();
  const ctx = await loadContext(repo, side);

  ctx.boot = await bootServer(repo, { port: ctx.port });
  ctx.bootOk = ctx.boot.ok;
  ctx.bootReason = ctx.boot.reason || null;

  const results = new Map();
  const emit = (id, kind, status, value) =>
    results.set(id, {
      id,
      kind,
      status,
      value: { type: 'json', value: value === undefined ? null : value },
    });

  for (const item of inventory.observations) {
    const id = item.id;
    try {
      if (id.startsWith('F2P-')) {
        const fn = F2P_CHECK[id];
        if (!fn) {
          emit(id, item.kind, 'INCONCLUSIVE', { reason: 'no-predicate-registered' });
          continue;
        }
        if (!ctx.bootOk && !STATIC_F2P.has(id)) {
          emit(id, item.kind, 'INCONCLUSIVE', { reason: 'server-not-running', detail: ctx.bootReason });
          continue;
        }
        const outcome = await fn(ctx);
        emit(id, item.kind, outcome.pass ? 'PASS' : 'FAIL', outcome.value);
      } else if (id.startsWith('P2P-')) {
        if (!ctx.bootOk) {
          emit(id, item.kind, 'INCONCLUSIVE', { reason: 'server-not-running', detail: ctx.bootReason });
          continue;
        }
        emit(id, item.kind, 'PASS', await p2pProjection(id, ctx));
      } else if (id.startsWith('DIFF-')) {
        if (!ctx.bootOk && !STATIC_DIFF.has(id)) {
          emit(id, item.kind, 'INCONCLUSIVE', { reason: 'server-not-running', detail: ctx.bootReason });
          continue;
        }
        emit(id, item.kind, 'PASS', await diffProjection(id, ctx));
      } else {
        emit(id, item.kind, 'INCONCLUSIVE', { reason: 'unknown-observation-kind' });
      }
    } catch (err) {
      emit(id, item.kind, 'INCONCLUSIVE', { error: String((err && err.message) || err) });
    }
  }

  stopServer(ctx.boot);

  const observations = inventory.observations.map(
    (item) =>
      results.get(item.id) || {
        id: item.id,
        kind: item.kind,
        status: 'INCONCLUSIVE',
        value: { type: 'json', value: null },
      }
  );
  return { schema_version: 'migration-observations:v6', observations };
}

export function writeReport(report, reportPath) {
  if (reportPath && reportPath !== '-') {
    try {
      mkdirSync(dirname(reportPath), { recursive: true });
    } catch {
      /* ignore */
    }
    writeFileSync(reportPath, JSON.stringify(report, null, 2) + '\n');
  }
  process.stdout.write(JSON.stringify(report) + '\n');
}
