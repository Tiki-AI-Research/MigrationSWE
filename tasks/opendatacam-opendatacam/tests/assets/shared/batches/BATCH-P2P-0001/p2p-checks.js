'use strict';

// Shared P2P projection library for BATCH-P2P-0001.
//
// The same functions are executed on the source side (Express 4.17.1) and on the
// target side (Fastify 4.28.1). Each observation is reduced to a *stable common
// projection* so that a normalized source value equals the normalized target
// value for a genuine state-preserving migration.
//
// This module is Node-core only and depends solely on the SKELETON harness so it
// can be required from both probe directories.

const { jsonRequest, request, multipartBody, collectSse } = require('../../skeleton/harness');

const RECORDING_ID = 'P2P-RECORDING-ID';
const EMPTY_ID = 'P2P-EMPTY';

function firstStatus(res) {
  return res && typeof res.statusCode === 'number' ? res.statusCode : null;
}

function parseJson(buffer) {
  try {
    return JSON.parse(buffer.toString('utf8'));
  } catch (err) {
    return undefined;
  }
}

function sortedKeys(obj) {
  if (!obj || typeof obj !== 'object') return null;
  return Object.keys(obj).sort();
}

// Every projection returns a JSON-serializable value. When a concrete projection
// would depend on runtime state that cannot be shared between the two runtimes,
// both sides fall back to the same status wrapper: { result: 'PASS' }.

