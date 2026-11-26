import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import http from 'node:http';
import { EventEmitter } from 'node:events';

import { dependencyDetails } from '../../integration/dependency-policy.mjs';

import {
  defaultConfig,
  discoveryPath,
  jwksPath,
  notFoundPath,
  origin,
  oversizedUrlEncoded,
  preflight,
  tokenForm,
  unsupportedContentType,
} from '../../../fixtures/batches/BATCH-F2P-0003/requests.mjs';

/*
 * Shared probe library for BATCH-F2P-0003.
 *
 * Every F2P observation in this batch is a target-only postcondition: the
 * source technology (Koa 3.2.1) must fail the predicate and the target
 * technology (Fastify 5.12.0) must satisfy it. The probes are executed against
 * a materialized provider root supplied through PROBE_ROOT:
 *
 *   source side -> a checkout of the source repository
 *   target side -> a checkout of the reference target repository
 *
 * A probe combines two assertions:
 *   1. the concrete invocation described by the observation's required
 *      postcondition actually runs through the provider's request handling,
 *   2. that handling path is owned by the target technology (a Fastify-backed
 *      provider) rather than the source technology (a Koa application).
 *
 * The second assertion is the discriminator: the observable HTTP output is
 * deliberately preserved across the migration, so the technology ownership of
 * the execution path is what makes the postcondition fail on the source.
 */

export { defaultConfig };

function root() {
  const value = process.env.PROBE_ROOT;
  if (!value) {
    throw new Error('PROBE_ROOT is not set');
  }
  return value;
}

export async function loadProvider() {
  const module = await import(`${root()}/lib/index.js`);
  return module.default;
}

function loadKoa() {
  try {
    const require = createRequire(`${root()}/package.json`);
    return require('koa');
  } catch {
    return undefined;
  }
}

/*
 * Target technology ownership. On the source the Provider is a Koa application
 * (its class chain reaches Koa's Application); on the target it is an
 * EventEmitter whose request path is a Fastify instance.
 */
export async function fastifyOwns(provider) {
  const Koa = loadKoa();
  let koaBacked;
  if (Koa) {
    koaBacked = provider instanceof Koa;
  } else {
    const parent = Object.getPrototypeOf(provider.constructor.prototype);
    koaBacked = Boolean(parent && parent.constructor && parent.constructor.name === 'Application');
  }
  const eventEmitter = provider instanceof EventEmitter;
  return {
    ok: !koaBacked && eventEmitter,
    detail: { koaBacked, eventEmitter },
  };
}

export async function newProvider(setup = {}, { proxy } = {}) {
  const Provider = await loadProvider();
  const provider = new Provider('https://op.example', { ...defaultConfig, ...setup });
  if (proxy !== undefined) {
    provider.proxy = proxy;
  }
  return provider;
}

export async function serve(setup = {}, { proxy } = {}) {
  const provider = await newProvider(setup, { proxy });
  const server = http.createServer(provider.callback());
  await new Promise((resolve) => server.listen(0, resolve));
  const { port } = server.address();
  const base = `http://localhost:${port}`;
  const request = async (path, options = {}) => {
    const response = await fetch(base + path, options);
    const text = await response.text();
    return { status: response.status, headers: Object.fromEntries(response.headers), text };
  };
  const close = () => new Promise((resolve) => server.close(resolve));
  return { provider, server, base, request, close };
}

