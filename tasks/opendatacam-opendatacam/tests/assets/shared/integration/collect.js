'use strict';

// Batch collectors for the INTEGRATION runner.
//
// Each collector executes the shared projection contributed by the owning
// dependency batch and reduces its real output to report entries. A status is
// PASS only when an executable projection actually produced a value, and
// INCONCLUSIVE/TIMEOUT when it could not. On top of that, the integration layer
// applies two *documented* normalizations that are symmetric on both sides:
//
//   * `next.render` / the static apidoc mount: Express' `serve-static` mounted
//     at `/api/doc` answers a bare `/api/doc` with a 301 to `/api/doc/`, while
//     Fastify's `@fastify/static` answers it directly. Following the redirect
//     is the client-observable projection, so the redirected status is used.
//   * CSV field quoting is not semantic; quote delimiters are dropped before
//     comparison.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { STATUS_CODES } = require('http');
const { execFileSync } = require('child_process');
const {
  assetPath,
  batchPath,
  resolveNodeModules,
  withTimeout,
  TIMEOUT,
  request,
  multipartBody,
} = require('./lib');
const { bootServer } = require('./boot');

const ALLOWED_CHANGED = [
  'server.js', 'package.json', 'package-lock.json', 'npm-shrinkwrap.json',
  'yarn.lock', 'pnpm-lock.yaml', '.npmrc', '.yarnrc', '.yarnrc.yml',
];
// Build outputs / caches a candidate legitimately (re)generates are not authored
// source changes and must not count as scope violations.
const GENERATED_DIRS = new Set(['node_modules', '.git', '.build', '.next']);
const CHILD_STUB = path.join(__dirname, 'child-stub.js');
const semanticCache = new Map();

function semanticContracts(root) {
  if (!semanticCache.has(root)) {
    let output = '';
    try {
      output = execFileSync(process.execPath, [path.join(__dirname, 'semantic-probe.js'), root],
        { timeout: 90000, maxBuffer: 4 * 1024 * 1024, encoding: 'utf8' });
    } catch (error) {
      output = String(error.stdout || '');
    }
    const match = /(?:^|\n)SEMANTIC_RESULT=([^\n]+)/.exec(output);
    semanticCache.set(root, match ? JSON.parse(match[1]) : { error: 'semantic probe did not finish' });
  }
  return semanticCache.get(root);
}

function semanticResult(root, names) {
  const results = semanticContracts(root);
  const keys = ['technology'].concat(names);
  const missing = keys.filter((key) => !results[key]);
  return { status: missing.length ? 'INCONCLUSIVE'
    : keys.every((key) => results[key].satisfied) ? 'PASS' : 'FAIL',
  value: { contracts: keys.map((key) => ({ name: key, result: results[key] || null })),
    error: results.error || null } };
}

function putSemantic(root, put, id, names) {
  const result = semanticResult(root, names);
  put(id, result.status, result.value);
}

const REDIRECT_STATUS = [301, 302, 303, 307, 308];

// Deterministic counter history fed to `getCounterHistoryOfRecording` for the
// CSV surfaces. Both implementations run the identical route code over the
// identical data, so the only remaining difference is CSV quoting (normalized
// below).
function counterFixture() {
  return {
    dateStart: new Date('2020-01-01T00:00:00.000Z'),
    areas: { 'area-1': { name: 'area-1' } },
    counterHistory: [[{
      timestamp: new Date('2020-01-01T00:00:01.000Z'),
      area: 'area-1',
      name: 'car',
      id: 1,
      bearing: 0,
      countingDirection: 'rightleft_bottomtop',
    }]],
  };
}

function counterDataFor(id) {
  if (id === 'P2P-006' || id === 'P2P-038') return counterFixture();
  return {};
}

// Reduce volatile values (absolute paths, fractional numbers) to stable shape
// descriptors so a source projection and a target projection compare equal
// after normalization.
function scrub(value) {
  if (Array.isArray(value)) return value.map(scrub);
  if (value && typeof value === 'object') {
    const out = {};
    Object.keys(value).sort().forEach((key) => { out[key] = scrub(value[key]); });
    return out;
  }
  if (typeof value === 'string') {
    if (value.length > 1 && /^(\/|[A-Za-z]:\\|\\\\)/.test(value)) return '<abs-path>';
    return value;
  }
  if (typeof value === 'number' && !Number.isInteger(value)) return '<number>';
  return value;
}

