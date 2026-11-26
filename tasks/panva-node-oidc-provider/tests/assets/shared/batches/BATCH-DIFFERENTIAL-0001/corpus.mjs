import { createServer } from 'node:http';
import { request as httpRequest } from 'node:http';
import { generateKeyPair, exportJWK } from 'jose';

export const OBSERVATION_IDS = [
  'DIFF-001', 'DIFF-002', 'DIFF-003', 'DIFF-004', 'DIFF-005', 'DIFF-006',
  'DIFF-007', 'DIFF-008', 'DIFF-009', 'DIFF-010', 'DIFF-011', 'DIFF-012',
  'DIFF-013', 'DIFF-014', 'DIFF-015', 'DIFF-016', 'DIFF-017', 'DIFF-018',
  'DIFF-019', 'DIFF-020', 'DIFF-021', 'DIFF-022', 'DIFF-023', 'DIFF-024',
  'DIFF-025', 'DIFF-026', 'DIFF-027', 'DIFF-028', 'DIFF-029',
];

export const CLIENT = {
  client_id: 'client',
  client_secret: 'secret',
  redirect_uris: ['https://client.example.com/cb'],
  grant_types: ['authorization_code', 'refresh_token'],
  response_types: ['code'],
  token_endpoint_auth_method: 'client_secret_basic',
};

const BASIC = `Basic ${Buffer.from('client:secret').toString('base64')}`;
const FORM = 'application/x-www-form-urlencoded';

async function mainJwks() {
  const { privateKey } = await generateKeyPair('RS256', { extractable: true });
  const jwk = await exportJWK(privateKey);
  jwk.kid = 'main';
  jwk.alg = 'RS256';
  jwk.use = 'sig';
  return { keys: [jwk] };
}

export function makeProvider(Provider, MemoryAdapter, jwks, extra = {}) {
  return new Provider('http://127.0.0.1', {
    jwks,
    adapter: MemoryAdapter,
    findAccount: (_ctx, id) => ({ accountId: id, claims: () => ({ sub: id }) }),
    clients: [CLIENT],
    features: { devInteractions: { enabled: false } },
    ...extra,
  });
}

export function createHttp() {
  return function send(port, { method = 'GET', path = '/', headers = {}, body } = {}) {
    return new Promise((resolve, reject) => {
      const req = httpRequest({ host: '127.0.0.1', port, method, path, headers }, (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => resolve({
          status: res.statusCode,
          headers: res.headers,
          setCookie: res.headers['set-cookie'] ?? [],
          body: Buffer.concat(chunks).toString('utf8'),
        }));
      });
      req.on('error', reject);
      if (body !== undefined) req.write(body);
      req.end();
    });
  };
}

export function listen(server) {
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
}

export function close(server) {
  return new Promise((resolve) => server.close(resolve));
}

export function createServerFor(provider) {
  return createServer(provider.callback());
}

const OVERSIZE = `grant_type=authorization_code&code=${'a'.repeat(60 * 1024)}`;

