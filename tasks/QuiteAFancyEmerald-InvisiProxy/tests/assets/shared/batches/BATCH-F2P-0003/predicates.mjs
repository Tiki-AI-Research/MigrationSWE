// Shared F2P predicates for BATCH-F2P-0003.
//
// These predicates encode the target postconditions bound to the batch's
// observations. They are intentionally side-agnostic: the source probe and the
// target probe run the identical predicate set against their own repository and
// their own running server. The source side is expected to fail the predicates
// (it serves through Fastify); the target side is expected to satisfy them
// (it serves through Express 5).
//
// Environment:
//   PROBE_REPO  repository root under inspection (default /workspace/repo)
//   PROBE_BASE  running server origin (default http://127.0.0.1:8080)
//   PROBE_HOST  connect host used for the upgrade probe (default 127.0.0.1)
//   PROBE_PORT  connect port used for the upgrade probe (default 8080)

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import net from 'node:net';

export const REPO = process.env.PROBE_REPO || '/workspace/repo';
export const BASE = process.env.PROBE_BASE || 'http://127.0.0.1:8080';
export const HOST = process.env.PROBE_HOST || '127.0.0.1';
export const PORT = Number(process.env.PROBE_PORT || 8080);

const WISP_CANDIDATES = ['/cron/', '/wisp/'];
const GITHUB_CANDIDATES = ['/github/router', '/github/wisp'];

const readRepo = (relative) => readFileSync(join(REPO, relative), 'utf8');

const readJson = (relative) => JSON.parse(readRepo(relative));

const readServerSource = () => {
  for (const candidate of ['src/server.mjs', 'backend.js']) {
    try {
      return readRepo(candidate);
    } catch (_) {
      /* try the next candidate */
    }
  }
  throw new Error('unable to locate the migrated server module');
};

async function httpGet(pathname, headers = {}) {
  const res = await fetch(BASE + pathname, { redirect: 'manual', headers });
  const body = Buffer.from(await res.arrayBuffer());
  return { status: res.status, headers: res.headers, body };
}

function upgradeStatus(pathname) {
  return new Promise((resolve) => {
    const socket = net.connect(PORT, HOST, () => {
      socket.write(
        [
          `GET ${pathname} HTTP/1.1`,
          `Host: ${HOST}:${PORT}`,
          'Upgrade: websocket',
          'Connection: Upgrade',
          'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
          'Sec-WebSocket-Version: 13',
          'Sec-WebSocket-Protocol: wisp',
          '',
          '',
        ].join('\r\n')
      );
    });
    let data = '';
    const timer = setTimeout(() => {
      socket.destroy();
      resolve(data);
    }, 2500);
    socket.on('data', (chunk) => (data += chunk.toString('latin1')));
    socket.on('error', () => {
      clearTimeout(timer);
      resolve(data);
    });
    socket.on('close', () => {
      clearTimeout(timer);
      resolve(data);
    });
  });
}

const isSwitchingProtocols = (raw) => /HTTP\/1\.1 101/.test(raw);

const headerText = (headers) =>
  [...headers.entries()].map(([k, v]) => `${k}: ${v}`).join('\n').toLowerCase();

async function checkDependencyPolicy() {
  const pkg = readJson('package.json');
  const deps = { ...(pkg.dependencies || {}), ...(pkg.devDependencies || {}) };
  const hasExpress = 'express' in deps;
  const hasHelmet = 'helmet' in deps;
  const hasFastify =
    'fastify' in deps || '@fastify/static' in deps || '@fastify/helmet' in deps;
  if (!hasExpress || !hasHelmet || hasFastify)
    return {
      ok: false,
      detail: `express=${hasExpress} helmet=${hasHelmet} fastify-family=${hasFastify}`,
    };
  const root = await httpGet('/');
  return {
    ok: root.status === 200,
    detail: `express/helmet present, fastify absent, GET / -> ${root.status}`,
  };
}