function stripQuotes(text) {
  return typeof text === 'string' ? text.replace(/"/g, '') : text;
}

// Volatile substrings that a booted server writes to captured stdout (ports,
// temporary work directories, timestamps) are reduced to stable placeholders so
// the `/console` stream projects the same value on both sides.
function stableText(text) {
  return text
    .replace(/https?:\/\/[0-9A-Za-z_.-]+:\d+/g, 'http://HOST:PORT')
    .replace(/\/tmp\/integration-boot-[^/\s]+/g, '<tmp>')
    .replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?/g, '<date>');
}

const VOLATILE_HEADERS = ['last-modified', 'date', 'etag', 'expires'];

function stableHeaders(headers) {
  const out = {};
  Object.keys(headers || {}).forEach((key) => {
    if (VOLATILE_HEADERS.indexOf(String(key).toLowerCase()) === -1) out[key] = headers[key];
  });
  return out;
}

function hashFile(file) {
  try {
    return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  } catch (err) {
    return null;
  }
}

function listFiles(root) {
  const acc = [];
  const walk = (rel) => {
    const base = rel ? path.join(root, rel) : root;
    let entries;
    try {
      entries = fs.readdirSync(base, { withFileTypes: true });
    } catch (err) {
      return;
    }
    entries.forEach((entry) => {
      if (!rel && entry.isDirectory() && GENERATED_DIRS.has(entry.name)) return;
      const next = rel ? rel + '/' + entry.name : entry.name;
      if (entry.isDirectory()) walk(next);
      else acc.push(next);
    });
  };
  walk('');
  return acc;
}

// Instruction constraint 4 applies to authored additions, edits and deletions.
// Without the baseline no scope comparison can produce a passing observation.
function scopeLimited(root, baseline) {
  if (!baseline) {
    return { satisfied: null, evidence: ['no baseline snapshot provided; change scope not verifiable'] };
  }
  // The entry captures this before npm test/build can mutate the candidate.
  // Invalid or mismatched snapshots must never silently fall back to a pass.
  if (process.env.MIGRATION_AUTHORED_SCOPE !== undefined) {
    try {
      const snapshot = JSON.parse(process.env.MIGRATION_AUTHORED_SCOPE);
      if (snapshot.root !== path.resolve(root) || snapshot.baseline !== path.resolve(baseline)
        || !Array.isArray(snapshot.changed)
        || !snapshot.changed.every((file) => typeof file === 'string')) {
        throw new Error('scope snapshot does not match the evaluated trees');
      }
      const unexpected = snapshot.changed.filter((file) => !ALLOWED_CHANGED.includes(file));
      return { satisfied: unexpected.length === 0,
        evidence: ['unexpected=' + JSON.stringify(unexpected), 'snapshot=before-declared-commands'] };
    } catch (error) {
      return { satisfied: null, evidence: ['invalid authored scope snapshot: ' + error.message] };
    }
  }
  const baselineFiles = listFiles(baseline);
  const targetFiles = listFiles(root);
  const baselineSet = new Set(baselineFiles);
  const targetSet = new Set(targetFiles);
  const changed = targetFiles.filter(
    (f) => baselineSet.has(f) && hashFile(path.join(baseline, f)) !== hashFile(path.join(root, f)),
  );
  const added = targetFiles.filter((f) => !baselineSet.has(f));
  const removed = baselineFiles.filter((f) => !targetSet.has(f));
  const unexpected = changed.concat(added, removed)
    .filter((file) => ALLOWED_CHANGED.indexOf(file) === -1);
  return {
    satisfied: unexpected.length === 0,
    evidence: ['unexpected=' + JSON.stringify(unexpected)],
  };
}

// GET a path following up to three redirects. Returns the final response, or
// the last redirect response when it carries no usable Location.
async function requestFollowingRedirects(port, method, startPath, timeoutMs) {
  let res = await request({ port, method, path: startPath, timeoutMs: timeoutMs || 5000 });
  let hops = 0;
  while (res && REDIRECT_STATUS.indexOf(res.statusCode) !== -1
    && res.headers && res.headers.location && hops < 3) {
    const location = res.headers.location;
    const target = location.replace(/^https?:\/\/[^/]+/, '') || '/';
    res = await request({ port, method: 'GET', path: target, timeoutMs: timeoutMs || 5000 });
    hops += 1;
  }
  return res;
}