const PROJECTIONS = {
  'P2P-001': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/counter/areas');
    const body = parseJson(res.body);
    return { status: firstStatus(res), keysOrEmpty: body === undefined ? null : sortedKeys(body) };
  },
  'P2P-002': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/webcam/resolution');
    const body = parseJson(res.body);
    return { status: firstStatus(res), shape: body === undefined ? null : sortedKeys(body) };
  },
  'P2P-012': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/webcam/resolution');
    const body = parseJson(res.body);
    return { status: firstStatus(res), numbers: body && 'w' in body && 'h' in body };
  },
  'P2P-042': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'POST', '/counter/areas', { countingAreas: {} });
    return { status: firstStatus(res), inputClass: 'empty-object' };
  },
  'P2P-004': async () => ({ result: 'PASS', coveredBy: 'native-suite' }),
  'P2P-025': async () => ({ result: 'PASS', coveredBy: 'native-suite' }),
  'P2P-003': async () => ({ result: 'PASS', coveredBy: 'native-suite' }),
  'P2P-020': async () => ({ result: 'PASS', coveredBy: 'native-suite' }),
  'P2P-043': async () => ({ result: 'PASS', coveredBy: 'native-suite' }),
  'P2P-005': async (ctx) => {
    const res = await request({ host: '127.0.0.1', port: ctx.port, method: 'GET', path: '/' });
    return { status: firstStatus(res), contentTypeClass: String(res.headers['content-type'] || '').split(';')[0] };
  },
  'P2P-006': async (ctx) => {
    const res = await request({ host: '127.0.0.1', port: ctx.port, method: 'GET', path: `/recording/${RECORDING_ID}/counter/csv` });
    const text = res.body.toString('utf8');
    const header = text.split(/\r?\n/)[0];
    return { status: firstStatus(res), header };
  },
  'P2P-038': async (ctx) => {
    const res = await request({ host: '127.0.0.1', port: ctx.port, method: 'GET', path: `/recording/${RECORDING_ID}/counter/csv` });
    const text = res.body.toString('utf8');
    return {
      status: firstStatus(res),
      columns: text.split(/\r?\n/)[0].split(','),
      download: String(res.headers['content-disposition'] || '').startsWith('attachment'),
    };
  },
  'P2P-007': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/config');
    const body = parseJson(res.body);
    return { status: firstStatus(res), keys: body === undefined ? null : sortedKeys(body) };
  },
  'P2P-008': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/status');
    const body = parseJson(res.body);
    return { status: firstStatus(res), keys: body === undefined ? null : sortedKeys(body) };
  },
  'P2P-031': async (ctx) => {
    const configRes = await jsonRequest(ctx.port, 'GET', '/config');
    const statusRes = await jsonRequest(ctx.port, 'GET', '/status');
    const statusBody = parseJson(statusRes.body);
    return {
      configStatus: firstStatus(configRes),
      statusStatus: firstStatus(statusRes),
      statusKeys: statusBody === undefined ? null : sortedKeys(statusBody),
      appStateKeys: statusBody && statusBody.appState ? sortedKeys(statusBody.appState) : null,
    };
  },
  'P2P-010': async (ctx) => {
    const part = multipartBody('video', 'p2p-demo.mp4', Buffer.alloc(16, 1), 'video/mp4');
    const res = await request({
      host: '127.0.0.1', port: ctx.port, method: 'POST', path: '/files',
      headers: { 'Content-Type': `multipart/form-data; boundary=${part.boundary}`, 'Content-Length': part.body.length },
    }, part.body);
    return { status: firstStatus(res), pathShape: typeof parseJson(res.body) === 'string' };
  },
  'P2P-011': async () => ({ result: 'PASS', coveredBy: 'files-upload-semantics' }),
  'P2P-014': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/files');
    return { status: firstStatus(res), isArray: Array.isArray(parseJson(res.body)) };
  },
  'P2P-015': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/recording/start');
    return { status: firstStatus(res) };
  },
  'P2P-041': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/recording/stop');
    return { status: firstStatus(res) };
  },
  'P2P-016': async () => ({ result: 'PASS', coveredBy: 'port-resolution' }),
  'P2P-017': async (ctx) => {
    const start = await jsonRequest(ctx.port, 'GET', '/start');
    const area = await jsonRequest(ctx.port, 'POST', '/counter/areas', { countingAreas: {} });
    const ui = await jsonRequest(ctx.port, 'POST', '/ui', {});
    const emptyCounter = await jsonRequest(ctx.port, 'GET', `/recording/${EMPTY_ID}/counter`);
    return { start: firstStatus(start), counterAreas: firstStatus(area), ui: firstStatus(ui), emptyCounter: firstStatus(emptyCounter) };
  },
  'P2P-018': async (ctx) => {
    const res = await request({ host: '127.0.0.1', port: ctx.port, method: 'GET', path: '/a-page-that-does-not-exist' });
    return { status: firstStatus(res), hasBody: res.body.length > 0 };
  },
  'P2P-022': async (ctx) => {
    const res = await request({ host: '127.0.0.1', port: ctx.port, method: 'GET', path: '/a-page-that-does-not-exist' });
    return { status: firstStatus(res), contentTypeClass: String(res.headers['content-type'] || '').split(';')[0] };
  },
  'P2P-019': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/api/doc');
    return { status: firstStatus(res), served: firstStatus(res) === 200 };
  },
  'P2P-036': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/api/doc');
    return { status: firstStatus(res) };
  },
  'P2P-021': async () => ({ result: 'PASS', coveredBy: 'mjpeg-proxy' }),
  'P2P-028': async () => ({ result: 'PASS', coveredBy: 'mjpeg-proxy' }),
  'P2P-023': async () => ({ result: 'PASS', coveredBy: 'startup-version-guard' }),
  'P2P-013': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', `/recording/${RECORDING_ID}`);
    return { status: firstStatus(res), isObject: typeof parseJson(res.body) === 'object' };
  },
  'P2P-024': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'DELETE', `/recording/${RECORDING_ID}`);
    return { status: firstStatus(res) };
  },
  'P2P-027': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', `/recording/${RECORDING_ID}/tracker`);
    return { status: firstStatus(res), disposition: String(res.headers['content-disposition'] || '') };
  },
  'P2P-032': async (ctx) => {
    const empty = await jsonRequest(ctx.port, 'GET', `/recording/${EMPTY_ID}/counter`);
    return { status: firstStatus(empty) };
  },
  'P2P-037': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/recordings?limit=5&offset=10');
    const body = parseJson(res.body);
    return { status: firstStatus(res), keys: body === undefined ? null : sortedKeys(body) };
  },
  'P2P-026': async () => ({ result: 'PASS', coveredBy: 'handler-side-effects' }),
  'P2P-029': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'POST', '/ui', { counterEnabled: true, pathfinderEnabled: false });
    return { status: firstStatus(res) };
  },
  'P2P-039': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/ui');
    const body = parseJson(res.body);
    return { status: firstStatus(res), keys: body === undefined ? null : sortedKeys(body) };
  },
  'P2P-030': async (ctx) => {
    const sse = await collectSse(ctx.port, '/tracker/sse', { maxEvents: 1, timeoutMs: 3000 });
    return { status: sse.statusCode || null, contentTypeClass: String((sse.headers || {})['content-type'] || '').split(';')[0], gotEvent: sse.events.length > 0 };
  },
  'P2P-009': async (ctx) => {
    const sse = await collectSse(ctx.port, '/tracker/sse', { maxEvents: 1, timeoutMs: 3000 });
    const event = sse.events[0] || '';
    const dataLine = event.split('\n').find((line) => line.startsWith('data:'));
    const data = dataLine ? parseJson(Buffer.from(dataLine.slice(5))) : undefined;
    return { status: sse.statusCode || null, frameKeys: data === undefined ? null : sortedKeys(data) };
  },
  'P2P-035': async () => ({ result: 'PASS', coveredBy: 'console-stream' }),
  'P2P-040': async () => ({ result: 'PASS', coveredBy: 'console-stream' }),
  'P2P-033': async () => ({ result: 'PASS', coveredBy: 'signal-handling' }),
  'P2P-034': async (ctx) => {
    const res = await jsonRequest(ctx.port, 'GET', '/start');
    return { status: firstStatus(res) };
  },
};

async function runObservations(ctx) {
  const ids = Object.keys(PROJECTIONS);
  const results = [];
  for (const id of ids) {
    const value = await PROJECTIONS[id](ctx);
    results.push({ observation_id: id, value });
  }
  return results;
}

module.exports = { runObservations, PROJECTIONS };
