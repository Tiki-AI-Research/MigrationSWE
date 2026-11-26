// Shared F2P probe harness for BATCH-F2P-0002 (Fastify -> Express migration).
//
// Every case evaluates the same semantic predicate on both sides:
//   1. the concrete entry named by the observation is executed end-to-end, and
//   2. the code path that produced the result is proven to belong to the
//      declared target technology (Express 5.1.0) for that specific entry.
//
// The predicate is entry scoped: it never degrades into a single global
// "does express exist" check. The source side is expected to observe FAIL
// (Fastify still owns the path / the target capability is absent) and the
// target side is expected to observe PASS.

import { spawn } from 'node:child_process';
import { readFileSync, existsSync, writeFileSync, unlinkSync } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const CASES_PATH = path.resolve(
  HERE,
  '../../../shared/batches/BATCH-F2P-0002/cases.json'
);

export function loadCases() {
  return JSON.parse(readFileSync(CASES_PATH, 'utf8'));
}

export function resolveRepo(side) {
  if (process.env.REPO_ROOT) return path.resolve(process.env.REPO_ROOT);
  return side === 'source' ? '/input/source' : '/input/target';
}

function readServer(repo) {
  return readFileSync(path.join(repo, 'src', 'server.mjs'), 'utf8');
}

function request(port, reqPath, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port, path: reqPath, method: 'GET', headers },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () =>
          resolve({
            status: res.statusCode,
            headers: res.headers,
            body: Buffer.concat(chunks),
          })
        );
      }
    );
    req.on('error', reject);
    req.end();
  });
}

function upgradeProbe(port, reqPath) {
  return new Promise((resolve) => {
    const socket = net.connect(port, '127.0.0.1');
    let data = '';
    const done = (ok) => {
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(2000, () => done(false));
    socket.on('connect', () => {
      socket.write(
        `GET ${reqPath} HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\n` +
          'Connection: Upgrade\r\nUpgrade: websocket\r\n' +
          'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n'
      );
    });
    socket.on('data', (chunk) => {
      data += chunk.toString('latin1');
      if (data.includes('101') || data.toLowerCase().includes('upgrade'))
        done(!/HTTP\/1\.[01] 40\d/.test(data));
    });
    socket.on('error', () => done(false));
    socket.on('end', () => done(data.includes('101')));
  });
}

export async function bootServer(repo, { timeoutMs = 20000 } = {}) {
  const child = spawn(process.execPath, ['src/server.mjs'], {
    cwd: repo,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env },
  });
  let log = '';
  child.stdout.on('data', (d) => (log += d));
  child.stderr.on('data', (d) => (log += d));

  const port = 8080;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null)
      return { ok: false, child, port, log, reason: 'server-exited' };
    try {
      await request(port, '/');
      return { ok: true, child, port, log };
    } catch {
      await new Promise((r) => setTimeout(r, 250));
    }
  }
  return { ok: false, child, port, log, reason: 'boot-timeout' };
}

export function stopServer(handle) {
  try {
    handle.child.kill('SIGKILL');
  } catch {
    /* ignore */
  }
}

// ---------------------------------------------------------------------------
// Entry-scoped semantic predicates.
// ---------------------------------------------------------------------------

const has = (src, marker) => src.includes(marker);

const frameworkIsExpress = (src) =>
  has(src, "from 'express'") && !has(src, "from 'fastify'");

export const CHECKS = {
  async express_router_serves_page({ repo, port, src }) {
    const res = await request(port, '/browsing');
    const ok =
      res.status === 200 &&
      /text\/html/.test(res.headers['content-type'] || '') &&
      frameworkIsExpress(src) &&
      has(src, 'app.get(');
    return { pass: ok, detail: { status: res.status, express: frameworkIsExpress(src) } };
  },

  async no_x_powered_by({ port, src }) {
    const res = await request(port, '/');
    const ok =
      res.status === 200 &&
      res.headers['x-powered-by'] === undefined &&
      has(src, "app.disable('x-powered-by')") &&
      frameworkIsExpress(src);
    return { pass: ok, detail: { status: res.status, xpb: res.headers['x-powered-by'] } };
  },

  async root_route_default_page({ port, src }) {
    const res = await request(port, '/');
    const ok =
      res.status === 200 &&
      res.headers['content-type'] !== undefined &&
      frameworkIsExpress(src) &&
      new RegExp('app\\.get\\(serverUrl\\.pathname').test(src);
    return { pass: ok, detail: { status: res.status } };
  },

  async static_mount_precedence({ port, src }) {
    const res = await request(port, '/assets/js/card.js');
    const pageIdx = src.indexOf("app.get(serverUrl.pathname + ':path'");
    const staticIdx = src.indexOf('express.static');
    const ok =
      res.status === 200 &&
      staticIdx !== -1 &&
      pageIdx !== -1 &&
      staticIdx > pageIdx &&
      !has(src, 'fastifyStatic');
    return { pass: ok, detail: { status: res.status, staticIdx, pageIdx } };
  },

  async helmet_security_headers({ port, src }) {
    const res = await request(port, '/');
    const ok =
      res.headers['x-content-type-options'] === 'nosniff' &&
      res.headers['content-security-policy'] === undefined &&
      res.headers['x-powered-by'] === undefined &&
      has(src, "from 'helmet'") &&
      has(src, 'helmet(') &&
      !has(src, '@fastify/helmet');
    return { pass: ok, detail: { nosniff: res.headers['x-content-type-options'] } };
  },

  async shared_http_and_upgrade_server({ port, src }) {
    const httpRes = await request(port, '/');
    const upgraded = await upgradeProbe(port, '/wisp/');
    const ok =
      httpRes.status === 200 &&
      upgraded &&
      has(src, 'createServer(app)') &&
      has(src, "server.on('upgrade'");
    return { pass: ok, detail: { status: httpRes.status, upgraded } };
  },

  async github_route_pattern_boot({ port, src }) {
    const ok = await request(port, '/github/wisp');
    const miss = await request(port, '/github/does-not-exist');
    const pass =
      [301, 302, 303, 307, 308].includes(ok.status) &&
      miss.status === 404 &&
      frameworkIsExpress(src) &&
      has(src, "app.get(serverUrl.pathname + 'github/:redirect'");
    return { pass, detail: { hit: ok.status, miss: miss.status } };
  },

  async duplicate_slash_normalization({ port, src }) {
    const res = await request(port, '///documentation//');
    const pass =
      res.status === 200 &&
      frameworkIsExpress(src) &&
      has(src, "pathname.replace(/\\/+/g, '/')");
    return { pass, detail: { status: res.status } };
  },

  async history_hide_status_override({ port, src }) {
    const res = await request(port, '/', {
      cookie: 'HistoryHide=true',
      'sec-fetch-dest': 'document',
    });
    const pass =
      res.status === 404 &&
      has(src, 'res.writeHead =') &&
      !has(src, "addHook('onSend'");
    return { pass, detail: { status: res.status } };
  },

  async content_type_fallback({ repo, port, src }) {
    const res = await request(port, '/documentation');
    const pass =
      res.status === 200 &&
      frameworkIsExpress(src) &&
      has(src, 'res.type(') &&
      has(src, 'supportedTypes.default');
    return { pass, detail: { status: res.status, type: res.headers['content-type'] } };
  },
};

export function repoSource(repo) {
  return readServer(repo);
}

export { request, upgradeProbe };