function collectP2P(side, root, put) {
  const checks = require(batchPath('BATCH-P2P-0001', 'p2p-checks.js'));
  const fixtures = require(assetPath('fixtures', 'batches', 'BATCH-P2P-0001', 'requests.json'));
  const ids = Object.keys(checks.PROJECTIONS);
  return bootServer(root).then((boot) => {
    const ctx = { side: side, port: boot.port, fixtures: fixtures };
    return ids.reduce(
      (chain, id) => chain.then(() => {
        // Each observation sees the counter state its surface requires: the CSV
        // surfaces get a populated history, every other surface an empty one.
        boot.setCounterData(counterDataFor(id));
        return withTimeout(
          Promise.resolve().then(() => checks.PROJECTIONS[id](ctx)),
          12000,
        ).then((value) => {
          if (value === TIMEOUT) {
            put(id, 'TIMEOUT', { timedOut: true });
            return undefined;
          }
          // The static apidoc mount redirects on the source side; follow it so
          // both sides project the client-observable final status.
          if ((id === 'P2P-019' || id === 'P2P-036')
            && value && REDIRECT_STATUS.indexOf(value.status) !== -1) {
            return requestFollowingRedirects(boot.port, 'GET', '/api/doc').then((final) => {
              const status = final ? final.statusCode : null;
              if (id === 'P2P-019') put(id, 'PASS', { status: status, served: status === 200 });
              else put(id, 'PASS', { status: status });
            });
          }
          if (value && typeof value === 'object') {
            if (typeof value.header === 'string') value.header = stripQuotes(value.header);
            if (Array.isArray(value.columns)) value.columns = value.columns.map(stripQuotes);
          }
          put(id, 'PASS', scrub(value));
          return undefined;
        }, (err) => {
          put(id, 'INCONCLUSIVE', { error: String((err && err.message) || err) });
        });
      }),
      Promise.resolve(),
    ).then(() => boot.restore());
  }, (err) => {
    ids.forEach((id) => put(id, 'INCONCLUSIVE', { error: String((err && err.message) || err) }));
  });
}

function collectF2P1(side, root, baseline, put, authoredScope) {
  const predicates = require(batchPath('BATCH-F2P-0001', 'predicates.js'));
  const table = predicates.makePredicates(root);
  const scope = authoredScope || scopeLimited(root, baseline);
  // The change-scope postcondition is target-technology anchored: it holds only
  // when the migrated (Fastify) implementation is loaded *and* the diff is
  // limited, so the un-migrated source tree can never satisfy it.
  const technology = semanticContracts(root).technology;
  const techOwned = Boolean(technology && technology.satisfied);
  table['F2P-022'] = () => techOwned && scope.satisfied;
  const contracts = {
    'F2P-001': ['actions'], 'F2P-002': [], 'F2P-003': ['listen'],
    'F2P-004': ['json'], 'F2P-005': ['static'], 'F2P-006': [],
    'F2P-007': ['json'], 'F2P-008': [], 'F2P-009': ['csv'],
    'F2P-010': ['sse'], 'F2P-011': ['json'], 'F2P-012': ['counter'],
    'F2P-013': ['listen'], 'F2P-014': ['json'], 'F2P-015': ['next'],
    'F2P-016': ['actions'], 'F2P-017': ['multipart'], 'F2P-018': ['actions'],
    'F2P-019': [], 'F2P-020': ['multipart'], 'F2P-021': ['next', 'actions', 'routing'],
    'F2P-023': ['next', 'actions', 'routing'], 'F2P-024': ['json'],
  };
  predicates.OBSERVATIONS.forEach((obs) => {
    if (obs.id === 'F2P-022' && scope.satisfied === null) {
      put(obs.id, 'INCONCLUSIVE', { evidence: scope.evidence });
      return;
    }
    if (contracts[obs.id]) {
      putSemantic(root, put, obs.id, contracts[obs.id]);
      return;
    }
    let status;
    let value = { surface: obs.surface, requirement_ids: obs.requirement_ids };
    try {
      status = table[obs.id]() ? 'PASS' : 'FAIL';
    } catch (err) {
      status = 'INCONCLUSIVE';
      value = Object.assign(value, { error: String((err && err.message) || err) });
    }
    put(obs.id, status, value);
  });
}

