import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { prepareInputRoundTrip } from '../evaluator/input-round-trip.mjs';

for (const immutable of [false, true]) {
  test(`round-trip probe ${immutable ? 'accepts one snapshot' : 'exposes split live reads'}`, () => {
    const base = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-roundtrip-'));
    try {
      const input = path.join(base, 'input.js');
      fs.writeFileSync(input, 'state A');
      const probe = prepareInputRoundTrip(base, input, 'state B');
      fs.writeFileSync(path.join(base, 'build.mjs'), `
import fs from 'node:fs';
const snapshot = fs.readFileSync('input.js', 'utf8');
fs.mkdirSync('.build-stage');
fs.writeFileSync('.build-stage/webvr-polyfill.js', snapshot);
const minified = ${immutable ? 'snapshot' : "fs.readFileSync('input.js', 'utf8')"};
for (const name of ['webvr-polyfill.min.js', 'webvr-polyfill.min.js.map', 'webvr-polyfill.min.mjs', 'webvr-polyfill.min.mjs.map']) {
  await fs.promises.writeFile('.build-stage/' + name, minified);
}
`);
      execFileSync(process.execPath, ['--require', probe.hook, 'build.mjs'], { cwd: base, stdio: 'inherit' });
      assert.deepEqual(probe.events(), { changed: true, restored: true });
      assert.equal(fs.readFileSync(input, 'utf8'), 'state A');
      const readable = fs.readFileSync(path.join(base, '.build-stage/webvr-polyfill.js'), 'utf8');
      const minified = fs.readFileSync(path.join(base, '.build-stage/webvr-polyfill.min.js'), 'utf8');
      assert.equal(readable === minified, immutable);
    } finally {
      fs.rmSync(base, { recursive: true, force: true });
    }
  });
}
