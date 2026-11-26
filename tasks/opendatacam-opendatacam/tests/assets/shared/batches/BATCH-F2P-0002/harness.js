'use strict';

// Shared probe harness for BATCH-F2P-0002 (Express 4.17.1 -> Fastify 4.28.1).
//
// Node-core only. The harness exposes:
//   * HTTP request helpers used by the dynamic Fastify probes,
//   * a very small static scanner over `server.js`,
//   * a repository-scope differ used for the dependency/change-scope rules,
//   * a dependency-stubbed server boot used by the target-side probe.
//
// The target probe MUST boot the real /input/target `server.js` through
// `bootServer` (only `next`, the Mongo manager and the filesystem manager are
// stubbed) before it may report PASS for a target postcondition.

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const Module = require('module');

const SERVER_MODULE = 'server.js';
const ALLOWED_CHANGED = ['server.js', 'package.json', 'package-lock.json'];
// Documentation / non-behavioural files are not scope violations.
const HARMLESS_CHANGED = /(?:^|\/)(?:readme|licen[cs]e|changelog|changes|contributing|authors|notice|code_of_conduct|security)(?:\.[^/]*)?$/i;
const HARMLESS_PATH = /(?:^|\/)(?:docs?|\.github|\.vscode)\//i;
const HARMLESS_EXT = /\.(?:md|markdown|txt|rst|adoc|png|jpe?g|gif|svg|ico|webp|pdf)$/i;

function isHarmlessChange(file) {
  return HARMLESS_CHANGED.test(file) || HARMLESS_PATH.test(file) || HARMLESS_EXT.test(file);
}

function readText(file) {
  return fs.readFileSync(file, 'utf8');
}

function readJson(file) {
  return JSON.parse(readText(file));
}

function httpRequest(options) {
  const opts = Object.assign({ host: '127.0.0.1' }, options);
  return new Promise((resolve, reject) => {
    const req = http.request(opts, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({
        statusCode: res.statusCode,
        headers: res.headers,
        body: Buffer.concat(chunks),
        text: Buffer.concat(chunks).toString('utf8'),
      }));
    });
    req.on('error', reject);
    if (opts.body) req.write(opts.body);
    req.end();
  });
}

function jsonRequest(port, method, routePath, payload) {
  const body = payload === undefined ? null : Buffer.from(JSON.stringify(payload));
  return httpRequest({
    port,
    method,
    path: routePath,
    headers: body
      ? { 'Content-Type': 'application/json', 'Content-Length': body.length }
      : {},
    body,
  });
}

function jsonRequestRaw(port, method, routePath, raw) {
  const body = Buffer.from(raw);
  return httpRequest({
    port,
    method,
    path: routePath,
    headers: { 'Content-Type': 'application/json', 'Content-Length': body.length },
    body,
  });
}

