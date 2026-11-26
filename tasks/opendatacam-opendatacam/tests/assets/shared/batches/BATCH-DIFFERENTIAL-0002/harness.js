'use strict';

// Shared differential harness for BATCH-DIFFERENTIAL-0002 (server.js Express -> Fastify).
//
// Dependency-free (Node core only) so both the source-side (Express) probe and
// the target-side (Fastify) probe can require it without pulling in project or
// target runtime dependencies. This module owns no observation result: the
// probes build the normalized projections and the fragment declares the
// `equal_after_normalization` relation between them.

const http = require('http');
const net = require('net');
const { spawn } = require('child_process');

function request(options, body) {
  return new Promise((resolve, reject) => {
    const req = http.request(options, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => resolve({
        statusCode: res.statusCode,
        headers: res.headers,
        body: Buffer.concat(chunks),
      }));
    });
    req.on('error', reject);
    if (body !== undefined && body !== null) req.write(body);
    req.end();
  });
}

function jsonRequest(port, method, path, payload) {
  const body = payload === undefined ? null : Buffer.from(JSON.stringify(payload));
  return request({
    host: '127.0.0.1',
    port,
    method,
    path,
    headers: body
      ? { 'Content-Type': 'application/json', 'Content-Length': body.length }
      : {},
  }, body);
}

function rawRequest(port, method, path, headers, body) {
  return request({
    host: '127.0.0.1',
    port,
    method,
    path,
    headers: headers || {},
  }, body === undefined ? null : body);
}

// Read the first bytes of a streaming response and then abort, so token-stream
// routes (/console, /webcam/stream, /tracker/sse) never block the probe.
function readStreamHead(port, path, options) {
  const opts = options || {};
  const timeoutMs = opts.timeoutMs || 6000;
  const maxBytes = opts.maxBytes || 4096;
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    const req = http.request({ host: '127.0.0.1', port, method: 'GET', path }, (res) => {
      res.on('data', (chunk) => {
        if (size < maxBytes) {
          const slice = chunk.subarray(0, maxBytes - size);
          chunks.push(slice);
          size += slice.length;
        }
        if (size >= maxBytes) {
          req.destroy();
          resolve({
            statusCode: res.statusCode,
            headers: res.headers,
            body: Buffer.concat(chunks),
          });
        }
      });
      res.on('end', () => resolve({
        statusCode: res.statusCode,
        headers: res.headers,
        body: Buffer.concat(chunks),
      }));
    });
    req.on('error', (err) => {
      if (chunks.length > 0) {
        resolve({ headers: {}, body: Buffer.concat(chunks), aborted: true, error: err.message });
      } else {
        reject(err);
      }
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve({ headers: {}, body: Buffer.concat(chunks), timedOut: true });
    });
    req.end();
  });
}

function collectSse(port, path, options) {
  const opts = options || {};
  const maxEvents = opts.maxEvents || 3;
  const timeoutMs = opts.timeoutMs || 6000;
  return new Promise((resolve, reject) => {
    const events = [];
    const req = http.request({ host: '127.0.0.1', port, method: 'GET', path }, (res) => {
      let buffer = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => {
        buffer += chunk;
        let index = buffer.indexOf('\n\n');
        while (index !== -1) {
          events.push(buffer.slice(0, index));
          buffer = buffer.slice(index + 2);
          if (events.length >= maxEvents) {
            req.destroy();
            resolve({ statusCode: res.statusCode, headers: res.headers, events });
            return;
          }
          index = buffer.indexOf('\n\n');
        }
      });
      res.on('end', () => resolve({ statusCode: res.statusCode, headers: res.headers, events }));
    });
    req.on('error', (err) => {
      if (events.length > 0) resolve({ events });
      else reject(err);
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve({ events, timedOut: true });
    });
    req.end();
  });
}

function multipartBody(field, filename, content, contentType) {
  const boundary = `----differential${Date.now()}`;
  const head = Buffer.from(
    `--${boundary}\r\n`
    + `Content-Disposition: form-data; name="${field}"; filename="${filename}"\r\n`
    + `Content-Type: ${contentType || 'application/octet-stream'}\r\n\r\n`,
  );
  const tail = Buffer.from(`\r\n--${boundary}--\r\n`);
  return { boundary, body: Buffer.concat([head, Buffer.from(content), tail]) };
}

// Deterministic multipart/x-mixed-replace upstream used to observe that
// GET /webcam/stream proxies frames byte-for-byte.
function startMockMjpeg(port, frames) {
  const boundary = 'mockframe';
  const server = http.createServer((req, res) => {
    res.writeHead(200, {
      'Content-Type': `multipart/x-mixed-replace; boundary=${boundary}`,
      Connection: 'close',
    });
    for (const frame of frames) {
      res.write(`--${boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: ${frame.length}\r\n\r\n`);
      res.write(Buffer.from(frame));
      res.write('\r\n');
    }
    res.end();
  });
  return new Promise((resolve) => {
    server.listen(port, '127.0.0.1', () => resolve({
      boundary,
      close: () => new Promise((done) => server.close(done)),
    }));
  });
}

function waitForPort(port, timeoutMs, intervalMs) {
  const deadline = Date.now() + (timeoutMs || 30000);
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const socket = net.connect({ host: '127.0.0.1', port });
      socket.once('connect', () => {
        socket.destroy();
        resolve(true);
      });
      socket.once('error', () => {
        socket.destroy();
        if (Date.now() > deadline) reject(new Error(`port ${port} not ready`));
        else setTimeout(attempt, intervalMs || 250);
      });
    };
    attempt();
  });
}

// Spawn the repo's server.js on a fixed port and resolve once it is listening.
function startServer(options) {
  const opts = options || {};
  const port = opts.port;
  const env = Object.assign({}, process.env, opts.env || {}, { PORT: String(port) });
  const child = spawn(process.execPath, ['server.js'], {
    cwd: opts.cwd,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
  child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

  const ready = waitForPort(port, opts.timeoutMs || 60000).then(() => ({
    child,
    port,
    output: () => stdout + stderr,
  }));

  return {
    child,
    ready,
    output: () => stdout + stderr,
    stop: () => new Promise((resolve) => {
      if (child.exitCode !== null || child.signalCode !== null) {
        resolve({ code: child.exitCode, signal: child.signalCode });
        return;
      }
      child.once('exit', (code, signal) => resolve({ code, signal }));
      child.kill('SIGTERM');
      setTimeout(() => child.kill('SIGKILL'), 3000).unref();
    }),
  };
}

// Stable normalization used by every differential projection: JSON is parsed,
// volatile fields (dates, ports, absolute paths, durations) are reduced to
// shape descriptors, and header order is ignored.
function normalize(value) {
  if (Array.isArray(value)) return value.map(normalize);
  if (value && typeof value === 'object') {
    const out = {};
    Object.keys(value).sort().forEach((key) => { out[key] = normalize(value[key]); });
    return out;
  }
  if (typeof value === 'number' && !Number.isInteger(value)) return '<number>';
  return value;
}

function normalizeJson(buffer) {
  try {
    return normalize(JSON.parse(buffer.toString('utf8')));
  } catch (err) {
    return { unparseable: true };
  }
}

function header(headers, name) {
  if (!headers) return null;
  const key = String(name).toLowerCase();
  const found = Object.keys(headers).find((h) => h.toLowerCase() === key);
  return found === undefined ? null : headers[found];
}

module.exports = {
  request,
  jsonRequest,
  rawRequest,
  readStreamHead,
  collectSse,
  multipartBody,
  startMockMjpeg,
  waitForPort,
  startServer,
  normalize,
  normalizeJson,
  header,
};
