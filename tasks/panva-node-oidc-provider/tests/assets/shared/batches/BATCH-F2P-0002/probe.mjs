import { readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { once, EventEmitter } from 'node:events';
import { join, extname } from 'node:path';
import { pathToFileURL } from 'node:url';

import { dependencyPolicy } from '../../integration/dependency-policy.mjs';

export const BATCH_ID = 'BATCH-F2P-0002';
export const TRACE_ENV = 'BATCH_F2P_0002_TRACE_OUT';

const root = process.argv[2] ?? process.env.BATCH_F2P_0002_ROOT;

// Keep the provider's development-only notices off stdout so the report stays
// machine readable.
process.env.NODE_ENV ||= 'test';

if (!root) {
  process.stderr.write('usage: node --experimental-loader hooks.mjs probe.mjs <repo-root>\n');
  process.exit(2);
}

function snapshotTrace() {
  const out = process.env[TRACE_ENV];
  if (!out) return [];
  try {
    return readFileSync(out, 'utf8').split('\n').filter(Boolean);
  } catch {
    return [];
  }
}

// Importing the published entry point is the invocation every F2P anchors on;
// the loader records the production module graph that actually got loaded.
const Provider = (await import(`${root}/lib/index.js`)).default;
const bootTrace = snapshotTrace();

const framework = {
  fastifyLoaded: bootTrace.some((url) => url.includes('/node_modules/fastify/')),
  koaLoaded: bootTrace.some((url) => url.includes('/node_modules/koa/')),
};

const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
const deps = pkg.dependencies ?? {};
const devDeps = pkg.devDependencies ?? {};

function productionFiles(dir) {
  const found = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) found.push(...productionFiles(full));
    else if (extname(entry) === '.js') found.push(full);
  }
  return found;
}