function checkExpressInProcess() {
  const src = readServerSource();
  const importsFastify =
    /(?:^|\n)\s*import[^\n]*from\s*['"]fastify['"]/.test(src) ||
    /require\(\s*['"]fastify['"]\s*\)/.test(src) ||
    /import\(\s*['"]fastify['"]\s*\)/.test(src);
  const importsFastifyPlugins =
    /['"]@fastify\/(?:static|helmet)['"]/.test(src);
  const usesExpress =
    /(?:^|\n)\s*import\s+express\s+from\s*['"]express['"]/.test(src) ||
    /require\(\s*['"]express['"]\s*\)/.test(src);
  const delegatesToFastify =
    /child_process|fork\(|spawn\(|execFile\(|\.register\(/.test(src) &&
    /fastify/i.test(src);
  const ok =
    usesExpress && !importsFastify && !importsFastifyPlugins && !delegatesToFastify;
  return {
    ok,
    detail: `express=${usesExpress} fastify-import=${importsFastify} fastify-plugins=${importsFastifyPlugins} delegation=${delegatesToFastify}`,
  };
}

function checkNoXPoweredBy() {
  return httpGet('/').then((res) => ({
    ok: !res.headers.has('x-powered-by'),
    detail: `x-powered-by=${res.headers.get('x-powered-by')}`,
  }));
}

function checkHelmetHeaders() {
  return httpGet('/').then((res) => {
    const h = headerText(res.headers);
    const nosniff = res.headers.get('x-content-type-options') === 'nosniff';
    const noCsp = !res.headers.has('content-security-policy');
    const noPowered = !res.headers.has('x-powered-by');
    return {
      ok: nosniff && noCsp && noPowered,
      detail: `nosniff=${nosniff} no-csp=${noCsp} no-powered-by=${noPowered} (${h.slice(0, 120)})`,
    };
  });
}

function checkHistoryHide() {
  const headers = { 'sec-fetch-dest': 'document', cookie: 'HistoryHide=true' };
  return Promise.all([
    httpGet('/', headers),
    httpGet('/documentation', headers),
    httpGet('/assets/js/card.js', headers),
  ]).then(([root, page, asset]) => {
    const ok = [root, page, asset].every((r) => r.status === 404);
    return {
      ok,
      detail: `root=${root.status} page=${page.status} asset=${asset.status}`,
    };
  });
}

async function checkSlashNormalization() {
  const canonical = await httpGet('/documentation');
  for (const mutant of ['///documentation//', '/documentation/']) {
    const res = await httpGet(mutant);
    const same =
      res.status === canonical.status && res.body.equals(canonical.body);
    if (!same)
      return {
        ok: false,
        detail: `${mutant} -> ${res.status} (canonical ${canonical.status})`,
      };
  }
  return {
    ok: canonical.status === 200,
    detail: `duplicate/trailing slash paths match GET /documentation (${canonical.status})`,
  };
}

async function checkStaticPrecedence() {
  const asset = await httpGet('/assets/js/card.js');
  const ct = (asset.headers.get('content-type') || '').toLowerCase();
  const ok = asset.status === 200 && asset.body.length > 0 && /javascript/.test(ct);
  return {
    ok,
    detail: `/assets/js/card.js -> ${asset.status} bytes=${asset.body.length} type=${ct}`,
  };
}

async function checkGithubRedirect() {
  for (const candidate of GITHUB_CANDIDATES) {
    const res = await httpGet(candidate);
    if (res.status === 302 && res.headers.has('location'))
      return { ok: true, detail: `${candidate} -> 302 ${res.headers.get('location')}` };
  }
  const miss = await httpGet('/github/does-not-exist');
  return {
    ok: miss.status === 404,
    detail: `no redirect for ${GITHUB_CANDIDATES.join(', ')}; miss -> ${miss.status}`,
  };
}

async function checkSharedServer() {
  const httpOk = (await httpGet('/')).status === 200;
  for (const candidate of WISP_CANDIDATES) {
    const raw = await upgradeStatus(candidate);
    if (isSwitchingProtocols(raw))
      return {
        ok: httpOk,
        detail: `GET / served and ${candidate} upgraded (101) on port ${PORT}`,
      };
  }
  const extra = (await httpGet('/wisp/')).status;
  return {
    ok: false,
    detail: `http=${httpOk} no upgrade on ${WISP_CANDIDATES.join(', ')} (probe /wisp/ -> ${extra})`,
  };
}

async function checkContentTypeFallback() {
  const src = readServerSource();
  let usingSeo = false;
  try {
    usingSeo = readJson('config.json').usingSEO === true;
  } catch (_) {
    usingSeo = false;
  }
  if (usingSeo) {
    const xml = await httpGet('/sitemap.xml');
    const txt = await httpGet('/robots.txt');
    const ok =
      /xml/.test(xml.headers.get('content-type') || '') &&
      /text\/plain/.test(txt.headers.get('content-type') || '');
    return {
      ok,
      detail: `sitemap.xml=${xml.headers.get('content-type')} robots.txt=${txt.headers.get('content-type')}`,
    };
  }
  const mapsXml = /xml:\s*'application\/xml'/.test(src);
  const mapsTxt = /txt:\s*'text\/plain'/.test(src);
  const hasFallback = /default:\s*config\.disguiseFiles|supportedTypes\.default/.test(src);
  return {
    ok: mapsXml && mapsTxt && hasFallback,
    detail: `(usingSEO=false static mapping) xml=${mapsXml} txt=${mapsTxt} fallback=${hasFallback}`,
  };
}

function checkEsmInterop() {
  const src = readServerSource();
  const expressDefault =
    /(?:^|\n)\s*import\s+express\s+from\s*['"]express['"]/.test(src) ||
    /require\(\s*['"]express['"]\s*\)/.test(src);
  const helmetDefault =
    /(?:^|\n)\s*import\s+helmet\s+from\s*['"]helmet['"]/.test(src) ||
    /require\(\s*['"]helmet['"]\s*\)/.test(src);
  const invokesStatic = /express\.static\(/.test(src);
  return httpGet('/').then((res) => ({
    ok: expressDefault && helmetDefault && invokesStatic && res.status === 200,
    detail: `express-default=${expressDefault} helmet-default=${helmetDefault} express.static=${invokesStatic} boot=${res.status}`,
  }));
}

// Each entry maps every observation in BATCH-F2P-0003 onto the target
// postcondition it is bound to.
export const OBSERVATION_CHECKS = {
  'F2P-069': checkDependencyPolicy,
  'F2P-072': checkExpressInProcess,
  'F2P-053': checkSharedServer,
  'F2P-068': checkHelmetHeaders,
  'F2P-070': checkHistoryHide,
  'F2P-071': checkSharedServer,
  'F2P-049': checkSharedServer,
  'F2P-050': checkHistoryHide,
  'F2P-051': checkHistoryHide,
  'F2P-052': checkSlashNormalization,
  'F2P-054': checkGithubRedirect,
  'F2P-055': checkHelmetHeaders,
  'F2P-056': checkStaticPrecedence,
  'F2P-057': checkContentTypeFallback,
  'F2P-058': checkNoXPoweredBy,
  'F2P-059': checkHistoryHide,
  'F2P-060': checkStaticPrecedence,
  'F2P-061': checkNoXPoweredBy,
  'F2P-062': checkContentTypeFallback,
  'F2P-063': checkHistoryHide,
  'F2P-064': checkHistoryHide,
  'F2P-065': checkStaticPrecedence,
  'F2P-066': checkGithubRedirect,
  'F2P-067': checkEsmInterop,
};

export const OBSERVATION_ORDER = Object.keys(OBSERVATION_CHECKS);

export async function runAll() {
  const results = [];
  for (const observationId of OBSERVATION_ORDER) {
    try {
      const outcome = await OBSERVATION_CHECKS[observationId]();
      results.push({ observation_id: observationId, ...outcome });
    } catch (error) {
      results.push({
        observation_id: observationId,
        ok: false,
        detail: `INFRA_ERROR: ${error && error.message ? error.message : error}`,
      });
    }
  }
  return results;
}