function collectF2P2(side, root, baseline, put, authoredScope) {
  const contracts = require(batchPath('BATCH-F2P-0002', 'contracts.json'));
  const predicates = require(batchPath('BATCH-F2P-0002', 'predicates.js'));
  let evaluated;
  try {
    const technology = semanticContracts(root).technology;
    evaluated = predicates.evaluateStatic(root, {
      fastifyOwnership: Boolean(technology && technology.satisfied),
    });
  } catch (err) {
    contracts.observations.forEach((obs) => put(obs.id, 'INCONCLUSIVE', { error: String((err && err.message) || err) }));
    return;
  }
  const scope = authoredScope || scopeLimited(root, baseline);
  // Anchor the change-scope postcondition to target-technology ownership so a
  // pre-migration source tree reports FAIL regardless of baseline availability.
  evaluated.P_CHANGE_SCOPE = {
    satisfied: scope.satisfied && evaluated.P_FASTIFY_OWNS.satisfied,
    evidence: scope.evidence.concat(['fastifyOwns=' + evaluated.P_FASTIFY_OWNS.satisfied]),
  };
  const dynamic = {
    P_BODY_LIMIT: ['json'], P_MALFORMED_JSON: ['json'],
    P_CATCHALL_PRECEDENCE: ['next', 'actions', 'routing'], P_APIDOC: ['static'],
    P_FILES_MULTIPART: ['multipart'], P_NEXT_HIJACK: ['next'],
    P_FASTIFY_OWNS: [], P_SIDE_EFFECT_BODY: ['actions'], P_COUNTER_404_500: ['counter'],
    P_PLUGINS_REGISTERED: ['static', 'multipart'],
  };
  contracts.observations.forEach((obs) => {
    if (obs.predicate_id === 'P_CHANGE_SCOPE' && scope.satisfied === null) {
      put(obs.id, 'INCONCLUSIVE', { evidence: scope.evidence });
      return;
    }
    if (dynamic[obs.predicate_id]) {
      putSemantic(root, put, obs.id, dynamic[obs.predicate_id]);
      return;
    }
    const res = evaluated[obs.predicate_id];
    if (!res) {
      put(obs.id, 'INCONCLUSIVE', { predicate_id: obs.predicate_id, evidence: ['predicate not evaluated'] });
      return;
    }
    put(obs.id, res.satisfied ? 'PASS' : 'FAIL', { predicate_id: obs.predicate_id, evidence: res.evidence });
  });
}

function collectF2P3(side, root, put) {
  const probeLib = require(batchPath('BATCH-F2P-0003', 'probe-lib.js'));
  let results;
  try {
    results = probeLib.evaluateAll(root);
  } catch (err) {
    Object.keys(probeLib.PREDICATES).forEach((id) => put(id, 'INCONCLUSIVE', { error: String((err && err.message) || err) }));
    return;
  }
  const dynamic = {
    'F2P-060': [],
    'F2P-049': [], 'F2P-050': [], 'F2P-051': ['json'], 'F2P-052': ['multipart'],
    'F2P-053': ['listen'], 'F2P-054': ['listen'], 'F2P-055': [],
    'F2P-056': [], 'F2P-057': ['actions'], 'F2P-058': [], 'F2P-059': ['next'],
    'F2P-061': ['listen'], 'F2P-062': ['static'], 'F2P-063': ['json'],
    'F2P-064': ['static'], 'F2P-065': ['csv'], 'F2P-067': ['static'],
    'F2P-068': ['sse'], 'F2P-069': [], 'F2P-070': ['static'],
    'F2P-071': ['static'], 'F2P-072': ['static'],
  };
  Object.keys(results).forEach((id) => {
    if (dynamic[id]) {
      putSemantic(root, put, id, dynamic[id]);
      return;
    }
    const status = results[id] === 'PASS' || results[id] === 'FAIL' ? results[id] : 'INCONCLUSIVE';
    put(id, status, {});
  });
}

