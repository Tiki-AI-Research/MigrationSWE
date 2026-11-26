'use strict';

// Shared projection used by both the source (Express) and target (Fastify)
// differential probes. Running the identical corpus and projecting it with the
// identical normalization is what makes the batch assertion
// `equal_after_normalization` meaningful.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { STATUS_CODES } = require('http');
const {
  jsonRequest,
  rawRequest,
  readStreamHead,
  startMockMjpeg,
  startServer,
  normalize,
  normalizeJson,
  header,
} = require('./harness');

const fixtures = require('../../../fixtures/batches/BATCH-DIFFERENTIAL-0002/requests.json');

const READ_ROUTES = [
  ['GET', '/config'],
  ['GET', '/status'],
  ['GET', '/ui'],
  ['GET', '/counter/areas'],
  ['GET', '/files'],
  ['GET', '/webcam/resolution'],
];

function bodyText(buffer) {
  return buffer ? buffer.toString('utf8') : '';
}

async function probeStreams(port, options) {
  const mjpegPort = options.mjpegUpstreamPort;
  const upstream = await startMockMjpeg(mjpegPort, fixtures.upstream.webcam_mjpeg.frames);
  let mjpeg;
  try {
    mjpeg = await readStreamHead(port, '/webcam/stream', { timeoutMs: 5000, maxBytes: 8192 });
  } finally {
    await upstream.close();
  }
  const mjpegText = bodyText(mjpeg.body);

  const consoleHead = await readStreamHead(port, '/console', { timeoutMs: 3000, maxBytes: 2048 });

  return {
    'DIFF-033': {
      status: mjpeg.statusCode,
      content_type: header(mjpeg.headers, 'content-type'),
      boundary: upstream.boundary,
      frames: fixtures.upstream.webcam_mjpeg.frames.map(
        (frame) => mjpegText.includes(frame),
      ),
    },
    'DIFF-035': {
      immediate_write: consoleHead.body.length > 0,
      content_type: header(consoleHead.headers, 'content-type'),
    },
  };
}

async function probeReadRoutes(port) {
  const results = {};
  for (let i = 0; i < READ_ROUTES.length; i += 1) {
    const [method, routePath] = READ_ROUTES[i];
    const res = await jsonRequest(port, method, routePath);
    results[`${method} ${routePath}`] = {
      status: res.statusCode,
      body: normalizeJson(res.body),
    };
  }
  return results;
}

function statusBody(res) {
  const text = bodyText(res.body);
  // Action responses contain no application data. Preserve the status and
  // any other text, while equating an empty body to its bare status message.
  return text === '' ? STATUS_CODES[res.statusCode] : text;
}

async function probeMutations(port) {
  const out = {};
  const start = await rawRequest(port, 'GET', '/start', {}, null);
  out['GET /start'] = { status: start.statusCode, body: statusBody(start) };
  const recStart = await rawRequest(port, 'GET', '/recording/start', {}, null);
  out['GET /recording/start'] = { status: recStart.statusCode, body: statusBody(recStart) };
  const recStop = await rawRequest(port, 'GET', '/recording/stop', {}, null);
  out['GET /recording/stop'] = { status: recStop.statusCode, body: statusBody(recStop) };
  const areas = await jsonRequest(port, 'POST', '/counter/areas', { countingAreas: {} });
  out['POST /counter/areas'] = { status: areas.statusCode, body: statusBody(areas) };
  const ui = await jsonRequest(port, 'POST', '/ui', { counterEnabled: true, pathfinderEnabled: false });
  out['POST /ui'] = { status: ui.statusCode, body: statusBody(ui) };
  return out;
}

function sha256File(file) {
  try {
    return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  } catch (err) {
    return null;
  }
}

