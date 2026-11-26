// Shared F2P harness for BATCH-F2P-0001.
//
// Every observation of this batch is run by the *same* semantic predicate on
// both sides. The predicate is a target postcondition; it is paired with a
// runtime probe that proves the request path is owned by the required target
// technology (Fastify, migration.target) and not by the source technology
// (Koa, migration.source). `run(repo)` therefore returns, for each observation,
// the raw observation of this side plus the technology that served it.
//
// The harness never hardcodes PASS/FAIL by role: `behavior` is the raw result of
// the predicate and `fastify` is the raw result of the technology probe. F2P is
// a target-only postcondition, so the caller derives PASS only when both are
// observed on the target side.

import { createServer } from 'node:http';
import { once } from 'node:events';
import { createRequire, register } from 'node:module';
import { channel } from 'node:diagnostics_channel';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

import { dependencyPolicy } from '../../integration/dependency-policy.mjs';

export const OBSERVATION_IDS = [
  'F2P-001', 'F2P-002', 'F2P-003', 'F2P-004', 'F2P-005', 'F2P-006',
  'F2P-007', 'F2P-008', 'F2P-009', 'F2P-010', 'F2P-011', 'F2P-012',
  'F2P-013', 'F2P-014', 'F2P-015', 'F2P-016', 'F2P-017', 'F2P-018',
  'F2P-019', 'F2P-020', 'F2P-021', 'F2P-022', 'F2P-023', 'F2P-024',
];

const GENERIC_ERROR = 'oops! something went wrong';

function repoModules(repo) {
  const require = createRequire(path.join(repo, 'package.json'));
  const resolve = (name) => pathToFileURL(require.resolve(name)).href;
  return { require, resolve };
}

// ---------------------------------------------------------------------------
// minimal provider materialization (no dependency on the repo's test helpers)
// ---------------------------------------------------------------------------

const storage = new Map();
const upserts = new Map();

class MemoryAdapter {
  constructor(name) { this.name = name; }

  key(id) { return `${this.name}:${id}`; }

  async upsert(id, payload, expiresIn) {
    upserts.set(this.name, (upserts.get(this.name) || 0) + 1);
    storage.set(this.key(id), { payload, expiresIn });
  }

  async find(id) { return storage.get(this.key(id))?.payload; }

  async findByUserCode(code) {
    return [...storage.values()].map((x) => x.payload).find((p) => p.userCode === code);
  }

  async findByUid(uid) {
    return [...storage.values()].map((x) => x.payload).find((p) => p.uid === uid);
  }

  async consume(id) {
    const entry = storage.get(this.key(id));
    if (entry) entry.payload.consumed = Math.floor(Date.now() / 1000);
  }

  async destroy(id) { storage.delete(this.key(id)); }

  async revokeByGrantId(gid) {
    for (const [key, value] of storage) if (value.payload.grantId === gid) storage.delete(key);
  }
}

function testClient(extraGrantTypes = []) {
  return {
    client_id: 'client',
    client_secret: 'secret',
    redirect_uris: ['https://rp.example/cb'],
    response_types: ['code'],
    grant_types: ['authorization_code', 'refresh_token', ...extraGrantTypes],
    token_endpoint_auth_method: 'client_secret_basic',
  };
}

const FEATURES = {
  dPoP: { nonceSecret: Buffer.alloc(32) },
  deviceFlow: { enabled: true },
  revocation: { enabled: true },
  introspection: { enabled: true },
  registration: { enabled: true },
  registrationManagement: { enabled: true },
  pushedAuthorizationRequests: { enabled: true },
  ciba: { enabled: true },
};

function baseConfig(jose, keys, extra) {
  return {
    clients: [testClient()],
    jwks: { keys },
    adapter: MemoryAdapter,
    cookies: { long: { sameSite: 'none' } },
    features: FEATURES,
    enableHttpPostMethods: true,
    findAccount: (_ctx, id) => ({ accountId: id, claims: () => ({ sub: id }) }),
    ...extra,
  };
}

async function makeKeys(jose) {
  const keys = [];
  for (const alg of ['RS256', 'ES256']) {
    const kp = await jose.generateKeyPair(alg, { extractable: true });
    keys.push(await jose.exportJWK(kp.privateKey));
  }
  return keys;
}