function buildCaseRequest(port, spec) {
  const method = spec.method;
  if (spec.content_type === 'application/json') {
    const body = Buffer.from(JSON.stringify(spec.body === undefined ? {} : spec.body));
    return request({
      port: port,
      method: method,
      path: spec.path,
      headers: { 'Content-Type': 'application/json', 'Content-Length': body.length },
      body: body,
      timeoutMs: 5000,
    });
  }
  if (spec.content_type === 'multipart/form-data') {
    const desc = spec.body || {};
    const part = multipartBody(desc.field || 'video', desc.filename || 'demo.mp4', Buffer.alloc(32, 1), 'video/mp4');
    return request({
      port: port,
      method: method,
      path: spec.path,
      headers: { 'Content-Type': 'multipart/form-data; boundary=' + part.boundary, 'Content-Length': part.body.length },
      body: part.body,
      timeoutMs: 5000,
    });
  }
  return request({ port: port, method: method, path: spec.path, timeoutMs: 5000 });
}

// Follow a redirect for the GET cases so `/api/doc` projects to its final
// client-observable response.
function buildCaseRequestFollowing(port, spec) {
  if (spec.method === 'GET') {
    return requestFollowingRedirects(port, 'GET', spec.path, 5000);
  }
  return buildCaseRequest(port, spec);
}

function normalizeProjection(projected, requestSpec) {
  if (!projected || !projected.headers) return projected;
  let contentType = String(projected.headers['content-type'] || '').toLowerCase();
  projected.headers = stableHeaders(projected.headers);
  // These actions return no application data. An empty response and the bare
  // status message express the same outcome; neither body spelling is required
  // by the instruction. Data-bearing responses keep their complete projection.
  if (requestSpec && typeof requestSpec === 'object') {
    const routes = require('../batches/BATCH-DIFFERENTIAL-0001/diff-contracts.json')
      .normalization.normalize_status_only_routes;
    const action = routes.includes(requestSpec.method + ' ' + requestSpec.path)
      || (requestSpec.method === 'DELETE' && /^\/recording\/[^/]+$/.test(requestSpec.path));
    const missingCounter = requestSpec.method === 'GET'
      && /^\/recording\/[^/]+\/counter$/.test(requestSpec.path)
      && [404, 500].includes(projected.status);
    if ((action || missingCounter) && projected.body === ''
      && (!contentType || /^text\/(?:plain|html)\s*(?:;|$)/.test(contentType))) {
      projected.body = STATUS_CODES[projected.status];
      contentType = 'text/plain; charset=utf-8';
      projected.headers['content-type'] = contentType;
    }
  }
  // A bare HTTP status message has the same meaning as plain text or HTML.
  // Preserve its status and body, and retain MIME checks for actual documents,
  // JSON, CSV and streams. Only this text-only representation is equivalent.
  if (/^text\/(?:plain|html)\s*(?:;|$)/.test(contentType)
    && typeof projected.body === 'string'
    && projected.body === STATUS_CODES[projected.status]) {
    projected.headers['content-type'] = 'text/plain; charset=utf-8';
  }
  if (contentType.indexOf('text/csv') !== -1 && typeof projected.body === 'string') {
    // CSV quoting and line terminators are not semantic.
    projected.body = stripQuotes(projected.body).replace(/\r\n/g, '\n');
  } else if (contentType.indexOf('application/json') === -1 && typeof projected.body === 'string') {
    // Captured stdout / streamed text carries boot ports and temp paths.
    projected.body = stableText(projected.body);
  }
  return projected;
}

