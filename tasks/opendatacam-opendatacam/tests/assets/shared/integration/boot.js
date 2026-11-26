'use strict';

// In-process, dependency-stubbed boot of the repository's `server.js`.
//
// The INTEGRATION runner uses this to execute the P2P and DIFFERENTIAL-0001
// probes on both the source (Express) and target (Fastify) trees. It is
// self-contained on purpose: the previous delegation to a dependency batch's
// booter did not expose the `next.render` entry point used by `GET /`, nor did
// it let a caller feed deterministic counter history, which made `GET /`,
// `GET /recording/:id/counter/csv` and the CSV differential cases hang.
//
// Only `next`, the OpenDataCam controller, the Mongo manager, the filesystem
// manager and the YOLO process are stubbed; every route is the repository's
// own implementation and is exercised through real HTTP requests.

const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const Module = require('module');

const { resolveNodeModules } = require('./lib');

const SERVER_MODULE = 'server.js';

function copyTree(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  const entries = fs.readdirSync(src, { withFileTypes: true });
  for (let i = 0; i < entries.length; i += 1) {
    const entry = entries[i];
    if (entry.name === 'node_modules' || entry.name === '.git'
      || entry.name === '.build' || entry.name === '.next') continue;
    const from = path.join(src, entry.name);
    const to = path.join(dst, entry.name);
    if (entry.isDirectory()) copyTree(from, to);
    else fs.copyFileSync(from, to);
  }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      const port = addr && addr.port;
      server.close(() => resolve(port));
    });
  });
}

function waitForPort(port, deadlineMs) {
  const deadline = Date.now() + deadlineMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const socket = net.connect({ host: '127.0.0.1', port }, () => {
        socket.destroy();
        resolve();
      });
      socket.on('error', () => {
        socket.destroy();
        if (Date.now() > deadline) reject(new Error('port ' + port + ' not open'));
        else setTimeout(attempt, 100);
      });
    };
    attempt();
  });
}

function htmlResponse(res) {
  res.statusCode = 200;
  res.setHeader('Content-Type', 'text/html');
  res.end('<!doctype html><html><body>next-page</body></html>');
}

