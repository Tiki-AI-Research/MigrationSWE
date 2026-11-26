'use strict';

// Shared F2P predicate module for BATCH-F2P-0001.
//
// The Express -> Fastify migration of `server.js` is a *technology* migration:
// several observations reuse behaviour that the Express server already
// produced (200/OK, 404/500, 100 kB JSON limit, ...). A valid F2P must fail on
// the source technology and pass on the target technology, so every predicate
// below anchors the behavioural contract to the *target* technology actually
// owning the concern (a Fastify instance built from the fastify plugin stack),
// and never just to the observable response.
//
// The module is Node-core only. It is consumed by:
//   source/batches/BATCH-F2P-0001/source-probe.js   (source must FAIL all)
//   target/batches/BATCH-F2P-0001/target-probe.js   (target must PASS all)
//
// `evaluate(root)` is deterministic and side-effect free; `evaluateLive(base)`
// additionally cross-checks the same concerns over real HTTP when a server is
// running. Both return `{ id: 'PASS' | 'FAIL', ... }`.

const fs = require('fs');
const path = require('path');
const http = require('http');
const crypto = require('crypto');

const SERVER = 'server.js';
// The target-technology anchor is the Fastify framework itself.  The instruction
// requires the migrated responsibilities to be *owned by Fastify* and explicitly
// permits adding/removing plugins, so a specific plugin choice (`@fastify/static`
// / `@fastify/multipart`) must never be the pass/fail condition; the capability
// predicates below anchor to the observable mechanism instead.
const FASTIFY_STACK = ['fastify'];
const EXPRESS_MIDDLEWARE = [
  'express',
  'body-parser',
  'serve-static',
  'csv-express',
  'server-sent-events',
  'multer',
];

const OBSERVATIONS = [
  { id: 'F2P-001', requirement_ids: ['S013'], surface: 'server.js::GET /recording/stop' },
  { id: 'F2P-002', requirement_ids: ['R044'], surface: 'server.js::GET /start' },
  { id: 'F2P-003', requirement_ids: ['S012'], surface: 'server.js::GET /start' },
  { id: 'F2P-004', requirement_ids: ['R035'], surface: 'server.js::Startup version mismatch guard' },
  { id: 'F2P-005', requirement_ids: ['S004'], surface: 'server.js::GET /start' },
  { id: 'F2P-006', requirement_ids: ['S010'], surface: 'server.js::GET /recording/stop' },
  { id: 'F2P-007', requirement_ids: ['S001'], surface: 'server.js::GET /status' },
  { id: 'F2P-009', requirement_ids: ['S006'], surface: 'server.js::GET /webcam/resolution' },
  { id: 'F2P-010', requirement_ids: ['S007'], surface: 'server.js::GET /config' },
  { id: 'F2P-012', requirement_ids: ['S014'], surface: 'server.js::GET /recording/:id/counter' },
  { id: 'F2P-015', requirement_ids: ['S008'], surface: 'server.js::GET /webcam/stream' },
  { id: 'F2P-017', requirement_ids: ['S005'], surface: 'server.js::GET /webcam/stream' },
  { id: 'F2P-021', requirement_ids: ['R001'], surface: 'server.js::GET /recording/:id/counter' },
  { id: 'F2P-022', requirement_ids: ['R046'], surface: 'server.js::GET /' },
  { id: 'F2P-023', requirement_ids: ['R034'], surface: 'server.js::GET /start' },
  { id: 'F2P-011', requirement_ids: ['S001'], surface: 'server.js::Protected public specs under spec/' },
  { id: 'F2P-013', requirement_ids: ['S012'], surface: 'server.js::POST /ui' },
  { id: 'F2P-024', requirement_ids: ['R035'], surface: 'server.js::GET /recordings' },
  { id: 'F2P-008', requirement_ids: ['S010'], surface: 'server.js::GET /recording/stop' },
  { id: 'F2P-014', requirement_ids: ['R035'], surface: 'server.js::GET /a-page-that-does-not-exist' },
  { id: 'F2P-016', requirement_ids: ['S013'], surface: 'server.js::GET /config' },
  { id: 'F2P-018', requirement_ids: ['S013'], surface: 'server.js::GET /webcam/stream' },
  { id: 'F2P-019', requirement_ids: ['R044'], surface: 'server.js::GET /' },
  { id: 'F2P-020', requirement_ids: ['S005'], surface: 'server.js::GET /status' },
];