export const CASES = [
  { name: 'discovery', provider: 'default', req: { path: '/.well-known/openid-configuration' } },
  { name: 'discovery_oauth', provider: 'default', req: { path: '/.well-known/oauth-authorization-server' } },
  { name: 'jwks', provider: 'default', req: { path: '/jwks' } },
  { name: 'jwks_head', provider: 'default', req: { method: 'HEAD', path: '/jwks' } },
  { name: 'not_found', provider: 'default', req: { path: '/definitely-not-a-route' } },
  { name: 'me_unauth', provider: 'default', req: { path: '/me' } },
  { name: 'token_empty', provider: 'default', req: { method: 'POST', path: '/token', headers: { 'content-type': FORM } } },
  { name: 'token_no_dup', provider: 'default', req: { method: 'POST', path: '/token', headers: { 'content-type': FORM, authorization: BASIC }, body: 'grant_type=bogus' } },
  { name: 'token_invalid_client', provider: 'default', req: { method: 'POST', path: '/token', headers: { 'content-type': FORM, authorization: BASIC }, body: 'grant_type=authorization_code&client_id=unknown' } },
  { name: 'token_xml', provider: 'default', req: { method: 'POST', path: '/token', headers: { 'content-type': 'application/xml' }, body: '<x/>' } },
  { name: 'token_json', provider: 'default', req: { method: 'POST', path: '/token', headers: { 'content-type': 'application/json' }, body: '{}' } },
  { name: 'token_badescape', provider: 'default', req: { method: 'POST', path: '/token', headers: { 'content-type': FORM, authorization: BASIC }, body: 'grant_type=authorization_code&code=%ZZ' } },
  { name: 'token_oversize', provider: 'default', req: { method: 'POST', path: '/token', headers: { 'content-type': FORM, authorization: BASIC }, body: OVERSIZE } },
  { name: 'token_upper', provider: 'default', req: { method: 'POST', path: '/TOKEN', headers: { 'content-type': FORM, authorization: BASIC }, body: '' } },
  { name: 'token_slash', provider: 'default', req: { method: 'POST', path: '/token/', headers: { 'content-type': FORM, authorization: BASIC }, body: '' } },
  { name: 'options_token', provider: 'default', req: { method: 'OPTIONS', path: '/token', headers: { origin: 'https://rp.example', 'access-control-request-method': 'POST' } } },
  { name: 'options_me', provider: 'default', req: { method: 'OPTIONS', path: '/me', headers: { origin: 'https://rp.example', 'access-control-request-method': 'GET' } } },
  { name: 'me_cors', provider: 'default', req: { path: '/me', headers: { origin: 'https://rp.example' } } },
  { name: 'auth_noparams', provider: 'default', req: { path: '/auth' } },
  { name: 'auth_interaction_cookie', provider: 'default', req: { path: '/auth?client_id=client&redirect_uri=https%3A%2F%2Fclient.example.com%2Fcb&response_type=code&scope=openid&state=xyz' } },
  { name: 'head_auth', provider: 'default', req: { method: 'HEAD', path: '/auth' } },
  { name: 'registration_wrong_ct', provider: 'default', req: { method: 'POST', path: '/reg', headers: { 'content-type': 'text/plain' }, body: 'x' } },
  { name: 'session_end', provider: 'default', req: { path: '/session/end' } },
  { name: 'device_authorization_get', provider: 'default', req: { path: '/device/auth' } },
  { name: 'proxy_discovery', provider: 'proxy', req: { path: '/.well-known/openid-configuration', headers: { 'x-forwarded-proto': 'https' } } },
  { name: 'proxy_me', provider: 'proxy', req: { path: '/me', headers: { 'x-forwarded-proto': 'https' } } },
];

export function normalize(res, { port }) {
  const substitute = (value) => {
    if (typeof value !== 'string') return value;
    return value
      .split(`:${port}`).join(':{{PORT}}')
      .replace(/127\.0\.0\.1:\d+/g, '127.0.0.1:{{PORT}}');
  };
  let body = substitute(res.body);
  body = body.replace(/"n":"[^"]*"/g, '"n":"{{N}}"').replace(/"e":"[^"]*"/g, '"e":"{{E}}"');
  body = body.replace(/&state=[^&"]*/g, '&state={{STATE}}');
  body = body.replace(/\/interaction\/[A-Za-z0-9_-]{16,}/g, '/interaction/{{UID}}');
  body = body.replace(/value="[0-9a-f]{32,}"/g, 'value="{{XSRF}}"');
  const cookieNames = [...new Set((res.setCookie ?? []).map((c) => c.split('=')[0]))];
  return {
    status: res.status,
    'content-type': substitute(res.headers['content-type'] ?? null),
    'www-authenticate': substitute(res.headers['www-authenticate'] ?? null),
    'access-control-allow-origin': substitute(res.headers['access-control-allow-origin'] ?? null),
    'access-control-allow-methods': substitute(res.headers['access-control-allow-methods'] ?? null),
    'access-control-expose-headers': substitute(res.headers['access-control-expose-headers'] ?? null),
    'access-control-max-age': substitute(res.headers['access-control-max-age'] ?? null),
    'set-cookie-names': cookieNames,
    body: body.slice(0, 600),
  };
}