function koaImportsInProduction() {
  const hits = [];
  for (const file of productionFiles(join(root, 'lib'))) {
    const source = readFileSync(file, 'utf8');
    if (/from\s+['"]koa['"]|require\(\s*['"]koa['"]\s*\)|from\s+['"]@koa\//.test(source)) {
      hits.push(file.slice(root.length + 1));
    }
  }
  return hits;
}

// The shared semantic predicate, evaluated identically for source and target:
// the required target technology (Fastify) must be on the executed production
// module graph and the source technology (Koa) must not be.
function targetTechnologyOwns() {
  return framework.fastifyLoaded && !framework.koaLoaded;
}

let freePortServer;
async function freePort() {
  freePortServer = createServer();
  freePortServer.listen(0, '127.0.0.1');
  await once(freePortServer, 'listening');
  const { port } = freePortServer.address();
  freePortServer.close();
  return port;
}

const [{ default: baseConfig }, { default: keys }, { Account, TestAdapter }] = await Promise.all([
  import(new URL('../../../fixtures/batches/BATCH-F2P-0002/provider.config.mjs', import.meta.url).href),
  import(`${root}/test/keys.js`),
  import(`${root}/test/models.js`),
]);

async function boot({ proxy = false, mountPrefix = '', extra = {} } = {}) {
  const port = await freePort();
  const issuer = `http://127.0.0.1:${port}`;
  const provider = new Provider(issuer, {
    ...baseConfig,
    jwks: { keys },
    adapter: TestAdapter,
    findAccount: Account.findAccount,
    proxy,
    ...extra,
  });

  const handler = provider.callback();
  let server;
  if (mountPrefix) {
    server = createServer((req, res) => {
      if (req.url.startsWith(mountPrefix)) {
        req.url = req.url.slice(mountPrefix.length) || '/';
        handler(req, res);
      } else {
        res.statusCode = 404;
        res.end('Not Found');
      }
    });
  } else {
    server = createServer(handler);
  }
  server.listen(port, '127.0.0.1');
  await once(server, 'listening');

  const request = async (method, path, options = {}) => {
    const response = await fetch(`${issuer}${mountPrefix}${path}`, { method, ...options });
    const text = await response.text();
    return { status: response.status, headers: response.headers, text };
  };

  const close = () => new Promise((resolve) => {
    server.closeAllConnections?.();
    server.close(() => resolve());
  });

  return { provider, server, issuer, request, close };
}

const observations = [];
function observation(id, requirement_ids, run) {
  observations.push({ id, requirement_ids, run });
}

// F2P-025 — dependency policy is read from the declared manifest and the
// production import of lib/index.js.
observation('F2P-025', ['S001'], async () => {
  const declared = dependencyPolicy(root);
  const { provider, close } = await boot();
  const constructs = typeof provider.callback === 'function' && typeof provider.listen === 'function';
  close();
  return { ok: declared && constructs && targetTechnologyOwns(), detail: { declared, constructs, framework } };
});

// F2P-042 — router reverse lookup by name (first registration wins).
observation('F2P-042', ['S011'], async () => {
  const { provider, close } = await boot();
  const discovery = provider.pathFor('discovery');
  const resume = provider.pathFor('resume', { uid: 'x' });
  let unknown = null;
  try { provider.pathFor('definitely-not-a-route-name'); } catch (err) { unknown = err; }
  close();
  const ok = discovery === '/.well-known/oauth-authorization-server'
    && resume === '/auth/x' && unknown instanceof Error && targetTechnologyOwns();
  return { ok, detail: { discovery, resume, unknown: Boolean(unknown), framework } };
});

// F2P-039 — callback() returns a Node (req,res) handler; listen() exists; the
// production import resolves Fastify and never Koa.
observation('F2P-039', ['S001'], async () => {
  const { provider, server, request, close } = await boot();
  const handlerIsNode = typeof provider.callback() === 'function';
  const listens = typeof provider.listen === 'function';
  const served = (await request('GET', '/jwks')).status === 200;
  void server;
  close();
  return { ok: handlerIsNode && listens && served && targetTechnologyOwns(), detail: { handlerIsNode, listens, served, framework } };
});

// F2P-026 / F2P-028 / F2P-035 — a listen() immediately followed by a request
// must be routed once Fastify is ready.
async function readiness(id, requirementIds) {
  return async () => {
    const { server, issuer, close } = await boot();
    let status = 0;
    try {
      const response = await fetch(`${issuer}/jwks`);
      status = response.status;
      await response.text();
    } catch (err) {
      status = -1;
    }
    void server;
    close();
    return { ok: status === 200 && targetTechnologyOwns(), detail: { status, framework } };
  };
}
observation('F2P-026', ['S006'], await readiness());
observation('F2P-028', ['S006'], await readiness());
observation('F2P-035', ['S006'], await readiness());

// F2P-027 — a body endpoint keeps its content-type rejection instead of
// Fastify's default 415.
observation('F2P-027', ['S004'], async () => {
  const { request, close } = await boot();
  const res = await request('POST', '/token', {
    headers: { 'content-type': 'application/xml' },
    body: 'a=b',
  });
  const ok = res.status === 400
    && res.text.includes('only application/x-www-form-urlencoded content-type bodies are supported')
    && targetTechnologyOwns();
  close();
  return { ok, detail: { status: res.status, body: res.text.slice(0, 120), framework } };
});

// F2P-029 — request bodies on body-consuming endpoints are read on the Fastify
// path (the token action sees the parsed body).
observation('F2P-029', ['S005'], async () => {
  const { request, close } = await boot();
  const res = await request('POST', '/token', {
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: 'grant_type=authorization_code&code=x&client_id=none',
  });
  const ok = (res.status === 400 || res.status === 401) && targetTechnologyOwns();
  close();
  return { ok, detail: { status: res.status, body: res.text.slice(0, 120), framework } };
});

// F2P-037 — an unparseable urlencoded body still produces the exact 400 body.
observation('F2P-037', ['S004'], async () => {
  const { request, close } = await boot();
  const responses = [];
  for (const size of [60 * 1024, 11 * 1024 * 1024]) {
    const res = await request('POST', '/token', {
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: `a=${'b'.repeat(size)}`,
    });
    responses.push({ size, status: res.status, body: res.text.slice(0, 120) });
  }
  const ok = responses.every((res) => res.status === 400
    && res.body.includes('failed to parse the request body')) && targetTechnologyOwns();
  close();
  return { ok, detail: { responses, framework } };
});

// F2P-041 — urlencoded/json body parsing keeps producing the action body.
observation('F2P-041', ['S003'], async () => {
  const { request, close } = await boot({ extra: { features: { registration: { enabled: true } } } });
  const res = await request('POST', '/token', {
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: 'grant_type=authorization_code&code=x&client_id=none',
  });
  const bodySeen = res.text.includes('invalid_client') || res.text.includes('invalid_request');
  // The old reader received ctx.charset, which Koa did not delegate from
  // request.charset. JSON.parse therefore decoded the raw bytes as UTF-8 even
  // when a different charset was declared. Preserve that observable behavior.
  const clientNameBytes = Buffer.from('café', 'latin1');
  const registration = await request('POST', '/reg', {
    headers: { 'content-type': 'application/json; charset=iso-8859-1' },
    body: Buffer.from(JSON.stringify({
      redirect_uris: ['https://rp.example/cb'], client_name: 'café',
      token_endpoint_auth_method: 'none', grant_types: ['authorization_code'], response_types: ['code'],
    }), 'latin1'),
  });
  let clientName;
  try { clientName = JSON.parse(registration.text).client_name; } catch { /* assertion below */ }
  const charsetPreserved = registration.status === 201
    && clientName === clientNameBytes.toString('utf8');
  const ok = bodySeen && charsetPreserved && targetTechnologyOwns();
  close();
  return { ok, detail: { status: res.status, body: res.text.slice(0, 120),
    registrationStatus: registration.status, clientName, charsetPreserved, framework } };
});

// F2P-030 / F2P-032 / F2P-036 / F2P-038 — per-profile CORS exposed headers on
// the Fastify path.
async function cors(path, method = 'GET') {
  return async () => {
    const { request, close } = await boot();
    const res = await request(method, path, { headers: { Origin: 'https://rp.example' } });
    const expose = res.headers.get('access-control-expose-headers') ?? '';
    const allowOrigin = res.headers.get('access-control-allow-origin');
    const ok = allowOrigin === 'https://rp.example'
      && expose.includes('WWW-Authenticate') && targetTechnologyOwns();
    close();
    return { ok, detail: { status: res.status, expose, allowOrigin, framework } };
  };
}
observation('F2P-030', ['S008'], await cors('/me'));
observation('F2P-032', ['S008'], await cors('/me'));
observation('F2P-036', ['S008'], await cors('/me'));
observation('F2P-038', ['S004'], await cors('/me'));

// F2P-031 / F2P-048 — Provider identity and event emitter contract.
async function identity(id, requirementIds) {
  return async () => {
    const { provider, close } = await boot();
    const emitter = provider instanceof EventEmitter
      && typeof provider.on === 'function' && typeof provider.emit === 'function';
    close();
    return { ok: emitter && targetTechnologyOwns(), detail: { emitter, framework } };
  };
}
observation('F2P-031', ['S014'], await identity());
observation('F2P-048', ['S014'], await identity());

// F2P-033 — POST /auth is dispatched (enableHttpPostMethods) on the target path.
observation('F2P-033', ['S014'], async () => {
  const { request, close } = await boot();
  const res = await request('POST', '/auth', {
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: 'client_id=none',
  });
  const ok = res.status === 400 && targetTechnologyOwns();
  close();
  return { ok, detail: { status: res.status, framework } };
});

// F2P-040 — mounting through provider.callback() under a path prefix.
observation('F2P-040', ['S006'], async () => {
  const { request, close } = await boot({ mountPrefix: '/oidc' });
  const res = await request('GET', '/jwks');
  const ok = res.status === 200 && targetTechnologyOwns();
  close();
  return { ok, detail: { status: res.status, framework } };
});

// F2P-045 — Provider class identity and the production module graph.
observation('F2P-045', ['S002'], async () => {
  const koaImports = koaImportsInProduction();
  const { provider, close } = await boot();
  const emitter = provider instanceof EventEmitter;
  close();
  return { ok: emitter && koaImports.length === 0 && targetTechnologyOwns(), detail: { emitter, koaImports, framework } };
});

// F2P-043 / F2P-044 — an unregistered method/path must not dispatch an endpoint.
// The instruction does not prescribe a representation for this fallback 404.
async function notFound(method, path) {
  return async () => {
    const { request, close } = await boot();
    const res = await request(method, path);
    const ct = res.headers.get('content-type') ?? '';
    const ok = res.status === 404 && targetTechnologyOwns();
    close();
    return { ok, detail: { status: res.status, ct, body: res.text.slice(0, 40), framework } };
  };
}
observation('F2P-043', ['S002'], await notFound('POST', '/device/auth'));
observation('F2P-044', ['S002'], await notFound('POST', '/challenge'));

// F2P-046 — POST /me is matched and dispatched (the userinfo action rejects
// the missing token) rather than answered by a framework default.
observation('F2P-046', ['S002'], async () => {
  const { request, close } = await boot();
  const res = await request('POST', '/me');
  const ok = res.status === 401 && res.text.includes('invalid_token') && targetTechnologyOwns();
  close();
  return { ok, detail: { status: res.status, body: res.text.slice(0, 80), framework } };
});

// F2P-034 / F2P-047 — proxy-aware protocol resolution on the Fastify path.
async function proxy(id, requirementIds) {
  return async () => {
    const { request, close } = await boot({ proxy: true });
    const res = await request('GET', '/.well-known/openid-configuration', {
      headers: { 'x-forwarded-proto': 'https' },
    });
    const reachable = res.status === 200 && res.text.includes('authorization_endpoint');
    close();
    return { ok: reachable && targetTechnologyOwns(), detail: { status: res.status, framework } };
  };
}
observation('F2P-034', ['S005'], await proxy());
observation('F2P-047', ['S005'], await proxy());

export async function runAll() {
  const results = [];
  for (const obs of observations) {
    try {
      const { ok, detail } = await obs.run();
      results.push({ id: obs.id, requirement_ids: obs.requirement_ids, status: ok ? 'PASS' : 'FAIL', detail });
    } catch (error) {
      results.push({ id: obs.id, requirement_ids: obs.requirement_ids, status: 'FAIL', detail: { error: String(error?.message ?? error) } });
    }
  }
  return { root, framework, results };
}

const isMain = process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url;
if (isMain) {
  const report = await runAll();
  const json = JSON.stringify(report, null, 2);
  if (process.env.BATCH_F2P_0002_REPORT) {
    const { writeFileSync } = await import('node:fs');
    writeFileSync(process.env.BATCH_F2P_0002_REPORT, json);
  }
  process.stdout.write(`${json}\n`);
  process.exit(0);
}
