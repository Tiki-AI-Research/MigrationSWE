'use strict';

// Runtime module-identity probe.
//
// Loaded (via `NODE_OPTIONS=--require`) ahead of the candidate entry module, it
// records the module specifier of every module the entry actually resolves --
// including a request that *fails* because the source stack is not installed --
// and every child process the entry spawns.  A hidden source-runtime fallback
// (a wrapped/aliased require, a split name, or an opaque payload handed to
// `node -e`) is therefore observed from real behaviour instead of source text.
//
// Records are appended to `MIGRATION_MODULE_PROBE_LOG` (JSON-lines).  The probe
// never changes program behaviour: it only observes.

const fs = require('fs');
const Module = require('module');

const LOG = process.env.MIGRATION_MODULE_PROBE_LOG || '';
const MAX_RECORDS = Number(process.env.MIGRATION_MODULE_PROBE_MAX || 1000);
const records = [];
const seenRequests = new Set();
let flushed = false;

function record(entry) {
  if (records.length < MAX_RECORDS) records.push(entry);
}

const realLoad = Module._load;
Module._load = function probedLoad(request, parent, isMain) {
  try {
    const specifier = String(request);
    if (!seenRequests.has(specifier)) {
      seenRequests.add(specifier);
      record({
        type: 'require',
        request: specifier,
        parent: parent && parent.filename ? String(parent.filename) : null,
      });
    }
  } catch (err) { /* observation must never throw */ }
  return realLoad.apply(this, arguments);
};

try {
  const childProcess = require('child_process');
  ['spawn', 'exec', 'execFile', 'fork', 'spawnSync', 'execSync', 'execFileSync']
    .forEach((name) => {
      const original = childProcess[name];
      if (typeof original !== 'function') return;
      childProcess[name] = function probedChildProcess(...args) {
        try {
          const argv = args[1];
          record({
            type: 'child',
            method: name,
            command: String(args[0]),
            args: Array.isArray(argv) ? argv.map((item) => String(item)) : null,
          });
        } catch (err) { /* observation must never throw */ }
        return original.apply(this, args);
      };
    });
} catch (err) { /* child_process unavailable */ }

function flush() {
  if (flushed) return;
  flushed = true;
  if (!LOG) return;
  try {
    const body = records.map((entry) => JSON.stringify(entry)).join('\n');
    // A node child process inherits NODE_OPTIONS and therefore this probe; give
    // every process its own file so concurrent writers never interleave.
    fs.writeFileSync(LOG + '.' + process.pid, body ? body + '\n' : '');
  } catch (err) { /* best effort */ }
}

process.on('exit', flush);

// The candidate server keeps the event loop alive while it listens; stop after a
// bounded window so the probe always terminates deterministically.
const windowMs = Number(process.env.MIGRATION_MODULE_PROBE_MS || 8000);
setTimeout(() => { flush(); process.exit(0); }, windowMs).unref();
