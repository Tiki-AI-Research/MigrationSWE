import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export class ProbeAssertionError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ProbeAssertionError';
  }
}

export class ProbeInfraError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ProbeInfraError';
  }
}

export function assert(condition, message) {
  if (!condition) throw new ProbeAssertionError(message);
}

export function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new ProbeAssertionError(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

export function assertMatch(value, pattern, message) {
  if (!pattern.test(String(value))) {
    throw new ProbeAssertionError(`${message}: ${JSON.stringify(String(value).slice(0, 600))} did not match ${pattern}`);
  }
}

export function assertNotMatch(value, pattern, message) {
  if (pattern.test(String(value))) {
    throw new ProbeAssertionError(`${message}: ${JSON.stringify(String(value).slice(0, 600))} unexpectedly matched ${pattern}`);
  }
}

export function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    throw new ProbeInfraError(`cannot read JSON ${file}: ${error.message}`);
  }
}

export function makeContext(repoRoot, fixtureRoot) {
  const resolved = path.resolve(repoRoot);
  const cliPath = path.join(resolved, 'dist', 'cli.js');
  const binPath = path.join(resolved, 'bin', 'ft.mjs');
  if (!fs.existsSync(path.join(resolved, 'package.json'))) {
    throw new ProbeInfraError(`repository root ${resolved} has no package.json`);
  }
  if (!fs.existsSync(cliPath)) {
    throw new ProbeInfraError(`built CLI missing at ${cliPath}; run npm run build first`);
  }
  if (!fs.existsSync(binPath)) {
    throw new ProbeInfraError(`bin entry missing at ${binPath}`);
  }
  return {
    repoRoot: resolved,
    fixtureRoot: path.resolve(fixtureRoot),
    cliPath,
    binPath,
    pkg: readJson(path.join(resolved, 'package.json')),
    pkgPath: path.join(resolved, 'package.json'),
    lockPath: path.join(resolved, 'package-lock.json'),
  };
}

export function makeSandbox() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-f2p-probe-'));
  const data = path.join(home, 'data');
  fs.mkdirSync(data, { recursive: true });
  return {
    home,
    data,
    library: path.join(home, 'library'),
    commands: path.join(home, 'commands'),
  };
}

export function cleanupSandbox(box) {
  if (!box?.home) return;
  fs.rmSync(box.home, { recursive: true, force: true });
}

export function withSandbox(fn) {
  const box = makeSandbox();
  try {
    return fn(box);
  } finally {
    cleanupSandbox(box);
  }
}

export function runNode(entry, args, options = {}) {
  const box = options.box ?? makeSandbox();
  const ownsBox = !options.box;
  const env = {
    ...process.env,
    HOME: box.home,
    FT_DATA_DIR: box.data,
    FT_LIBRARY_DIR: box.library,
    FT_COMMANDS_DIR: box.commands,
    NO_COLOR: '1',
    ...(options.env ?? {}),
  };
  const result = spawnSync(process.execPath, [entry, ...args], {
    cwd: options.cwd ?? box.home,
    encoding: 'utf8',
    env,
    input: options.input,
    timeout: options.timeout ?? 60_000,
  });
  try {
    if (result.error) {
      throw new ProbeInfraError(`failed to execute ${entry} ${args.join(' ')}: ${result.error.message}`);
    }
    if (result.signal) {
      throw new ProbeInfraError(`${entry} ${args.join(' ')} terminated by signal ${result.signal}`);
    }
    return {
      status: result.status,
      stdout: result.stdout ?? '',
      stderr: result.stderr ?? '',
      args,
      box,
    };
  } finally {
    if (ownsBox) cleanupSandbox(box);
  }
}

export function runCli(ctx, args, options = {}) {
  return runNode(ctx.cliPath, args, options);
}

export function runBin(ctx, args, options = {}) {
  return runNode(ctx.binPath, args, options);
}

export function runNpm(ctx, args, options = {}) {
  const result = spawnSync('npm', args, {
    cwd: ctx.repoRoot,
    encoding: 'utf8',
    env: { ...process.env, NO_COLOR: '1', ...(options.env ?? {}) },
    timeout: options.timeout ?? 120_000,
  });
  if (result.error) {
    throw new ProbeInfraError(`failed to execute npm ${args.join(' ')}: ${result.error.message}`);
  }
  return {
    status: result.status,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
  };
}