// ---------------------------------------------------------------------------
// technology probe: observe whether the live request path is owned by Fastify
// ---------------------------------------------------------------------------

function installTechProbe(repo) {
  const state = { fastify: false };
  // Observe Fastify before importing the provider. createContext is an
  // out-of-scope extension API; its argument layout is not an HTTP contract.
  // Replacing require.cache exports alone does not replace the ESM default
  // namespace cached when Node loaded the CommonJS module. Instrument ESM
  // imports through a loader as well as direct CommonJS calls.
  const { require } = repoModules(repo);
  const modulePath = require.resolve('fastify');
  const original = require(modulePath);
  const wrapped = new Proxy(original, {
    get(factory, key, receiver) {
      if (key === 'fastify' || key === 'default') return wrapped;
      return Reflect.get(factory, key, receiver);
    },
    apply(factory, receiver, args) {
      const app = Reflect.apply(factory, receiver, args);
      app.addHook('onSend', (request, reply, payload, done) => {
        // Raw responses sent after reply.hijack() do not run this hook.
        if (reply.request === request && request.raw && reply.raw) state.fastify = true;
        done(null, payload);
      });
      return app;
    },
  });
  require.cache[modulePath].exports = wrapped;
  channel('migration.fastify.onSend').subscribe(() => { state.fastify = true; });
  register(new URL('./fastify-loader.mjs', import.meta.url), {
    parentURL: import.meta.url,
    data: { target: pathToFileURL(modulePath).href },
  });
  return state;
}

// ---------------------------------------------------------------------------
// request helpers
// ---------------------------------------------------------------------------

async function call(origin, route, options = {}) {
  const response = await fetch(origin + route, options);
  const text = await response.text();
  const headers = Object.fromEntries(response.headers.entries());
  // Node exposes one entry per Set-Cookie header. Object.fromEntries keeps
  // only the last one, so collect all cookies before checking the HTTP contract.
  headers['set-cookie'] = response.headers.getSetCookie().join('\n');
  return { status: response.status, headers, text };
}

function json(text) {
  try { return JSON.parse(text); } catch { return undefined; }
}

// ---------------------------------------------------------------------------
// observation predicates
// ---------------------------------------------------------------------------