export const observationMeta = {
  'F2P-072': { requirement_ids: ['S007'], surface: 'lib/shared/cors.js::per-profile CORS header values and exposed headers' },
  'F2P-069': { requirement_ids: ['S001'], surface: 'lib/index.js::runtime entry' },
  'F2P-049': { requirement_ids: ['S006'], surface: 'lib/provider.js::POST /token/introspection' },
  'F2P-050': { requirement_ids: ['S004'], surface: 'lib/shared/selective_body.js::rejection of unparseable request bodies' },
  'F2P-051': { requirement_ids: ['S008'], surface: 'lib/helpers/initialize_app.js::per-profile CORS header values and exposed headers' },
  'F2P-052': { requirement_ids: ['S006'], surface: 'lib/provider.js::POST /device' },
  'F2P-053': { requirement_ids: ['S008'], surface: 'lib/helpers/initialize_app.js::CORS preflight OPTIONS responses' },
  'F2P-054': { requirement_ids: ['S014'], surface: 'lib/provider.js::GET /jwks' },
  'F2P-055': { requirement_ids: ['S003'], surface: 'lib/actions/token.js::request-body parsing for urlencoded and JSON bodies' },
  'F2P-056': { requirement_ids: ['S014'], surface: 'lib/provider.js::interaction and session cookie reading and writing' },
  'F2P-057': { requirement_ids: ['S002'], surface: 'lib/provider.js::POST /request' },
  'F2P-058': { requirement_ids: ['S008'], surface: 'lib/helpers/initialize_app.js::POST /token' },
  'F2P-059': { requirement_ids: ['S005'], surface: 'lib/provider.js::mounting under a path prefix' },
  'F2P-060': { requirement_ids: ['S014'], surface: 'lib/provider.js::POST /token' },
  'F2P-061': { requirement_ids: ['S006'], surface: 'lib/provider.js::POST /session/end/confirm' },
  'F2P-062': { requirement_ids: ['S010'], surface: 'lib/helpers/router.js::route-name resolution and exposed router fields' },
  'F2P-063': { requirement_ids: ['S014'], surface: 'lib/provider.js::provider.callback() and provider.listen()' },
  'F2P-064': { requirement_ids: ['S008'], surface: 'lib/helpers/initialize_app.js::POST /auth' },
  'F2P-065': { requirement_ids: ['S008'], surface: 'lib/helpers/initialize_app.js::GET /device/:uid' },
  'F2P-066': { requirement_ids: ['S002'], surface: 'lib/provider.js::session and interaction persistence timing' },
  'F2P-067': { requirement_ids: ['S006'], surface: 'lib/index.js::public capabilities and their contracts' },
  'F2P-068': { requirement_ids: ['S008'], surface: 'lib/helpers/initialize_app.js::GET /auth' },
  'F2P-070': { requirement_ids: ['S008'], surface: 'lib/helpers/initialize_app.js::POST /reg' },
  'F2P-071': { requirement_ids: ['S008'], surface: 'lib/helpers/initialize_app.js::DELETE /reg/:clientId' },
};

const text = (result) => (result && result.text ? result.text : '');

async function checkRuntimeEntry() {
  const { fastifyPinned, koaRuntime } = dependencyDetails(root());
  const Provider = await loadProvider();
  const provider = new Provider('https://op.example', defaultConfig);
  const tech = await fastifyOwns(provider);
  return {
    ok: fastifyPinned && !koaRuntime && tech.ok,
    detail: { fastifyPinned, koaRuntime, ...tech.detail },
  };
}

async function checkNotFound() {
  const { provider, request, close } = await serve();
  const tech = await fastifyOwns(provider);
  const result = await request(notFoundPath);
  await close();
  return {
    // Registered endpoints retain their error contracts; this URL is not one.
    ok: tech.ok && result.status === 404,
    detail: { status: result.status, contentType: result.headers['content-type'], body: text(result), ...tech.detail },
  };
}

async function checkUnsupportedContentType() {
  const { provider, request, close } = await serve();
  const tech = await fastifyOwns(provider);
  const result = await request('/token', unsupportedContentType);
  await close();
  let body = {};
  try { body = JSON.parse(text(result)); } catch { body = {}; }
  return {
    ok: tech.ok && result.status === 400 && body.error === 'invalid_request'
      && /only application\/x-www-form-urlencoded content-type bodies are supported on POST \/token/.test(body.error_description || ''),
    detail: { status: result.status, body, ...tech.detail },
  };
}

async function checkBodySizeCap() {
  const { provider, request, close } = await serve();
  const tech = await fastifyOwns(provider);
  const result = await request('/token', oversizedUrlEncoded);
  await close();
  let parsed = {};
  try { parsed = JSON.parse(text(result)); } catch { parsed = {}; }
  return {
    ok: tech.ok && result.status === 400 && parsed.error === 'invalid_request'
      && parsed.error_description === 'failed to parse the request body',
    detail: { status: result.status, body: parsed, ...tech.detail },
  };
}

async function checkProxyProtocol() {
  const { provider, request, close } = await serve({}, { proxy: true });
  const tech = await fastifyOwns(provider);
  const result = await request(discoveryPath, {
    headers: { 'x-forwarded-proto': 'https' },
  });
  await close();
  let issuer;
  try { issuer = JSON.parse(text(result)).issuer; } catch { issuer = undefined; }
  return {
    ok: tech.ok && result.status === 200 && issuer === 'https://op.example',
    detail: { status: result.status, issuer, ...tech.detail },
  };
}

async function checkCallbackBootstrap() {
  const provider = await newProvider();
  const tech = await fastifyOwns(provider);
  const server = http.createServer(provider.callback());
  await new Promise((resolve) => server.listen(0, resolve));
  const { port } = server.address();
  const response = await fetch(`http://localhost:${port}${jwksPath}`);
  const ok200 = response.status === 200;
  await new Promise((resolve) => server.close(resolve));
  return {
    ok: tech.ok && ok200,
    detail: { status: response.status, ...tech.detail },
  };
}