async function bootServer(root, options) {
  const opts = options || {};
  const nodeModules = opts.nodeModules || resolveNodeModules(root);
  if (!nodeModules) throw new Error('no node_modules available for ' + root);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'integration-boot-'));
  const work = path.join(tmp, 'repo');
  copyTree(root, work);
  fs.symlinkSync(nodeModules, path.join(work, 'node_modules'));

  const configPath = path.join(work, 'config.json');
  const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  config.VIDEO_INPUT = 'simulation';
  config.NEURAL_NETWORK = config.NEURAL_NETWORK || 'yolov4';
  config.DATABASE = 'mongo';
  config.PATH_TO_YOLO_DARKNET = '.';
  config.CMD_TO_YOLO_DARKNET = 'node scripts/YoloSimulation.js';
  if (opts.mjpegPort) config.PORTS.darknet_mjpeg_stream = opts.mjpegPort;
  config.VIDEO_UPLOAD_FOLDER = path.join(tmp, 'uploads');
  fs.mkdirSync(config.VIDEO_UPLOAD_FOLDER, { recursive: true });
  fs.writeFileSync(configPath, JSON.stringify(config, null, 2));

  const apidocRoot = path.join(work, '.build', 'apidoc');
  fs.mkdirSync(apidocRoot, { recursive: true });
  fs.writeFileSync(
    path.join(apidocRoot, 'index.html'),
    '<!doctype html><html><body>apidoc-index</body></html>',
  );

  const port = opts.port || await freePort();
  const nextLog = [];
  const calls = [];
  const instances = [];
  const frameworkVersions = [];
  let uiSettings = {};
  let countingAreas = {};
  const dbControl = { mode: 'empty', data: {} };

  function nextResponse(req, res) {
    if (opts.audit) {
      // The unchanged Next pages call urlHelper, which needs these methods.
      const urlData = require(path.join(work, 'server/utils/urlHelper')).getURLData(req);
      calls.push(['next', urlData.protocol, typeof req.on, typeof res.writeHead, {
        method: req.method, url: req.url, body: req.body, query: req.query,
      }]);
    }
    htmlResponse(res);
  }

  const fakeNext = () => ({
    prepare: () => Promise.resolve(),
    render: (req, res) => {
      nextLog.push({ url: req.url, hasGet: typeof req.get === 'function', protocol: req.protocol });
      nextResponse(req, res);
    },
    getRequestHandler: () => (req, res) => {
      nextLog.push({ url: req.url, hasGet: typeof req.get === 'function', protocol: req.protocol });
      nextResponse(req, res);
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
    getCounterHistoryOfRecording: (id) => {
      if (opts.audit) calls.push(['counterHistory', id]);
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
    listenToYOLO() { calls.push(['listenToYOLO']); },
    registerCountingAreas(value) { countingAreas = value; calls.push(['areas', value]); },
    getCountingAreas() { return opts.audit ? countingAreas : {}; },
    addStreamClient(res) {
      if (opts.audit) res.sse('data:' + JSON.stringify({ videoResolution: { w: 1280, h: 720 },
        trackerDataForLastFrame: [], appState: {}, counterSummary: {}, trackerSummary: {} }) + '\n\n');
    },
    startRecording() { calls.push(['startRecording']); },
    requestFileRecording() { calls.push(['requestFileRecording']); },
    stopRecording() { calls.push(['stopRecording']); },
    sendUpdateToClients() {},
    setUISettings(value) { uiSettings = value; calls.push(['ui', value]); },
    getUISettings() { return opts.audit ? uiSettings : {}; },
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
  class FakeYolo extends EventEmitter {
    constructor(cfg) {
      super();
      this.config = cfg;
      this.videoResolution = { w: 1280, h: 720 };
      this.started = false;
    }

    start() { calls.push(['yoloStart']); this.started = true; return Promise.resolve(); }

    stop() { calls.push(['yoloStop']); this.started = false; return Promise.resolve(); }

    restart() { return Promise.resolve(); }

    isLive() { return true; }

    isRunning() { return this.started; }

    getVideoResolution() { return this.videoResolution; }

    getVideoParams() { return this.config && this.config.videoParams; }

    getStatus() { return 'started'; }
  }

  const realLoad = Module._load;
  Module._load = function load(request, parent, isMain) {
    if (opts.audit && request === 'fastify') {
      const factory = realLoad.call(this, request, parent, isMain);
      frameworkVersions.push(realLoad.call(this, 'fastify/package.json', parent, false).version);
      const observedFactory = function observedFactory(...args) {
        const instance = factory(...args);
        instances.push(instance);
        return instance;
      };
      return new Proxy(observedFactory, { get(target, key) {
        if (key === 'fastify' || key === 'default') return observedFactory;
        return factory[key];
      } });
    }
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

  let booted = false;
  try {
    require(path.join(work, SERVER_MODULE));
    await waitForPort(port, opts.timeoutMs || 20000);
    booted = true;
  } finally {
    if (!booted) {
      Module._load = realLoad;
      process.chdir(previousCwd);
    }
  }

  return {
    port,
    root,
    work,
    uploadDir: config.VIDEO_UPLOAD_FOLDER,
    apidocRoot,
    nextLog,
    calls,
    instances,
    frameworkVersions,
    dbControl,
    // P2P / differential probes call this before a route that reads counter
    // history so the CSV and counter surfaces answer deterministically.
    setCounterData(data) { dbControl.data = data === undefined ? {} : data; },
    restore() {
      try {
        Module._load = realLoad;
      } catch (err) { /* best effort */ }
      process.chdir(previousCwd);
    },
  };
}

module.exports = { bootServer, SERVER_MODULE };