function makeContext(repo, Provider, tech) {
  let provider;
  let origin;
  let close;

  async function boot({ proxy = true, extra = {} } = {}) {
    const { resolve } = repoModules(repo);
    const jose = await import(resolve('jose'));
    const keys = await makeKeys(jose);
    provider = new Provider('http://127.0.0.1', baseConfig(jose, keys, extra));
    provider.proxy = proxy;
    const server = createServer(provider.callback());
    server.listen(0);
    await once(server, 'listening');
    origin = `http://127.0.0.1:${server.address().port}`;
    close = () => new Promise((r) => server.close(r));
    return { provider, origin };
  }

  async function corsProfiles() {
    const me = await call(origin, '/me', {
      headers: { Origin: 'https://rp.example', Authorization: 'Bearer x' },
    });
    const jwks = await call(origin, '/jwks', { headers: { Origin: 'https://rp.example' } });
    const meExpose = me.headers['access-control-expose-headers'] || '';
    const jwksExpose = jwks.headers['access-control-expose-headers'] || '';
    return meExpose.includes('DPoP-Nonce')
      && meExpose.includes('WWW-Authenticate')
      && !meExpose.includes('OAuth-Client-Attestation-Challenge')
      && meExpose !== jwksExpose;
  }

  async function proxyDiscovery() {
    const body = await call(origin, '/.well-known/openid-configuration', {
      headers: { 'X-Forwarded-Proto': 'https' },
    });
    const document = json(body.text);
    return Boolean(document)
      && String(document.authorization_endpoint || '').startsWith('https://')
      && String(document.token_endpoint || '').startsWith('https://');
  }

  async function bootstrapImmediate(route) {
    const { resolve } = repoModules(repo);
    const jose = await import(resolve('jose'));
    const keys = await makeKeys(jose);
    const p = new Provider('http://127.0.0.1', baseConfig(jose, keys, {}));
    const server = createServer(p.callback());
    server.listen(0);
    await once(server, 'listening');
    try {
      const probe = await call(`http://127.0.0.1:${server.address().port}`, route);
      return probe.status === 200 || probe.status === 401 || probe.status === 400;
    } finally {
      await new Promise((r) => server.close(r));
    }
  }

  async function notFound() {
    const response = await call(origin, '/definitely-not-a-route');
    // No provider endpoint is registered here. Preserve non-dispatch (404),
    // without inventing a contract for the fallback body's spelling or type.
    return response.status === 404;
  }

  async function routeEquivalence() {
    const opts = {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: 'grant_type=authorization_code',
    };
    const canonical = await call(origin, '/token', opts);
    const upper = await call(origin, '/TOKEN', opts);
    const slash = await call(origin, '/token/', opts);
    const ordinary = canonical.status === 400
      && upper.status === canonical.status
      && upper.text === canonical.text
      && slash.status === canonical.status
      && slash.text === canonical.text;
    if (!ordinary) return false;

    // Exercise the live callback, not just the standalone Router helper.
    // Fastify decodes static segments before matching; the previous router
    // matched their literal spelling. These must not become /token or /jwks.
    for (const route of ['/t%6Fken', '/to%E2%84%AAen', '/%74oken']) {
      const response = await call(origin, route, opts);
      if (response.status !== 404) {
        throw new Error(`literal route ${route}: expected 404, got ${response.status}`);
      }
    }

    // A malformed escape inside a parameter was retained verbatim, and the
    // old router imposed no parameter length cap. Both cases still enter the
    // registration endpoint and produce its normal missing-token response.
    const client = await call(origin, '/reg/nonexistent');
    if (client.status !== 401) return false;
    for (const segment of ['%', '%E0%A4%A', '%FF', 'a'.repeat(256), 'a%2Fb']) {
      const response = await call(origin, `/reg/${segment}`);
      if (response.status !== client.status || response.text !== client.text
        || response.headers['www-authenticate'] !== client.headers['www-authenticate']) {
        throw new Error(`client route ${segment}: expected endpoint 401, got ${response.status}`);
      }
    }
    const get = await call(origin, '/jwks');
    const head = await call(origin, '/jwks', { method: 'HEAD' });
    return get.status === 200 && head.status === get.status && head.text === '';
  }

  async function unsupportedContentType() {
    const response = await call(origin, '/token', {
      method: 'POST',
      headers: { 'content-type': 'application/xml' },
      body: 'a=b',
    });
    const body = json(response.text) || {};
    return response.status === 400
      && body.error_description === 'only application/x-www-form-urlencoded content-type bodies are supported on POST /token';
  }

  async function bodySizeCap() {
    const payload = `grant_type=authorization_code&junk=${'x'.repeat(60 * 1024)}`;
    const response = await call(origin, '/token', {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: payload,
    });
    const body = json(response.text) || {};
    return response.status === 400 && body.error_description === 'failed to parse the request body';
  }

  async function sessionPersistence() {
    const session = new provider.Session({
      jti: 'session-persist-probe',
      loginTs: Math.floor(Date.now() / 1000),
      accountId: 'account-1',
    });
    session.authorizations = {};
    await session.save(3600);
    const before = upserts.get('Session') || 0;
    await call(origin, `/auth?client_id=client&response_type=code&scope=openid&redirect_uri=${encodeURIComponent('https://rp.example/cb')}`, {
      headers: { cookie: '_session=session-persist-probe' },
      redirect: 'manual',
    });
    await new Promise((r) => setTimeout(r, 100));
    const after = upserts.get('Session') || 0;
    return after - before === 1;
  }

  async function providerIdentity() {
    const { resolve } = repoModules(repo);
    const { default: EventEmitter } = await import('node:events');
    let Koa;
    try {
      Koa = (await import(resolve('koa'))).default;
    } catch { Koa = undefined; }
    const p = new Provider('http://127.0.0.1', baseConfig(await repoJose(repo), await makeKeys(await repoJose(repo)), {}));
    let emitted = false;
    p.once('access_token.issued', () => { emitted = true; });
    p.emit('access_token.issued');
    return p instanceof EventEmitter === true
      && (Koa ? p instanceof Koa === false : true)
      && emitted === true;
  }

  async function cookieJarIdentity() {
    // Both access paths must share HTTP cookie state, not wrapper identity.
    const { resolve } = repoModules(repo);
    const jose = await import(resolve('jose'));
    const p = new Provider('http://127.0.0.1', baseConfig(jose, await makeKeys(jose), {
      clients: [testClient(['cookie_probe'])],
    }));
    p.registerGrantType('cookie_probe', (ctx) => {
      const options = { signed: false, secure: false, httpOnly: true, path: '/' };
      ctx.cookies.set('context_probe', 'one', options);
      ctx.oidc.cookies.set('oidc_probe', 'two', options);
      ctx.body = {
        contextRead: ctx.cookies.get('incoming_probe', options),
        oidcRead: ctx.oidc.cookies.get('incoming_probe', options),
      };
    });
    const server = createServer(p.callback());
    server.listen(0);
    await once(server, 'listening');
    try {
      const response = await call(`http://127.0.0.1:${server.address().port}`, '/token', {
        method: 'POST',
        headers: {
          'content-type': 'application/x-www-form-urlencoded',
          Authorization: `Basic ${Buffer.from('client:secret').toString('base64')}`,
          Cookie: 'incoming_probe=readable',
        },
        body: 'grant_type=cookie_probe',
      });
      const body = json(response.text) || {};
      const cookies = response.headers['set-cookie'] || '';
      const ok = response.status === 200 && body.contextRead === 'readable'
        && body.oidcRead === 'readable' && cookies.includes('context_probe=one;')
        && cookies.includes('oidc_probe=two;');
      if (!ok) {
        throw new Error(`cookie HTTP contract failed: ${JSON.stringify({
          status: response.status, body: response.text.slice(0, 1000), setCookie: cookies,
        })}`);
      }
      return true;
    } finally {
      await new Promise((r) => server.close(r));
    }
  }

  async function genericErrorBody() {
    const { resolve } = repoModules(repo);
    const jose = await import(resolve('jose'));
    const keys = await makeKeys(jose);
    const p = new Provider('http://127.0.0.1', baseConfig(jose, keys, { clients: [testClient(['boom'])] }));
    p.registerGrantType('boom', () => { throw new Error('boom'); });
    const server = createServer(p.callback());
    server.listen(0);
    await once(server, 'listening');
    try {
      const basic = `Basic ${Buffer.from('client:secret').toString('base64')}`;
      const response = await call(`http://127.0.0.1:${server.address().port}`, '/token', {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded', Accept: 'application/json', Authorization: basic },
        body: 'grant_type=boom',
      });
      const body = json(response.text) || {};
      return response.status === 500 && body.error === 'server_error' && body.error_description === GENERIC_ERROR;
    } finally {
      await new Promise((r) => server.close(r));
    }
  }

  return {
    boot,
    close: () => close?.(),
    get provider() { return provider; },
    get origin() { return origin; },
    corsProfiles,
    proxyDiscovery,
    bootstrapImmediate,
    notFound,
    routeEquivalence,
    unsupportedContentType,
    bodySizeCap,
    sessionPersistence,
    providerIdentity,
    cookieJarIdentity,
    genericErrorBody,
  };
}

