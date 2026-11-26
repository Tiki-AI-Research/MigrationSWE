import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { test } from 'node:test';
import { preparePublicationPause } from '../evaluator/publication-pause.mjs';

async function waitFor(predicate) {
  const deadline = Date.now() + 10000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, 'publisher did not reach the pause');
    await delay(10);
  }
}

test('publication fixtures do not count as abandoned build staging', () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-pause-residue-'));
  const pause = preparePublicationPause(base, 'build-kill');
  const residue = () => fs.readdirSync(base).filter(name => /^\.?build[-.]/.test(name));
  try {
    pause.arm();
    assert.deepEqual(residue(), []);
    pause.release();
    assert.deepEqual(residue(), []);
    // Keep the real staging check strict, including after an interrupted run.
    fs.mkdirSync(path.join(base, '.build-staging-dead'));
    fs.mkdirSync(path.join(base, 'build.abandoned'));
    assert.deepEqual(residue().sort(), ['.build-staging-dead', 'build.abandoned']);
  } finally {
    pause.release();
    fs.rmSync(base, { recursive: true, force: true });
  }
});

for (const api of ['sync', 'callback', 'promise', 'named-import']) {
  test(`SIGKILL pauses before publication through ${api}`, async () => {
    const base = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-pause-'));
    let child;
    const pause = preparePublicationPause(base, 'kill');
    try {
      fs.mkdirSync(path.join(base, 'build'));
      fs.writeFileSync(path.join(base, 'build/artifact.js'), 'previous');
      const operations = {
        sync: "fs.renameSync('build', '.build-stage/previous'); fs.renameSync('.build-stage/generation', 'build');",
        callback: "await new Promise((resolve, reject) => fs.rename('build', '.build-stage/previous', error => error ? reject(error) : resolve())); await fs.promises.rename('.build-stage/generation', 'build');",
        promise: "await fs.promises.rename('build', '.build-stage/previous'); await fs.promises.rename('.build-stage/generation', 'build');",
        'named-import': "renameSync('build', '.build-stage/previous'); renameSync('.build-stage/generation', 'build');",
      };
      fs.writeFileSync(path.join(base, 'publisher.mjs'), `
import fs, { renameSync } from 'node:fs';
fs.mkdirSync('.build-stage/generation', { recursive: true });
fs.writeFileSync('.build-stage/generation/artifact.js', 'next');
${operations[api]}
`);
      pause.arm();
      child = spawn(process.execPath, ['publisher.mjs'], {
        cwd: base, env: { ...process.env, ...pause.env }, stdio: 'ignore',
      });
      const exited = once(child, 'exit');
      await waitFor(() => pause.pausedPid());
      // Delay deliberately: the original staging poll allowed a complete
      // swap to win this race. The hook must keep the old generation intact.
      await delay(75);
      assert.equal(pause.pausedPid(), child.pid);
      assert.equal(fs.readFileSync(path.join(base, 'build/artifact.js'), 'utf8'), 'previous');
      child.kill('SIGKILL');
      const [, signal] = await exited;
      assert.equal(signal, 'SIGKILL');
      assert.equal(fs.readFileSync(path.join(base, 'build/artifact.js'), 'utf8'), 'previous');
      assert.ok(fs.existsSync(path.join(base, '.build-stage/generation/artifact.js')));
    } finally {
      child?.kill('SIGKILL');
      pause.release();
      fs.rmSync(base, { recursive: true, force: true });
    }
  });
}

test('a disarmed hook permits publication and post-commit staging cleanup', async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-pause-spaces '));
  const pause = preparePublicationPause(base, 'idle');
  try {
    fs.mkdirSync(path.join(base, 'build'));
    fs.writeFileSync(path.join(base, 'build/artifact.js'), 'previous');
    fs.writeFileSync(path.join(base, 'publisher.mjs'), `
import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
fs.mkdirSync('.build-stage/generation', { recursive: true });
fs.writeFileSync('.build-stage/generation/artifact.js', 'next');
fs.renameSync('build', '.build-stage/previous');
fs.renameSync('.build-stage/generation', 'build');
await delay(200);
fs.rmSync('.build-stage', { recursive: true });
`);
    const child = spawn(process.execPath, ['publisher.mjs'], {
      cwd: base, env: { ...process.env, ...pause.env }, stdio: 'ignore',
    });
    const exited = once(child, 'exit');
    await waitFor(() => {
      try {
        return fs.readFileSync(path.join(base, 'build/artifact.js'), 'utf8') === 'next';
      } catch (error) {
        if (error.code === 'ENOENT') return false;
        throw error;
      }
    });
    assert.ok(fs.existsSync(path.join(base, '.build-stage')), 'staging can remain after commit');
    assert.equal(pause.pausedPid(), null);
    const [code] = await exited;
    assert.equal(code, 0);
    assert.equal(fs.existsSync(path.join(base, '.build-stage')), false);
  } finally {
    pause.release();
    fs.rmSync(base, { recursive: true, force: true });
  }
});