function collectDIFF1(side, root, put) {
  const diffHarness = require(batchPath('BATCH-DIFFERENTIAL-0001', 'diff-harness.js'));
  const diff2 = require(batchPath('BATCH-DIFFERENTIAL-0002', 'harness.js'));
  const cases = require(assetPath('fixtures', 'batches', 'BATCH-DIFFERENTIAL-0001', 'cases.json')).cases;
  return bootServer(root).then((boot) => {
    const empty = { statusCode: null, headers: {}, body: Buffer.alloc(0) };
    return cases.reduce(
      (chain, testCase) => chain.then(() => {
        if (testCase.observation_id === 'DIFF-005' || testCase.observation_id === 'DIFF-007') {
          const name = testCase.observation_id === 'DIFF-005' ? 'mjpeg' : 'console';
          const result = semanticContracts(root)[name];
          put(testCase.observation_id, result ? (result.satisfied ? 'PASS' : 'FAIL') : 'INCONCLUSIVE',
            { contract: name, satisfied: Boolean(result && result.satisfied) });
          return undefined;
        }
        const spec = testCase.request;
        // The CSV cases need a populated counter history to answer at all.
        const isCsv = /\/counter\/csv$/.test(spec.path);
        boot.setCounterData(isCsv ? counterFixture() : {});
        const stream = /\/(tracker\/sse|console|webcam\/stream)$/.test(spec.path);
        const pending = stream
          ? withTimeout(diff2.readStreamHead(boot.port, spec.path, { timeoutMs: 4000, maxBytes: 2048 }), 12000)
          : withTimeout(buildCaseRequestFollowing(boot.port, spec), 12000);
        return pending.then((raw) => {
          if (raw === TIMEOUT || !raw) {
            put(testCase.observation_id, 'TIMEOUT', scrub(diffHarness.project(empty)));
          } else {
            put(testCase.observation_id, 'PASS', scrub(normalizeProjection(diffHarness.project(raw), spec)));
          }
        }, (err) => {
          put(testCase.observation_id, 'INCONCLUSIVE', { error: String((err && err.message) || err) });
        });
      }),
      Promise.resolve(),
    ).then(() => boot.restore());
  }, (err) => {
    cases.forEach((testCase) => put(testCase.observation_id, 'INCONCLUSIVE', { error: String((err && err.message) || err) }));
  });
}

// The DIFFERENTIAL-0002 probe spawns `server.js` as a child process. Inject the
// dependency-stub preload through NODE_OPTIONS so the spawned server boots
// against the stubs instead of a missing Next.js build / MongoDB.
function prepareChildEnvironment(root) {
  const nodeModules = resolveNodeModules(root);
  if (nodeModules) {
    process.env.NODE_PATH = nodeModules;
    try {
      require('module').Module._initPaths();
    } catch (err) { /* not fatal: the child reads NODE_PATH from its env */ }
  }
  const requireFlag = '--require ' + CHILD_STUB;
  const existing = process.env.NODE_OPTIONS || '';
  const flags = existing.split(/\s+/).filter(Boolean);
  if (flags.indexOf(requireFlag) === -1) {
    process.env.NODE_OPTIONS = existing ? existing + ' ' + requireFlag : requireFlag;
  }
}

function collectDIFF2(side, root, put) {
  const projections = require(batchPath('BATCH-DIFFERENTIAL-0002', 'projections.js'));
  const contracts = require(batchPath('BATCH-DIFFERENTIAL-0002', 'contracts.json'));
  const ids = contracts.observations.map((obs) => obs.id);
  const markAll = (status, value) => ids.forEach((id) => put(id, status, value));
  prepareChildEnvironment(root);
  const streams = {};
  [['DIFF-033', 'mjpeg'], ['DIFF-035', 'console']].forEach(([id, name]) => {
    const observed = semanticContracts(root)[name];
    streams[id] = { contract: name, satisfied: Boolean(observed && observed.satisfied) };
  });
  return withTimeout(projections.run(root, { side: side, port: 18240, timeoutMs: 15000,
    streamObservations: streams }), 120000).then((result) => {
    if (result === TIMEOUT) {
      markAll('TIMEOUT', { timedOut: true });
      return;
    }
    ids.forEach((id) => {
      if (id === 'DIFF-033' || id === 'DIFF-035') {
        const name = id === 'DIFF-033' ? 'mjpeg' : 'console';
        const observed = semanticContracts(root)[name];
        put(id, observed ? (observed.satisfied ? 'PASS' : 'FAIL') : 'INCONCLUSIVE',
          { contract: name, satisfied: Boolean(observed && observed.satisfied) });
        return;
      }
      if (result && result.observations && Object.prototype.hasOwnProperty.call(result.observations, id)) {
        put(id, 'PASS', scrub(result.observations[id]));
      } else {
        put(id, 'INCONCLUSIVE', { reason: 'projection missing' });
      }
    });
  }, (err) => {
    markAll('INCONCLUSIVE', { error: String((err && err.message) || err) });
  });
}

module.exports = {
  normalizeProjection,
  scopeLimited,
  collectP2P,
  collectF2P1,
  collectF2P2,
  collectF2P3,
  collectDIFF1,
  collectDIFF2,
};