export function npmLs(ctx, packageName) {
  const result = runNpm(ctx, ['ls', packageName, '--json', '--depth=0']);
  const parsed = result.stdout.trim() ? JSON.parse(result.stdout) : {};
  return {
    ...result,
    json: parsed,
    resolved: parsed?.dependencies?.[packageName] ?? null,
  };
}

export function runNamedTest(ctx, testFile, testName) {
  const tsx = path.join(ctx.repoRoot, 'node_modules', '.bin', 'tsx');
  if (!fs.existsSync(tsx)) {
    throw new ProbeInfraError(`tsx executable missing at ${tsx}; install repository dependencies first`);
  }
  const result = spawnSync(tsx, ['--test', '--test-name-pattern', testName, testFile], {
    cwd: ctx.repoRoot,
    encoding: 'utf8',
    env: { ...process.env, NO_COLOR: '1' },
    timeout: 120_000,
  });
  if (result.error) {
    throw new ProbeInfraError(`failed to run ${testFile}: ${result.error.message}`);
  }
  return {
    status: result.status,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    combined: `${result.stdout ?? ''}\n${result.stderr ?? ''}`,
  };
}

export function expectNamedTest(ctx, testFile, testName) {
  const result = runNamedTest(ctx, testFile, testName);
  assertEqual(result.status, 0, `${testFile} "${testName}" should pass`);
  assertMatch(result.combined, /# fail 0/, `${testFile} "${testName}" reported failures`);
  return result;
}

export function expectSuccess(result, label) {
  assertEqual(result.status, 0, `${label} should exit 0 (stderr: ${result.stderr.slice(0, 400)})`);
}

export function expectFailure(result, label) {
  assert(result.status !== 0, `${label} should exit nonzero`);
}

export function exactVersion(ctx) {
  return String(ctx.pkg.version);
}

export function expectVersionFlags(ctx) {
  const version = exactVersion(ctx);
  for (const flag of ['--version', '-V']) {
    const result = runCli(ctx, [flag]);
    expectSuccess(result, `ft ${flag}`);
    assertEqual(result.stdout.trim(), version, `ft ${flag} must print exactly the package version`);
  }
}

export function expectOclifUsage(result, label) {
  assertMatch(result.stderr, /(^|\n)USAGE(\n|$)/, `${label} must include oclif USAGE help on stderr`);
}

export function expectMissingRequiredArg(ctx) {
  const result = runCli(ctx, ['show']);
  expectFailure(result, 'ft show');
  assertMatch(result.stderr, /Missing 1 required arg/, 'ft show must fail through oclif required-argument validation');
  expectOclifUsage(result, 'ft show');
}

export function expectNonexistentFlag(ctx) {
  const result = runCli(ctx, ['sync', '--bogus']);
  expectFailure(result, 'ft sync --bogus');
  assertMatch(result.stderr, /(?:nonexistent|unknown|unexpected)[^\n]{0,20}(?:flag|option)[^\n]{0,20}--bogus/i, 'ft sync --bogus must be rejected as an unknown flag');
  expectOclifUsage(result, 'ft sync --bogus');
}

export function expectRequiredArtifactOption(ctx) {
  const result = runCli(ctx, ['seeds', 'create']);
  expectFailure(result, 'ft seeds create');
  assertMatch(result.stderr, /required option '--artifact <id\.\.\.>' not specified/, 'ft seeds create must translate the oclif required-flag error');
  expectOclifUsage(result, 'ft seeds create');
}

export function expectShortHelp(ctx) {
  const result = runCli(ctx, ['-h']);
  expectSuccess(result, 'ft -h');
  assertMatch(result.stdout, /(^|\n)USAGE(\n|$)/, 'ft -h must render oclif help');
}

export function ensureFixture(ctx, name, box) {
  const source = path.join(ctx.fixtureRoot, name);
  if (!fs.existsSync(source)) {
    throw new ProbeInfraError(`fixture missing at ${source}`);
  }
  const target = path.join(box.data, name);
  fs.copyFileSync(source, target);
  return target;
}