async function checkCorsPreflight() {
  const { provider, request, close } = await serve();
  const tech = await fastifyOwns(provider);
  const result = await request('/token', preflight);
  await close();
  return {
    ok: tech.ok && result.status === 204
      && result.headers['access-control-allow-origin'] === origin
      && result.headers['access-control-allow-methods'] === 'POST'
      && result.headers['access-control-max-age'] === '3600'
      && result.headers['access-control-allow-headers'] === 'content-type',
    detail: { status: result.status, headers: result.headers, ...tech.detail },
  };
}

async function checkCorsProfileHeaders() {
  const { provider, request, close } = await serve();
  const tech = await fastifyOwns(provider);
  const preflightResult = await request('/token', preflight);
  const open = await request(jwksPath, { headers: { Origin: origin } });
  await close();
  return {
    ok: tech.ok && preflightResult.status === 204
      && preflightResult.headers['access-control-allow-origin'] === origin
      && preflightResult.headers['access-control-allow-methods'] === 'POST'
      && open.status === 200
      && open.headers['access-control-allow-origin'] === origin,
    detail: {
      preflightStatus: preflightResult.status,
      preflightHeaders: preflightResult.headers,
      jwksStatus: open.status,
      jwksHeaders: open.headers,
      ...tech.detail,
    },
  };
}

async function checkCaseInsensitiveRoutes() {
  const { provider, request, close } = await serve();
  const tech = await fastifyOwns(provider);
  const options = tokenForm('unknown');
  const plain = await request('/token', options);
  const upper = await request('/TOKEN', options);
  const slash = await request('/token/', options);
  await close();
  return {
    ok: tech.ok && plain.status === upper.status && plain.status === slash.status
      && text(plain) === text(upper) && text(plain) === text(slash),
    detail: {
      token: { status: plain.status, body: text(plain) },
      upper: { status: upper.status, body: text(upper) },
      trailingSlash: { status: slash.status, body: text(slash) },
      ...tech.detail,
    },
  };
}

async function checkProviderIdentity() {
  const provider = await newProvider();
  const tech = await fastifyOwns(provider);
  const listens = typeof provider.callback === 'function' && typeof provider.listen === 'function';
  return {
    ok: tech.ok && tech.detail.eventEmitter && listens,
    detail: { callback: typeof provider.callback, listen: typeof provider.listen, ...tech.detail },
  };
}

const CHECKS = {
  'F2P-069': checkRuntimeEntry,
  'F2P-072': checkCorsPreflight,
  'F2P-049': checkCallbackBootstrap,
  'F2P-050': checkBodySizeCap,
  'F2P-051': checkCorsProfileHeaders,
  'F2P-052': checkCallbackBootstrap,
  'F2P-053': checkCorsProfileHeaders,
  'F2P-054': checkCallbackBootstrap,
  'F2P-055': checkUnsupportedContentType,
  'F2P-056': checkProviderIdentity,
  'F2P-057': checkNotFound,
  'F2P-058': checkCorsProfileHeaders,
  'F2P-059': checkProxyProtocol,
  'F2P-060': checkProviderIdentity,
  'F2P-061': checkCallbackBootstrap,
  'F2P-062': checkCaseInsensitiveRoutes,
  'F2P-063': checkProviderIdentity,
  'F2P-064': checkCorsProfileHeaders,
  'F2P-065': checkCorsProfileHeaders,
  'F2P-066': checkNotFound,
  'F2P-067': checkCallbackBootstrap,
  'F2P-068': checkCorsProfileHeaders,
  'F2P-070': checkCorsProfileHeaders,
  'F2P-071': checkCorsProfileHeaders,
};

export function observationIds() {
  return Object.keys(observationMeta);
}

export async function runObservation(observationId, side) {
  const meta = observationMeta[observationId];
  const emit = (status, detail) => {
    const payload = {
      observation_id: observationId,
      side,
      requirement_ids: meta ? meta.requirement_ids : [],
      surface: meta ? meta.surface : undefined,
      status,
      detail,
    };
    process.stdout.write(`${JSON.stringify(payload)}\n`);
    process.exitCode = status === 'PASS' ? 0 : 1;
  };

  const check = CHECKS[observationId];
  if (!check) {
    emit('FAIL', { reason: 'no probe implemented for observation' });
    return;
  }

  try {
    const result = await check();
    emit(result.ok ? 'PASS' : 'FAIL', result.detail);
  } catch (error) {
    emit('INFRA_ERROR', { message: error && error.message, stack: error && error.stack });
  }
}
