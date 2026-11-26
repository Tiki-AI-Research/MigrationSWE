import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { assertPublishedPermissions, restrictivePublicationEnv } from '../evaluator/publication-permissions.mjs';

test('publication permissions check every directory and file, including build/ itself', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr-permissions-'));
  try {
    const build = path.join(temp, 'build');
    const nested = path.join(build, 'nested');
    const artifact = path.join(nested, 'bundle.js');
    fs.mkdirSync(nested, { recursive: true });
    fs.writeFileSync(artifact, 'published');
    for (const directory of [build, nested]) fs.chmodSync(directory, 0o755);
    fs.chmodSync(artifact, 0o644);
    assert.doesNotThrow(() => assertPublishedPermissions(build));
    for (const [filename, mode, restored] of [
      [build, 0o700, 0o755],
      [nested, 0o750, 0o755],
      [nested, 0o744, 0o755],
      [artifact, 0o600, 0o644],
      [artifact, 0o640, 0o644],
      [artifact, 0o604, 0o644],
    ]) {
      fs.chmodSync(filename, mode);
      assert.throws(() => assertPublishedPermissions(build), /not readable by all users/);
      fs.chmodSync(filename, restored);
    }
    fs.chmodSync(artifact, 0o444);
    assert.doesNotThrow(() => assertPublishedPermissions(build));
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});

test('restrictive umask exposes an uncorrected directory swap and accepts permission repair', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'webvr umask '));
  const originalUmask = process.umask();
  try {
    const env = restrictivePublicationEnv(temp, process.env);
    execFileSync(process.execPath, ['--input-type=module', '-e', `
      import fs from 'node:fs';
      fs.mkdirSync('.build-stage');
      fs.writeFileSync('.build-stage/bundle.js', 'published');
      fs.renameSync('.build-stage', 'build');
    `], { cwd: temp, env, stdio: 'inherit' });
    const build = path.join(temp, 'build');
    assert.equal(fs.statSync(build).mode & 0o777, 0o700);
    assert.throws(() => assertPublishedPermissions(build), /not readable by all users/);
    fs.chmodSync(build, 0o755);
    fs.chmodSync(path.join(build, 'bundle.js'), 0o644);
    assert.doesNotThrow(() => assertPublishedPermissions(build));
    assert.equal(process.umask(), originalUmask);
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});