function multipartBody(field, filename, content, contentType) {
  const boundary = `----f2p${Date.now()}${Math.floor(Math.random() * 1e6)}`;
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

function collectSse(port, routePath, options) {
  const opts = options || {};
  const maxEvents = opts.maxEvents || 1;
  const timeoutMs = opts.timeoutMs || 5000;
  return new Promise((resolve, reject) => {
    const events = [];
    const req = http.request({ host: '127.0.0.1', port, method: 'GET', path: routePath }, (res) => {
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

// ---------------------------------------------------------------------------
// Static scanner over server.js
// ---------------------------------------------------------------------------

const EXPRESS_REQUIRES = [
  'express', 'serve-static', 'body-parser', 'csv-express', 'server-sent-events', 'multer',
];

function requirePattern(name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`require\\(\\s*['"]${escaped}['"]\\s*\\)`);
}

function analyzeServerJs(root) {
  const file = path.join(root, SERVER_MODULE);
  const raw = readText(file);
  const text = raw
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/[^\n]*/g, '');

  const routes = {};
  // The instance variable may be named anything (fastify, server, app, …); the
  // framework is identified from the module it was required from, not the
  // binding name.
  const routeRe = /([A-Za-z_$][\w$]*)\.(get|post|delete|put|patch)\(\s*['"]([/*][^'"]*)['"]/g;
  let m = routeRe.exec(text);
  while (m) {
    routes[`${m[2].toUpperCase()} ${m[3]}`] = m.index;
    m = routeRe.exec(text);
  }

  const expressRoutes = {};
  const expressRe = /express\.(get|post|delete|put|patch|use)\(\s*['"]([^'"]+)['"]/g;
  m = expressRe.exec(text);
  while (m) {
    expressRoutes[`${m[1].toUpperCase()} ${m[2]}`] = m.index;
    m = expressRe.exec(text);
  }

  const requires = {};
  for (const name of EXPRESS_REQUIRES) requires[name] = requirePattern(name).test(text);
  requires.fastify = requirePattern('fastify').test(text);
  requires['@fastify/static'] = requirePattern('@fastify/static').test(text);
  requires['@fastify/multipart'] = requirePattern('@fastify/multipart').test(text);

  return {
    text,
    routes,
    expressRoutes,
    requires,
    constructsFastify: /require\(\s*['"]fastify['"]\s*\)\s*\(/.test(text)
      || /fastify\s*\(/.test(text),
    registersStatic: /\.register\([^)\n]*(?:[Ss]tatic)/.test(text),
    registersMultipart: /\.register\([^)\n]*(?:[Mm]ultipart)/.test(text),
    delegation: /child_process|\.fork\(|\bspawn\(|child_process\.exec\(|\bexecSync\(|\bexecFile\(|require\(\s*['"]vm['"]\s*\)|_load\(/.test(text),
    fastifyOwns:
      /require\(\s*['"]fastify['"]\s*\)/.test(text)
      && Object.keys(routes).length > 0
      && Object.keys(expressRoutes).length === 0,
  };
}

// ---------------------------------------------------------------------------
// Repository scope differ
// ---------------------------------------------------------------------------

function listFiles(root, prefix, out) {
  const acc = out || [];
  const base = prefix ? path.join(root, prefix) : root;
  for (const entry of fs.readdirSync(base, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === '.build') continue;
    if (entry.isDirectory()) listFiles(root, rel, acc);
    else acc.push(rel);
  }
  return acc;
}

function hashFile(file) {
  const crypto = require('crypto');
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function diffScope(sourceRoot, targetRoot) {
  const sourceFiles = new Set(listFiles(sourceRoot));
  const targetFiles = new Set(listFiles(targetRoot));
  const changed = [];
  const added = [];
  const removed = [];
  for (const f of targetFiles) {
    if (!sourceFiles.has(f)) added.push(f);
    else if (hashFile(path.join(sourceRoot, f)) !== hashFile(path.join(targetRoot, f))) changed.push(f);
  }
  for (const f of sourceFiles) if (!targetFiles.has(f)) removed.push(f);
  const unexpected = [...changed, ...added, ...removed].filter(
    (f) => !ALLOWED_CHANGED.includes(f) && !isHarmlessChange(f),
  );
  return { changed, added, removed, unexpected };
}

// ---------------------------------------------------------------------------
// Dependency-stubbed server boot (target side only)
// ---------------------------------------------------------------------------

function copyTree(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === '.build') continue;
    const s = path.join(src, entry.name);
    const d = path.join(dst, entry.name);
    if (entry.isDirectory()) copyTree(s, d);
    else fs.copyFileSync(s, d);
  }
}

function waitForPort(port, deadlineMs) {
  const deadline = Date.now() + deadlineMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const socket = require('net').connect({ host: '127.0.0.1', port }, () => {
        socket.destroy();
        resolve();
      });
      socket.on('error', () => {
        socket.destroy();
        if (Date.now() > deadline) reject(new Error(`port ${port} not open`));
        else setTimeout(attempt, 100);
      });
    };
    attempt();
  });
}

async function bootServer(root, options) {
  const opts = options || {};
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'f2p-boot-'));
  const work = path.join(tmp, 'repo');
  copyTree(root, work);

  const modules = opts.nodeModules || path.join(root, 'node_modules');
  if (fs.existsSync(modules)) {
    fs.symlinkSync(modules, path.join(work, 'node_modules'));
  } else {
    throw new Error(`node_modules not found at ${modules}`);
  }

  const configPath = path.join(work, 'config.json');
  const config = readJson(configPath);
  config.VIDEO_INPUT = 'simulation';
  config.NEURAL_NETWORK = 'yolov4';
  config.DATABASE = 'mongo';
  config.PATH_TO_YOLO_DARKNET = '.';
  config.CMD_TO_YOLO_DARKNET = 'node scripts/YoloSimulation.js';
  config.VIDEO_UPLOAD_FOLDER = path.join(tmp, 'uploads');
  fs.mkdirSync(config.VIDEO_UPLOAD_FOLDER, { recursive: true });
  fs.writeFileSync(configPath, JSON.stringify(config, null, 2));

  const apidocRoot = path.join(work, '.build', 'apidoc');
  fs.mkdirSync(apidocRoot, { recursive: true });
  fs.writeFileSync(path.join(apidocRoot, 'index.html'), '<!doctype html><html><body>apidoc-index</body></html>');

  const port = opts.port || (18000 + Math.floor(Math.random() * 2000));
  const nextLog = [];
  const dbControl = { mode: 'empty', data: {} };

  const fakeNext = () => ({
    prepare: () => Promise.resolve(),
    getRequestHandler: () => (req, res) => {
      nextLog.push({
        url: req.url,
        hasGet: typeof req.get === 'function',
        protocol: req.protocol,
      });
      res.statusCode = 200;
      res.setHeader('Content-Type', 'text/html');
      res.end('<!doctype html><html><body>next-page</body></html>');
    },
  });

  const fakeDb = {
    connect: () => Promise.resolve(),
    getRecordings: () => Promise.resolve([]),
    getRecordingsCount: () => Promise.resolve(0),
    getTrackerHistoryOfRecording: () => Promise.resolve({}),
    getRecording: () => Promise.resolve({}),
    deleteRecording: () => Promise.resolve(),
    insertRecording: () => Promise.resolve({}),
    getAppSettings: () => Promise.resolve({}),
    persistAppSettings: () => Promise.resolve(),
    updateRecordingWithNewframe: () => Promise.resolve(),
    getCounterHistoryOfRecording: () => {
      if (dbControl.mode === 'reject') return Promise.reject(new Error('db down'));
      return Promise.resolve(dbControl.data);
    },
  };

  const fakeFs = {
    getFilesDirectoryPath: () => config.VIDEO_UPLOAD_FOLDER,
    getFiles: () => Promise.resolve([]),
  };

  const fakeOpendatacam = {
    recordingStatus: { filename: '' },
    setConfig() {},
    setTracker() {},
    setDatabase() {},
    listenToYOLO() {},
    registerCountingAreas() {},
    getCountingAreas() { return {}; },
    addStreamClient() {},
    startRecording() {},
    requestFileRecording() {},
    stopRecording() {},
    sendUpdateToClients() {},
    setUISettings() {},
    getUISettings() { return {}; },
    getStatus() {
      return {
        counterSummary: {},
        trackerSummary: {},
        videoResolution: null,
        appState: {},
      };
    },
    isRecording() { return false; },
    clean() {},
    reset() {},
  };

  const { EventEmitter } = require('events');
  class FakeYolo extends EventEmitter {    constructor(cfg) {
      super();
      this.config = cfg;
      this.videoResolution = { w: 1280, h: 720 };
      this.started = false;
    }

    start() { this.started = true; return Promise.resolve(); }

    stop() { this.started = false; return Promise.resolve(); }

    restart() { return Promise.resolve(); }

    isLive() { return true; }

    isRunning() { return this.started; }

    getVideoResolution() { return this.videoResolution; }

    getVideoParams() { return this.config && this.config.videoParams; }

    getStatus() { return 'started'; }
  }

  const realLoad = Module._load;
  Module._load = function load(request, parent, isMain) {
    if (request === 'next') return fakeNext;
    if (request === './server/Opendatacam') return fakeOpendatacam;
    if (request === './server/db/MongoDbManager') {
      return { MongoDbManager: function MongoDbManager() { return fakeDb; } };
    }
    if (request === './server/fs/FileSystemManager') return fakeFs;
    if (request === './server/processes/YoloDarknet') return { YoloDarknet: FakeYolo };
    return realLoad.call(this, request, parent, isMain);
  };

  const previousCwd = process.cwd();
  process.chdir(work);
  process.env.PORT = String(port);
  process.env.NODE_ENV = 'production';

  require(path.join(work, SERVER_MODULE));
  await waitForPort(port, 20000);

  return {
    port,
    work,
    uploadDir: config.VIDEO_UPLOAD_FOLDER,
    apidocRoot,
    nextLog,
    dbControl,
    restore() {
      Module._load = realLoad;
      process.chdir(previousCwd);
    },
  };
}

module.exports = {
  SERVER_MODULE,
  ALLOWED_CHANGED,
  readText,
  readJson,
  httpRequest,
  jsonRequest,
  jsonRequestRaw,
  multipartBody,
  collectSse,
  analyzeServerJs,
  listFiles,
  diffScope,
  bootServer,
};