// Strip // and /* */ comments while preserving string/template contents so a
// token that only appears in prose is not treated as code.
function stripComments(text) {
  let out = '';
  let quote = null;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quote !== null) {
      out += ch;
      if (ch === '\\' && i + 1 < text.length) { out += text[i + 1]; i += 1; continue; }
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; out += ch; continue; }
    if (ch === '/' && text[i + 1] === '/') { i += 2; while (i < text.length && text[i] !== '\n' && text[i] !== '\r') i += 1; i -= 1; continue; }
    if (ch === '/' && text[i + 1] === '*') { i += 2; while (i + 1 < text.length && !(text[i] === '*' && text[i + 1] === '/')) i += 1; i += 1; continue; }
    out += ch;
  }
  return out;
}

// Fold a bounded set of constant forms ('a' + 'b', String('x'), static template)
// so semantically equal spellings are matched identically.
function foldConstants(text) {
  const str = "(?:'(?:\\\\.|[^'\\\\])*'|\"(?:\\\\.|[^\"\\\\])*\")";
  const concat = new RegExp('(' + str + ')\\s*\\+\\s*(' + str + ')', 'g');
  const wrapped = new RegExp('\\bString\\(\\s*(' + str + ')\\s*\\)', 'g');
  const tpl = /`([^`\\$]*)`/g;
  let previous = null;
  let value = text;
  while (previous !== value) {
    previous = value;
    value = value.replace(concat, (m, a, b) => "'" + a.slice(1, -1) + b.slice(1, -1) + "'");
    value = value.replace(wrapped, (m, a) => a);
    value = value.replace(tpl, (m, a) => "'" + a + "'");
  }
  return value;
}

function makeReader(root) {
  const raw = (rel) => {
    try { return fs.readFileSync(path.join(root, rel), 'utf8'); } catch (err) { return null; }
  };
  const norm = (rel) => {
    const text = raw(rel);
    return text === null ? null : foldConstants(stripComments(text));
  };
  return {
    read: raw,
    normalized: norm,
    has(rel, literal) {
      const text = norm(rel);
      return text !== null && text.includes(literal);
    },
    hasRe(rel, regex) {
      const text = norm(rel);
      return text !== null && regex.test(text);
    },
    countRe(rel, regex) {
      const text = norm(rel) || '';
      const re = new RegExp(regex.source, regex.flags.includes('g') ? regex.flags : regex.flags + 'g');
      let n = 0;
      while (re.exec(text) !== null) n += 1;
      return n;
    },
    requires(rel) {
      const text = norm(rel) || '';
      const out = [];
      const re = /require\(\s*['"]([^'"]+)['"]\s*\)/g;
      let m;
      while ((m = re.exec(text)) !== null) out.push(m[1]);
      return out;
    },
  };
}


function listFiles(root, base, acc) {
  for (const entry of fs.readdirSync(path.join(root, base), { withFileTypes: true })) {
    if (entry.name === '.git' || entry.name === 'node_modules') continue;
    const rel = base === '.' ? entry.name : `${base}/${entry.name}`;
    if (entry.isDirectory()) listFiles(root, rel, acc);
    else acc.push(rel);
  }
  return acc;
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

// The core technology predicate reused by every observation.
function fastifyOwned(r) {
  const reqs = r.requires(SERVER);
  const hasStack = FASTIFY_STACK.every((name) => reqs.includes(name));
  const noExpress = !EXPRESS_MIDDLEWARE.some((name) => reqs.includes(name));
  return hasStack && noExpress;
}

// same predicate that INTEGRATION must apply on both sides
function makePredicates(root) {
  const r = makeReader(root);
  const own = () => fastifyOwned(r);

  // Value/semantic anchors rather than reference-implementation identifiers:
  // a local binding may be renamed and an equivalent literal may be spelled
  // differently, but the module specifier, the route path and the value do
  // not change.  Canonical RUNTIME bodies are recognised in every equivalent
  // spelling: the literal, `String(...)`, `http.STATUS_CODES[n]`, and a
  // `sendStatus(...)` helper — all of which emit the same wire body.
  const OK_SEND = /\.send\(\s*(?:String\(\s*)?['"]OK['"]|\.send\(\s*(?:http\.)?STATUS_CODES\[\s*200\s*\]|sendStatus\w*\(\s*[^)]*,\s*200\s*\)/g;
  const NOT_FOUND_BODY = /\.send\(\s*(?:String\(\s*)?['"]Not Found['"]|(?:http\.)?STATUS_CODES\[\s*404\s*\]|sendStatus\w*\(\s*[^)]*,\s*404\s*\)/;
  const SERVER_ERROR_BODY = /\.send\(\s*(?:String\(\s*)?['"]Internal Server Error['"]|(?:http\.)?STATUS_CODES\[\s*500\s*\]|sendStatus\w*\(\s*[^)]*,\s*500\s*\)/;
  const TEXT_PLAIN_TYPE = /\.type\(\s*['"]text\/plain(?:\s*;\s*charset=[^'"]*)?['"]\s*\)/;
  const HOST_ALL = /host:\s*['"]0\.0\.0\.0['"]/;
  const BODY_LIMIT = /bodyLimit\s*:\s*(?:102400|100\s*\*\s*1024|1024\s*\*\s*100)/;
  const APIDOC_PREFIX = /prefix\s*:\s*['"]\/api\/doc\/?['"]/;
  // Capability anchors are behavioural: static serving may be implemented with a
  // plugin, a sendFile helper or a raw stream, and multipart handling with a
  // plugin, a content-type parser or a parser library.  The externally
  // observable identifier (`/api/doc`) and the capability mechanism are what the
  // contract requires, not a particular module specifier.
  const APIDOC_PATH = /['"]\/api\/doc\/?['"]/;
  const STATIC_SERVE = /(?:createReadStream|sendFile|serveStatic|@fastify\/static|\bstatic\b)/;
  const MULTIPART_HANDLING = /(?:multipart|\.file\s*\(|busboy|addContentTypeParser)/i;
  const MULTIPART_MODULE = /@fastify\/multipart/;
  const STATIC_MODULE = /@fastify\/static/;
  const CSV_CONTENT_TYPE = /text\/csv/i;
  const NO_CSV_EXPRESS = /require\(\s*['"]csv-express['"]\s*\)|from\s+['"]csv-express['"]/;
  const NO_BODY_PARSER = /require\(\s*['"]body-parser['"]\s*\)|from\s+['"]body-parser['"]/;
  const SEND_STATUS_200 = /res\.sendStatus\(\s*200\s*\)/;
  const route = (method, pathValue) => new RegExp(
    '\\.' + method + '\\(\\s*[\'"]' + pathValue.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&') + '[\'"]'
  );

  return {
    'F2P-001': () => own() && r.countRe(SERVER, OK_SEND) >= 5 && !r.hasRe(SERVER, SEND_STATUS_200),
    'F2P-002': () => own(),
    'F2P-003': () => own() && r.hasRe(SERVER, HOST_ALL),
    'F2P-004': () => own() && r.hasRe(SERVER, BODY_LIMIT) && r.hasRe(SERVER, TEXT_PLAIN_TYPE),
    'F2P-005': () => own() && (r.hasRe(SERVER, STATIC_MODULE) || r.hasRe(SERVER, STATIC_SERVE)) && r.hasRe(SERVER, APIDOC_PATH),
    'F2P-006': () => own(),
    'F2P-007': () => own() && r.hasRe(SERVER, BODY_LIMIT) && !r.hasRe(SERVER, NO_BODY_PARSER),
    'F2P-008': () => own(),
    'F2P-009': () => own() && r.hasRe(SERVER, CSV_CONTENT_TYPE) && !r.hasRe(SERVER, NO_CSV_EXPRESS),
    'F2P-010': () => own() && r.hasRe(SERVER, /text\/event-stream/) && r.hasRe(SERVER, /retry:\s*2000/),
    'F2P-011': () => own() && r.hasRe(SERVER, BODY_LIMIT) && !r.hasRe(SERVER, NO_BODY_PARSER),
    'F2P-012': () => own() && r.hasRe(SERVER, NOT_FOUND_BODY) && r.hasRe(SERVER, SERVER_ERROR_BODY),
    'F2P-013': () => own() && r.hasRe(SERVER, HOST_ALL),
    'F2P-014': () => own() && r.hasRe(SERVER, BODY_LIMIT) && !r.hasRe(SERVER, NO_BODY_PARSER),
    'F2P-015': () => own() && r.hasRe(SERVER, /reply\.hijack\(\)|\.hijack\(\)/) && r.hasRe(SERVER, /\.raw\b/),
    'F2P-016': () => own() && r.countRe(SERVER, OK_SEND) >= 5,
    'F2P-017': () => own() && r.hasRe(SERVER, MULTIPART_HANDLING),
    'F2P-018': () => own() && r.countRe(SERVER, OK_SEND) >= 5,
    'F2P-019': () => own(),
    'F2P-020': () => own() && r.hasRe(SERVER, MULTIPART_HANDLING),
    'F2P-021': () => own() && r.hasRe(SERVER, route('(?:get|post)', '/*')) && r.hasRe(SERVER, route('post', '/ui')),
    'F2P-022': () => {
      if (!own()) return false;
      const sourceFiles = listFiles('/input/source', '.', []).sort();
      const targetFiles = listFiles('/input/target', '.', []).sort();
      const changed = targetFiles.filter((f) => sourceFiles.includes(f)
        && sha256(path.join('/input/source', f)) !== sha256(path.join('/input/target', f)));
      return changed.every((f) => f === 'server.js' || f === 'package.json' || f === 'package-lock.json');
    },
    'F2P-023': () => {
      if (!own()) return false;
      const text = r.normalized(SERVER) || '';
      const wildcard = text.search(/\.(?:get|post)\(\s*['"]\/\*['"]/);
      const files = text.search(/\.get\(\s*['"]\/files['"]/);
      return wildcard !== -1 && files !== -1 && files < wildcard;
    },
    'F2P-024': () => own() && r.hasRe(SERVER, BODY_LIMIT) && !r.hasRe(SERVER, NO_BODY_PARSER),
  };
}

function evaluate(root) {
  const runtime = makeReader(root);
  const predicates = makePredicates(root);
  const result = {};
  for (const obs of OBSERVATIONS) {
    const status = predicates[obs.id]() ? 'PASS' : 'FAIL';
    result[obs.id] = {
      id: obs.id,
      requirement_ids: obs.requirement_ids,
      surface: obs.surface,
      status,
      technology: FASTIFY_STACK.join(','),
      source_technology: EXPRESS_MIDDLEWARE.join(','),
      root,
      note: status === 'PASS'
        ? 'target technology owns the concern in the loaded/executed path'
        : 'target technology postcondition not satisfied (source technology path or missing capability)',
    };
  }
  return result;
}

// Optional live HTTP cross-check of the observable half of the contract.
function request(base, method, urlPath, headers, body) {
  const url = new URL(urlPath, base);
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: url.hostname,
      port: url.port,
      path: url.pathname + url.search,
      method,
      headers: Object.assign({}, headers || {}),
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({
        statusCode: res.statusCode,
        headers: res.headers,
        body: Buffer.concat(chunks).toString(),
      }));
    });
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

async function evaluateLive(base) {
  const out = {};
  const checks = {
    'F2P-001': async () => {
      const res = await request(base, 'GET', '/recording/stop');
      return res.statusCode === 200 && res.body === 'OK'
        && String(res.headers['content-type'] || '').startsWith('text/plain');
    },
    'F2P-003': async () => {
      const res = await request(base, 'GET', '/status');
      return res.statusCode === 200;
    },
    'F2P-007': async () => {
      const small = Buffer.from(JSON.stringify({ pad: 'x'.repeat(90 * 1024) }));
      const big = Buffer.from(JSON.stringify({ pad: 'x'.repeat(110 * 1024) }));
      const ok = await request(base, 'POST', '/ui', { 'Content-Type': 'application/json', 'Content-Length': small.length }, small);
      const rejected = await request(base, 'POST', '/ui', { 'Content-Type': 'application/json', 'Content-Length': big.length }, big);
      return ok.statusCode === 200 && rejected.statusCode >= 400;
    },
    'F2P-012': async () => {
      const missing = await request(base, 'GET', '/recording/does-not-exist/counter');
      return missing.statusCode === 404 && missing.body === 'Not Found';
    },
    'F2P-023': async () => {
      const files = await request(base, 'GET', '/files');
      return files.statusCode === 200 && String(files.headers['content-type'] || '').includes('json');
    },
  };
  for (const obs of OBSERVATIONS) {
    if (!checks[obs.id]) continue;
    try {
      out[obs.id] = (await checks[obs.id]()) ? 'PASS' : 'FAIL';
    } catch (err) {
      out[obs.id] = `INFRA_ERROR:${err.message}`;
    }
  }
  return out;
}

module.exports = {
  OBSERVATIONS,
  FASTIFY_STACK,
  EXPRESS_MIDDLEWARE,
  evaluate,
  evaluateLive,
  makePredicates,
  fastifyOwned,
  makeReader,
};