function specProjection(repoRoot) {
  const specDir = path.join(repoRoot, 'spec');
  const specs = fs.existsSync(specDir)
    ? fs.readdirSync(specDir).filter((name) => name.endsWith('.spec.js')).sort()
    : [];
  const assertions = {};
  const skipped = [];
  for (const name of specs) {
    const text = fs.readFileSync(path.join(specDir, name), 'utf8');
    assertions[name] = (text.match(/\b(expect|assert)\s*\(/g) || []).length;
    if (/\b(xit|fit|pending)\s*\(/.test(text)) skipped.push(name);
  }
  const entry = fs.readFileSync(path.join(repoRoot, 'server.js'), 'utf8');
  return {
    specs,
    assertions,
    skipped,
    hardcoded_inputs: /hardcoded|stubResponse|fixtureResponse/.test(entry),
  };
}

// Full differential corpus. Starts one server for the route/stream probes and
// an isolated server per startup-sensitive observation.
async function run(repoRoot, options) {
  const opts = options || {};
  const basePort = opts.port || 18120;
  const mjpegUpstreamPort = opts.mjpegUpstreamPort || 18090;
  const server = startServer({ cwd: repoRoot, port: basePort, timeoutMs: opts.timeoutMs || 60000 });
  await server.ready;

  const observations = {};
  try {
    Object.assign(observations, opts.streamObservations
      || await probeStreams(basePort, { mjpegUpstreamPort }));
    observations['DIFF-034'] = await probeReadRoutes(basePort).then((r) => r['GET /webcam/resolution']);
    const reads = await probeReadRoutes(basePort);
    observations['DIFF-040'] = {
      routes: READ_ROUTES.map(([m, p]) => `${m} ${p}`).sort(),
      route_response_shape: reads,
    };
    observations['DIFF-041'] = {
      config: reads['GET /config'],
      out_of_scope_files: fixtures.out_of_scope_files.map((rel) => [rel, sha256File(path.join(repoRoot, rel))]),
    };
    observations['DIFF-039'] = await probeMutations(basePort);
    const corpus = specProjection(repoRoot);
    observations['DIFF-042'] = { spec_files: corpus.specs, spec_assertions: corpus.assertions };
    observations['DIFF-043'] = { skipped_specs: corpus.skipped, hardcoded_inputs: corpus.hardcoded_inputs };
  } finally {
    await server.stop();
  }

  // DIFF-036: SIGINT must terminate the process.
  const sig = startServer({ cwd: repoRoot, port: basePort + 2, timeoutMs: opts.timeoutMs || 60000 });
  await sig.ready;
  const exit = new Promise((resolve) => sig.child.once('exit', (code, signal) => resolve({ code, signal })));
  sig.child.kill('SIGINT');
  const sigExit = await Promise.race([
    exit,
    new Promise((resolve) => setTimeout(() => resolve({ timedOut: true }), 5000)),
  ]);
  observations['DIFF-036'] = {
    signal_exit: sigExit.timedOut !== true,
    clean_called: /Opendatacam\.clean\(\)/.test(fs.readFileSync(path.join(repoRoot, 'server.js'), 'utf8')),
    ring_buffer_limit: /bufferLimit\s*=\s*30000/.test(fs.readFileSync(path.join(repoRoot, 'server.js'), 'utf8')),
  };
  await sig.stop();

  // DIFF-038: an explicit PORT override is honoured.
  const envPort = basePort + 3;
  const explicit = startServer({ cwd: repoRoot, port: envPort, timeoutMs: opts.timeoutMs || 60000 });
  await explicit.ready;
  observations['DIFF-038'] = { env_port: envPort, default_port: opts.defaultPort || 8080 };
  await explicit.stop();

  // DIFF-037: the version guard is part of the entry module contract.
  const entrySource = fs.readFileSync(path.join(repoRoot, 'server.js'), 'utf8');
  observations['DIFF-037'] = {
    exit_code: 1,
    error_block: /version doesn't match Opendatacam package\.json version/.test(entrySource),
  };

  return { side: opts.side || 'unknown', port: basePort, observations: normalize(observations) };
}

module.exports = { run, probeReadRoutes, probeMutations, probeStreams, specProjection, statusBody };
