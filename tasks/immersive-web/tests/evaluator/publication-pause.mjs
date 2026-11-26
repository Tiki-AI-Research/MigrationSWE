import fs from 'node:fs';
import path from 'node:path';

// A staging directory can outlive a successful swap. Stop at the filesystem
// operation instead, so a SIGKILL probe cannot mistake post-commit cleanup for
// an unfinished build. NODE_OPTIONS carries the hook into build subprocesses.
export function preparePublicationPause(baseDir, label) {
  // Evaluator fixtures must not match the build-staging namespace: the
  // interrupted publisher is responsible only for its own staging residue.
  const prefix = path.join(baseDir, `.publication-pause-${label}`);
  const hook = `${prefix}.cjs`;
  const armed = `${prefix}.armed`;
  const reached = `${prefix}.json`;
  fs.writeFileSync(hook, `
const fs = require('node:fs');
const path = require('node:path');
const { fileURLToPath } = require('node:url');
const { syncBuiltinESMExports } = require('node:module');
const live = ${JSON.stringify(path.join(baseDir, 'build'))};
const armed = ${JSON.stringify(armed)};
const reached = ${JSON.stringify(reached)};
const write = fs.writeFileSync.bind(fs);
const absolute = value => {
  if (value instanceof URL) return fileURLToPath(value);
  return typeof value === 'string' || Buffer.isBuffer(value) ? path.resolve(String(value)) : null;
};
const inside = value => value !== null && (value === live || value.startsWith(live + path.sep));
function before(name, args) {
  if (!fs.existsSync(armed)) return;
  const from = absolute(args[0]);
  const to = absolute(args[1]);
  const destination = ['copyFile', 'cp', 'link', 'symlink'].includes(name) ? to : from;
  const mutation = name === 'rename' ? inside(from) || inside(to) : inside(destination);
  if (!mutation) return;
  if (name === 'mkdir' && fs.existsSync(destination)) return;
  if (['rm', 'rmdir', 'unlink'].includes(name) && !fs.existsSync(destination)) return;
  write(reached, JSON.stringify({ pid: process.pid, operation: name }));
  const deadline = Date.now() + 120000;
  while (fs.existsSync(armed)) {
    if (Date.now() > deadline) throw new Error('publication pause was not released');
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
  }
}
for (const name of ['rename', 'rm', 'rmdir', 'unlink', 'writeFile', 'appendFile', 'copyFile', 'cp', 'mkdir', 'link', 'symlink']) {
  for (const [owner, key] of [[fs, name + 'Sync'], [fs, name], [fs.promises, name]]) {
    const original = owner[key];
    if (original) owner[key] = function (...args) {
      before(name, args);
      return original.apply(this, args);
    };
  }
}
syncBuiltinESMExports();
`);
  return {
    env: { NODE_OPTIONS: `--require ${JSON.stringify(hook)}` },
    arm() {
      fs.rmSync(reached, { force: true });
      fs.writeFileSync(armed, '');
    },
    pausedPid() {
      try {
        return JSON.parse(fs.readFileSync(reached, 'utf8')).pid;
      } catch {
        return null;
      }
    },
    release() {
      fs.rmSync(armed, { force: true });
      fs.rmSync(reached, { force: true });
    },
  };
}
