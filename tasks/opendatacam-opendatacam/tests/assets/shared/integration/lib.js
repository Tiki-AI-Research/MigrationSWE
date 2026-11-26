'use strict';

// Shared primitives for the INTEGRATION harness.
//
// This module is Node-core only. It resolves the *asset root* from its own
// location (so nothing here hardcodes an authoring path) and the *repository
// under test* strictly from the documented environment aliases.

const fs = require('fs');
const path = require('path');
const http = require('http');

// <assetRoot>/shared/integration/lib.js -> <assetRoot>
const ASSET_ROOT = path.resolve(__dirname, '..', '..');

const TIMEOUT = Symbol('integration-timeout');

// Runtime harnesses may only read the repo root from these aliases. Project
// derived aliases (e.g. REPO_ROOT) are intentionally not consulted.
const REPO_ENV_ALIASES = ['MIGRATION_REPO_ROOT', 'MIGRATION_REPO', 'REPO_ROOT', 'REPO'];
const BASELINE_ENV_ALIASES = [
  'MIGRATION_SOURCE_ROOT',
  'MIGRATION_BASE_ROOT',
  'SOURCE_ROOT',
];

function assetPath() {
  return path.join.apply(path, [ASSET_ROOT].concat(Array.prototype.slice.call(arguments)));
}

function batchPath() {
  return assetPath.apply(null, ['shared', 'batches'].concat(Array.prototype.slice.call(arguments)));
}

function isDir(candidate) {
  try {
    return Boolean(candidate) && fs.statSync(candidate).isDirectory();
  } catch (err) {
    return false;
  }
}

function envDir(aliases) {
  for (let i = 0; i < aliases.length; i += 1) {
    const value = process.env[aliases[i]];
    if (value && String(value).trim()) {
      const resolved = path.resolve(String(value).trim());
      if (isDir(resolved)) return resolved;
    }
  }
  return null;
}

function resolveRepoRoot(explicit) {
  if (isDir(explicit)) return path.resolve(explicit);
  const fromEnv = envDir(REPO_ENV_ALIASES);
  if (fromEnv) return fromEnv;
  return path.resolve(explicit || '/workspace/repo');
}

function resolveBaselineRoot(explicit) {
  if (isDir(explicit)) return path.resolve(explicit);
  return envDir(BASELINE_ENV_ALIASES);
}

function resolveNodeModules(root) {
  const local = path.join(root, 'node_modules');
  if (isDir(local)) return local;
  const candidates = [
    process.env.MIGRATION_NODE_MODULES,
    '/opt/migration/dependencies/node_modules',
  ];
  for (let i = 0; i < candidates.length; i += 1) {
    if (isDir(candidates[i])) return candidates[i];
  }
  return null;
}

// Resolve a promise against a deadline. Returns TIMEOUT (never throws) when the
// deadline elapses; propagates a rejection from the source promise.
function withTimeout(promise, ms, label) {
  let timer = null;
  const deadline = new Promise((resolve) => {
    timer = setTimeout(() => resolve(TIMEOUT), Math.max(1, ms));
  });
  return Promise.resolve()
    .then(() => Promise.race([Promise.resolve(promise), deadline]))
    .then(
      (value) => {
        clearTimeout(timer);
        return value;
      },
      (err) => {
        clearTimeout(timer);
        throw err;
      },
    );
}

// HTTP request with a hard socket timeout. Always resolves (never rejects) so a
// single hanging route cannot stall the whole runner.
function request(options) {
  const opts = options || {};
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    let req;
    try {
      req = http.request(
        {
          host: '127.0.0.1',
          port: opts.port,
          method: opts.method,
          path: opts.path,
          headers: opts.headers || {},
        },
        (res) => {
          const chunks = [];
          res.on('data', (chunk) => chunks.push(chunk));
          res.on('end', () => finish({
            statusCode: res.statusCode,
            headers: res.headers,
            body: Buffer.concat(chunks),
          }));
          res.on('error', (err) => finish({
            statusCode: null, headers: {}, body: Buffer.alloc(0), error: err.message,
          }));
        },
      );
    } catch (err) {
      finish({ statusCode: null, headers: {}, body: Buffer.alloc(0), error: err.message });
      return;
    }
    req.on('error', (err) => finish({
      statusCode: null, headers: {}, body: Buffer.alloc(0), error: err.message,
    }));
    req.setTimeout(opts.timeoutMs || 8000, () => {
      req.destroy();
      finish({ statusCode: null, headers: {}, body: Buffer.alloc(0), timedOut: true });
    });
    if (opts.body) req.write(opts.body);
    req.end();
  });
}

// Multipart body builder (video field) used by the upload differential cases.
function multipartBody(field, filename, content, contentType) {
  const boundary = '----integration' + Date.now().toString(16) + Math.floor(Math.random() * 1e9).toString(16);
  const head = Buffer.from(
    '--' + boundary + '\r\n'
    + 'Content-Disposition: form-data; name="' + field + '"; filename="' + filename + '"\r\n'
    + 'Content-Type: ' + (contentType || 'application/octet-stream') + '\r\n\r\n',
  );
  const tail = Buffer.from('\r\n--' + boundary + '--\r\n');
  return { boundary: boundary, body: Buffer.concat([head, Buffer.from(content), tail]) };
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

module.exports = {
  ASSET_ROOT,
  TIMEOUT,
  REPO_ENV_ALIASES,
  BASELINE_ENV_ALIASES,
  assetPath,
  batchPath,
  resolveRepoRoot,
  resolveBaselineRoot,
  resolveNodeModules,
  withTimeout,
  request,
  multipartBody,
  readJson,
};
