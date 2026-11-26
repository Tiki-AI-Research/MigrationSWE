'use strict';

// Dependency-stub preload for servers that the INTEGRATION runner spawns as a
// child process (see collect.js `collectDIFF2`).
//
// BATCH-DIFFERENTIAL-0002 boots `server.js` with `child_process.spawn(...)` so
// its probes observe a real, listening HTTP server. The authored reference
// tree does not ship a pre-rendered Next.js build nor a live MongoDB, so the
// integration layer injects this module through `NODE_OPTIONS=--require <this>`
// into the child. It installs the same dependency stubs the in-process boot
// (`./boot`) uses, without ever editing a dependency batch file.
//
// The stub is loaded before the entry module, so patching `Module._load` here
// intercepts every require `server.js` performs.

const Module = require('module');
const { EventEmitter } = require('events');

const realLoad = Module._load;

function htmlResponse(res) {
  res.statusCode = 200;
  res.setHeader('Content-Type', 'text/html');
  res.end('<!doctype html><html><body>next-page</body></html>');
}

const fakeNext = () => ({
  prepare: () => Promise.resolve(),
  render: (req, res) => htmlResponse(res),
  getRequestHandler: () => (req, res) => htmlResponse(res),
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
  getCounterHistoryOfRecording: () => Promise.resolve({}),
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

const fakeFs = {
  getFilesDirectoryPath: () => require('os').tmpdir(),
  getFiles: () => Promise.resolve([]),
};

class FakeYolo extends EventEmitter {
  constructor(cfg) {
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
