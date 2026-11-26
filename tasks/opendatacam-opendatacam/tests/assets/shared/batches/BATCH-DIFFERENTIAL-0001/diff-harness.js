'use strict';

// Shared DIFFERENTIAL harness for the Express -> Fastify server.js migration.
//
// Dependency-free (Node core only). Provides:
//   * a normalized result projection shared by source and target probes,
//   * a structural comparator used to decide `equal_after_normalization`,
//   * a read-only static inspector used to compare the module graphs of the
//     source (Express) and target (Fastify) server entry points.
//
// This file MUST NOT be promoted to `source/runner` or `target/runner.js`;
// those paths are owned exclusively by the INTEGRATION batch.

const fs = require('fs');
const path = require('path');

const STRIP_HEADERS = new Set([
  'date', 'connection', 'keep-alive', 'transfer-encoding',
  'content-length', 'etag', 'x-powered-by',
]);

function normalizeHeaders(headers) {
  const out = {};
  Object.keys(headers || {}).forEach((key) => {
    const lower = key.toLowerCase();
    if (STRIP_HEADERS.has(lower)) return;
    out[lower] = headers[key];
  });
  return out;
}

function normalizeBody(body, contentType) {
  if (body === null || body === undefined) return null;
  const text = Buffer.isBuffer(body) ? body.toString('utf8') : String(body);
  if (contentType && String(contentType).includes('application/json')) {
    try {
      return sortKeys(JSON.parse(text));
    } catch (err) {
      return text;
    }
  }
  return text;
}

function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === 'object') {
    const out = {};
    Object.keys(value).sort().forEach((key) => { out[key] = sortKeys(value[key]); });
    return out;
  }
  return value;
}

// Projects a raw probe result into the normalized shape compared by the gates.
function project(result) {
  return {
    status: result.statusCode || result.status || null,
    headers: normalizeHeaders(result.headers),
    body: normalizeBody(result.body, result.headers && (result.headers['content-type'] || result.headers['Content-Type'])),
  };
}

function equalAfterNormalization(left, right) {
  return JSON.stringify(project(left)) === JSON.stringify(project(right));
}

// ---- Read-only static inspector -------------------------------------------

const REQUIRED_TARGET_IMPORTS = ['fastify', '@fastify/static', '@fastify/multipart'];
const FORBIDDEN_SOURCE_IMPORTS = [
  'express', 'body-parser', 'serve-static', 'csv-express', 'server-sent-events', 'multer',
];

function readIfExists(file) {
  try { return fs.readFileSync(file, 'utf8'); } catch (err) { return null; }
}

function inspectModuleGraph(root, entry) {
  const serverPath = path.join(root, entry);
  const manifestPath = path.join(root, 'package.json');
  const server = readIfExists(serverPath) || '';
  const manifest = readIfExists(manifestPath) || '{}';
  const requires = [];
  const re = /require\(\s*['"]([^'"]+)['"]\s*\)/g;
  let match = re.exec(server);
  while (match !== null) {
    requires.push(match[1]);
    match = re.exec(server);
  }
  let deps = {};
  try { deps = JSON.parse(manifest).dependencies || {}; } catch (err) { deps = {}; }
  return {
    root,
    entry,
    requires,
    dependencies: Object.keys(deps).sort(),
    requiredImports: REQUIRED_TARGET_IMPORTS.filter((name) => requires.includes(name)),
    forbiddenImports: FORBIDDEN_SOURCE_IMPORTS.filter((name) => requires.includes(name)),
    forbiddenDependencies: FORBIDDEN_SOURCE_IMPORTS.filter((name) => Object.prototype.hasOwnProperty.call(deps, name)),
  };
}

module.exports = {
  STRIP_HEADERS,
  normalizeHeaders,
  normalizeBody,
  project,
  equalAfterNormalization,
  inspectModuleGraph,
  REQUIRED_TARGET_IMPORTS,
  FORBIDDEN_SOURCE_IMPORTS,
};
