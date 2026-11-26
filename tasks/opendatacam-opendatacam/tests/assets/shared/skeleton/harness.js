'use strict';

// Shared harness skeleton for the Express -> Fastify server.js migration.
//
// This module is deliberately dependency-free (Node core only) so that any
// probe batch (source-side Express probes, target-side Fastify probes) can
// require it without pulling in project or target runtime dependencies.
//
// Batches that own observations build on these primitives; they MUST NOT
// promote this file to `source/runner` or `target/runner.js` (INTEGRATION owns
// those paths).

const http = require('http');

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
    if (body) req.write(body);
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

function multipartBody(field, filename, content, contentType) {
  const boundary = `----skeleton${Date.now()}`;
  const head = Buffer.from(
    `--${boundary}\r\n`
    + `Content-Disposition: form-data; name="${field}"; filename="${filename}"\r\n`
    + `Content-Type: ${contentType || 'application/octet-stream'}\r\n\r\n`,
  );
  const tail = Buffer.from(`\r\n--${boundary}--\r\n`);
  return {
    boundary,
    body: Buffer.concat([head, Buffer.from(content), tail]),
  };
}

function collectSse(port, path, options) {
  const opts = options || {};
  const maxEvents = opts.maxEvents || 1;
  const timeoutMs = opts.timeoutMs || 5000;
  return new Promise((resolve, reject) => {
    const events = [];
    const req = http.request({ host: '127.0.0.1', port, method: 'GET', path }, (res) => {
      let buffer = '';
      let skipLF = false;
      res.setEncoding('utf8');
      res.on('data', (chunk) => {
        // SSE permits LF, CRLF and CR line endings. Carry a trailing CR across
        // chunks so its following LF cannot become an extra empty line.
        if (skipLF && chunk[0] === '\n') chunk = chunk.slice(1);
        skipLF = chunk.endsWith('\r');
        buffer += chunk.replace(/\r\n|\r/g, '\n');
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
      if (events.length >= maxEvents) resolve({ events });
      else reject(err);
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve({ events, timedOut: true });
    });
    req.end();
  });
}

module.exports = { request, jsonRequest, multipartBody, collectSse };
