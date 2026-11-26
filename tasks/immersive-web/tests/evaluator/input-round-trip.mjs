import fs from 'node:fs';
import path from 'node:path';

// Change an input after a readable output is written, then restore its exact
// bytes after the minified outputs are written. Separate compilations that
// read the live tree can mix revisions while a content-only final hash passes.
export function prepareInputRoundTrip(baseDir, inputPath, transientText) {
  const hook = path.join(baseDir, '.input-round-trip.cjs');
  const trace = path.join(baseDir, '.input-round-trip.json');
  fs.writeFileSync(hook, `
const fs = require('node:fs');
const path = require('node:path');
const { fileURLToPath } = require('node:url');
const { syncBuiltinESMExports } = require('node:module');
const root = ${JSON.stringify(baseDir)};
const live = path.join(root, 'build');
const input = ${JSON.stringify(inputPath)};
const transient = ${JSON.stringify(transientText)};
const trace = ${JSON.stringify(trace)};
const read = fs.readFileSync.bind(fs);
const write = fs.writeFileSync.bind(fs);
const original = read(input);
const minified = new Set([
  'webvr-polyfill.min.js', 'webvr-polyfill.min.js.map',
  'webvr-polyfill.min.mjs', 'webvr-polyfill.min.mjs.map',
]);
let changed = false;
let restored = false;
function after(value) {
  if (!(value instanceof URL) && typeof value !== 'string' && !Buffer.isBuffer(value)) return;
  const filename = value instanceof URL ? fileURLToPath(value) : path.resolve(String(value));
  if (!filename.startsWith(root + path.sep) || filename.startsWith(live + path.sep)) return;
  const name = path.basename(filename);
  if (!changed && /^webvr-polyfill\\.(?:js|mjs|cjs)$/.test(name)) {
    write(input, transient);
    changed = true;
    write(trace, JSON.stringify({ changed, restored }));
  }
  minified.delete(name);
  if (changed && !restored && minified.size === 0) {
    write(input, original);
    restored = true;
    write(trace, JSON.stringify({ changed, restored }));
  }
}
for (const name of ['writeFile', 'copyFile']) {
  const destination = args => args[name === 'copyFile' ? 1 : 0];
  const synchronous = fs[name + 'Sync'];
  fs[name + 'Sync'] = function (...args) {
    const result = synchronous.apply(this, args);
    after(destination(args));
    return result;
  };
  const promise = fs.promises[name];
  fs.promises[name] = async function (...args) {
    const result = await promise.apply(this, args);
    after(destination(args));
    return result;
  };
  const callback = fs[name];
  fs[name] = function (...args) {
    const complete = args.pop();
    return callback.call(this, ...args, (error, ...values) => {
      if (!error) after(destination(args));
      complete(error, ...values);
    });
  };
}
syncBuiltinESMExports();
`);
  return {
    hook,
    trace,
    events() {
      return fs.existsSync(trace) ? JSON.parse(fs.readFileSync(trace, 'utf8')) : null;
    },
  };
}