async function repoJose(repo) {
  const { resolve } = repoModules(repo);
  return import(resolve('jose'));
}

// ---------------------------------------------------------------------------
// per-observation table. `entry` is the route/entry named by the atomic claim;
// `behavior` is the frozen target postcondition (S00x/R027) predicate.
// ---------------------------------------------------------------------------

function observations() {
  return [
    { id: 'F2P-001', requirement: 'S008', entry: { method: 'GET', route: '/session/end/success' }, predicate: (c) => c.corsProfiles() },
    { id: 'F2P-002', requirement: 'S006', entry: { method: 'POST', route: '/device/auth' }, predicate: (c) => c.bootstrapImmediate('/jwks') },
    { id: 'F2P-003', requirement: 'S010', entry: { method: 'POST', route: '/TOKEN' }, predicate: (c) => c.routeEquivalence() },
    { id: 'F2P-004', requirement: 'S003', entry: { method: 'POST', route: '/token' }, predicate: (c) => c.unsupportedContentType() },
    { id: 'F2P-005', requirement: 'S006', entry: { method: 'POST', route: '/backchannel' }, predicate: (c) => c.bootstrapImmediate('/jwks') },
    { id: 'F2P-006', requirement: 'S002', entry: { method: 'POST', route: '/interaction/demo' }, predicate: (c) => c.notFound() },
    { id: 'F2P-007', requirement: 'S009', entry: { method: 'GET', route: '/auth' }, predicate: (c) => c.sessionPersistence() },
    { id: 'F2P-008', requirement: 'S014', entry: { method: 'DELETE', route: '/reg/client' }, predicate: (c) => c.providerIdentity() },
    { id: 'F2P-009', requirement: 'S013', entry: { method: 'GET', route: '/auth' }, predicate: (c) => c.cookieJarIdentity() },
    { id: 'F2P-010', requirement: 'S005', entry: { method: 'GET', route: '/session/end/success' }, predicate: (c) => c.proxyDiscovery() },
    { id: 'F2P-011', requirement: 'S004', entry: { method: 'POST', route: '/token' }, predicate: (c) => c.bodySizeCap() },
    { id: 'F2P-012', requirement: 'S008', entry: { method: 'POST', route: '/token/revocation' }, predicate: (c) => c.corsProfiles() },
    { id: 'F2P-013', requirement: 'S012', entry: { method: 'POST', route: '/token' }, predicate: (c) => c.genericErrorBody() },
    { id: 'F2P-014', requirement: 'S008', entry: { method: 'POST', route: '/device/auth' }, predicate: (c) => c.corsProfiles() },
    { id: 'F2P-015', requirement: 'S005', entry: { method: 'GET', route: '/.well-known/openid-configuration' }, predicate: (c) => c.proxyDiscovery() },
    { id: 'F2P-016', requirement: 'S008', entry: { method: 'HEAD', route: '/jwks' }, predicate: (c) => c.corsProfiles() },
    { id: 'F2P-017', requirement: 'S006', entry: { method: 'GET', route: '/jwks' }, predicate: (c) => c.bootstrapImmediate('/jwks') },
    { id: 'F2P-018', requirement: 'S008', entry: { method: 'POST', route: '/session/end/confirm' }, predicate: (c) => c.corsProfiles() },
    { id: 'F2P-019', requirement: 'S005', entry: { method: 'GET', route: '/.well-known/openid-configuration' }, predicate: (c) => c.proxyDiscovery() },
    { id: 'F2P-020', requirement: 'S014', entry: { method: 'GET', route: '/jwks' }, predicate: (c) => c.providerIdentity() },
    { id: 'F2P-021', requirement: 'S013', entry: { method: 'GET', route: '/auth' }, predicate: (c) => c.cookieJarIdentity() },
    { id: 'F2P-022', requirement: 'S005', entry: { method: 'GET', route: '/.well-known/openid-configuration' }, predicate: (c) => c.proxyDiscovery() },
    { id: 'F2P-023', requirement: 'R027', entry: { method: 'GET', route: '/jwks' }, predicate: (c) => dependencyPolicy(c.repo) },
    { id: 'F2P-024', requirement: 'S002', entry: { method: 'GET', route: '/definitely-not-a-route' }, predicate: (c) => c.notFound() },
  ];
}

// ---------------------------------------------------------------------------
// runner
// ---------------------------------------------------------------------------

export async function run(repo) {
  const { resolve } = repoModules(repo);
  const tech = installTechProbe(repo);
  const Provider = (await import(pathToFileURL(path.join(repo, 'lib/index.js')).href)).default;

  const results = [];
  for (const observation of observations()) {
    const context = makeContext(repo, Provider, tech);
    context.repo = repo;
    let behavior = false;
    let detail = '';
    tech.fastify = false;
    try {
      await context.boot();
      if (observation.entry) {
        await call(context.origin, observation.entry.route, { method: observation.entry.method, redirect: 'manual' });
        void resolve;
      }
      behavior = await observation.predicate(context);
    } catch (error) {
      behavior = false;
      detail = `${error?.name || 'Error'}: ${error?.message || error}`;
    } finally {
      try { await context.close(); } catch {}
    }
    results.push({
      id: observation.id,
      requirement: observation.requirement,
      entry: observation.entry,
      behavior,
      fastify: tech.fastify,
      detail,
    });
  }

  return { repo, results };
}
