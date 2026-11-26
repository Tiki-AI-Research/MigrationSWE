#!/usr/bin/env node
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HARNESS_DIR = path.dirname(fileURLToPath(import.meta.url));
const ASSET_ROOT = path.resolve(HARNESS_DIR, '../../..');
const BATCH_ID = 'BATCH-P2P-0002';
const FIXTURE_PATH = path.join(ASSET_ROOT, 'fixtures', 'batches', BATCH_ID, 'search-bookmark.jsonl');
const OBSERVATION_IDS = [
  'P2P-040',
  'P2P-029',
  'P2P-036',
  'P2P-025',
  'P2P-026',
  'P2P-027',
  'P2P-028',
  'P2P-030',
  'P2P-031',
  'P2P-032',
  'P2P-033',
  'P2P-034',
  'P2P-035',
  'P2P-037',
  'P2P-038',
  'P2P-039',
];

function stripAnsi(value) {
  return String(value ?? '').replace(/\u001b\[[0-9;]*m/g, '');
}

function normalize(value) {
  return stripAnsi(value).replace(/\r\n/g, '\n').trim();
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'ft-p2p-'));
}

function isolatedEnv(home, extra = {}) {
  const data = path.join(home, 'data');
  fs.mkdirSync(data, { recursive: true });
  return {
    ...process.env,
    HOME: home,
    FT_DATA_DIR: data,
    FT_LIBRARY_DIR: path.join(home, 'library'),
    FT_COMMANDS_DIR: path.join(home, 'commands'),
    npm_config_audit: 'false',
    npm_config_fund: 'false',
    npm_config_update_notifier: 'false',
    ...extra,
  };
}

function tsxEnv(home, extra = {}) {
  const env = { ...process.env, HOME: home };
  delete env.FT_DATA_DIR;
  delete env.FT_LIBRARY_DIR;
  delete env.FT_COMMANDS_DIR;
  return {
    ...env,
    npm_config_audit: 'false',
    npm_config_fund: 'false',
    npm_config_update_notifier: 'false',
    ...extra,
  };
}

function spawn(command, args, options = {}) {
  return spawnSync(command, args, {
    cwd: options.cwd ?? process.cwd(),
    encoding: 'utf8',
    timeout: options.timeout ?? 180000,
    maxBuffer: 32 * 1024 * 1024,
    input: options.input,
    env: options.env ?? process.env,
  });
}

function readPackage(repo) {
  return JSON.parse(fs.readFileSync(path.join(repo, 'package.json'), 'utf8'));
}

function ensureBuild(repo) {
  const entry = path.join(repo, 'dist', 'cli.js');
  if (fs.existsSync(entry)) return;
  const tsc = path.join(repo, 'node_modules', 'typescript', 'bin', 'tsc');
  if (fs.existsSync(tsc)) {
    const result = spawn(process.execPath, [tsc, '-p', 'tsconfig.json'], {
      cwd: repo,
      timeout: 300000,
      env: isolatedEnv(tempHome()),
    });
    if (result.status === 0 && fs.existsSync(entry)) return;
  }
  const npmResult = spawn('npm', ['run', 'build'], {
    cwd: repo,
    timeout: 300000,
    env: isolatedEnv(tempHome()),
  });
  assert(npmResult.status === 0, `build failed: ${normalize(npmResult.stderr || npmResult.stdout).slice(-1600)}`);
  assert(fs.existsSync(entry), 'dist/cli.js is missing after build');
}

function tsxCommand(repo) {
  const candidates = [
    path.join(repo, 'node_modules', '.bin', 'tsx'),
    '/opt/migration/dependencies/node_modules/.bin/tsx',
  ];
  return candidates.find((candidate) => fs.existsSync(candidate)) ?? null;
}

function runCli(repo, args, options = {}) {
  const home = options.home ?? tempHome();
  const entry = options.entry ?? path.join(repo, 'dist', 'cli.js');
  if (options.seedFixture) {
    fs.mkdirSync(path.join(home, 'data'), { recursive: true });
    fs.copyFileSync(FIXTURE_PATH, path.join(home, 'data', 'bookmarks.jsonl'));
  }
  const result = spawn(process.execPath, [entry, ...args], {
    cwd: home,
    timeout: options.timeout ?? 120000,
    input: options.input,
    env: isolatedEnv(home, options.env),
  });
  return {
    ...result,
    home,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    combined: `${result.stdout ?? ''}${result.stderr ?? ''}`,
  };
}

function runTsx(repo, args, options = {}) {
  const bin = tsxCommand(repo);
  assert(bin, `tsx runner is missing under ${repo}`);
  const home = options.home ?? tempHome();
  const result = spawn(bin, args, {
    cwd: repo,
    timeout: options.timeout ?? 300000,
    input: options.input,
    env: tsxEnv(home, options.env),
  });
  return {
    ...result,
    home,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    combined: `${result.stdout ?? ''}${result.stderr ?? ''}`,
  };
}

function runRepoTestFile(repo, file, options = {}) {
  const absolute = path.join(repo, file);
  if (!fs.existsSync(absolute)) {
    return { file, present: false, passing: false, matched: false, passCount: 0, failCount: 0 };
  }
  const args = ['--test'];
  if (options.pattern) args.push('--test-name-pattern', options.pattern);
  args.push(file);
  const result = runTsx(repo, args, { home: options.home });
  const stdout = normalize(result.stdout);
  const passCount = Number(/# pass (\d+)/.exec(stdout)?.[1] ?? 0);
  const failCount = Number(/# fail (\d+)/.exec(stdout)?.[1] ?? 0);
  const names = [...stdout.matchAll(/^\s*(?:ok|not ok) \d+ - (.+)$/gm)].map((match) => match[1].trim());
  const matched = options.pattern ? names.some((name) => name.includes(options.pattern)) : passCount > 0;
  assert(
    result.status === 0,
    `${file}${options.pattern ? ` [${options.pattern}]` : ''} failed: ${normalize(result.combined).slice(-1400)}`,
  );
  assert(failCount === 0, `${file} reported ${failCount} failing tests`);
  if (options.pattern) assert(matched, `${file} did not run a test matching ${JSON.stringify(options.pattern)}`);
  if (!options.pattern) assert(passCount > 0, `${file} did not report any passing tests`);
  return { file, present: true, passing: true, matched, passCount, failCount };
}

function runModuleProbe(repo, source, options = {}) {
  const probeDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-probe-'));
  const probeFile = path.join(probeDir, 'probe.mjs');
  fs.writeFileSync(probeFile, source, 'utf8');
  const result = runTsx(repo, [probeFile, repo], { home: options.home });
  assert(
    result.status === 0,
    `module probe failed (${path.basename(probeFile)}): ${normalize(result.combined).slice(-1400)}`,
  );
  const lines = normalize(result.stdout).split('\n').filter((line) => line.trim().length > 0);
  const last = lines.at(-1);
  assert(last, 'module probe produced no output');
  return JSON.parse(last);
}

const WIZARD_PROBE = `
import { pathToFileURL } from 'node:url';
const repo = process.argv[2];
let mod;
let lastError;
for (const candidate of [repo + '/src/possible-wizard.ts', repo + '/dist/possible-wizard.js']) {
  try {
    mod = await import(pathToFileURL(candidate).href);
    break;
  } catch (error) {
    lastError = error;
  }
}
if (!mod) throw new Error('could not load possible-wizard: ' + String(lastError && lastError.message ? lastError.message : lastError));
const answers = ['/only-one'];
const prompter = {
  ask: async () => answers.shift(),
  write: () => {},
};
const result = await mod.stepPickRepos(prompter, { listRepos: () => [] });
console.log(JSON.stringify({
  scenario: 'stepPickRepos-no-saved-set',
  kind: result.kind,
  repos: result.kind === 'picked' ? result.repos : null,
}));
`;

const FRAMES_PROBE = `
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const repo = process.argv[2];
const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-frames-probe-'));
process.env.FT_DATA_DIR = dataDir;
let mod;
let lastError;
for (const candidate of [repo + '/src/frames-registry.ts', repo + '/dist/frames-registry.js']) {
  try {
    mod = await import(pathToFileURL(candidate).href);
    break;
  } catch (error) {
    lastError = error;
  }
}
if (!mod) throw new Error('could not load frames-registry: ' + String(lastError && lastError.message ? lastError.message : lastError));
const frame = mod.parseFrame({
  id: 'leverage-specificity',
  name: 'Focus Moat',
  group: 'building',
  generationPromptAddition: 'Consider focus and moat.',
  axisA: { label: 'Focus', rubricSentence: '0 sprawl, 100 laser.' },
  axisB: { label: 'Moat', rubricSentence: '0 commodity, 100 defensible.' },
  quadrantLabels: { highHigh: 'Killer app', highLow: 'Feature factory', lowHigh: 'Platform bet', lowLow: 'Me-too' },
});
let rejected = false;
let error = '';
try {
  mod.addUserFrame(frame);
} catch (errorValue) {
  rejected = true;
  error = String(errorValue && errorValue.message ? errorValue.message : errorValue);
}
console.log(JSON.stringify({
  scenario: 'addUserFrame-builtin-shadow',
  frameId: 'leverage-specificity',
  rejected,
  errorIncludesBuiltIn: /built-in/i.test(error),
}));
fs.rmSync(dataDir, { recursive: true, force: true });
`;

function hasChrome(value) {
  return /F i e l d|fieldtheory\.dev\/cli|What's new/i.test(value);
}

function writeUpdateCache(home, version) {
  const data = path.join(home, 'data');
  fs.mkdirSync(data, { recursive: true });
  fs.writeFileSync(path.join(data, '.update-check'), `${version}\n`);
}

function writeLastVersion(home, version) {
  const data = path.join(home, 'data');
  fs.mkdirSync(data, { recursive: true });
  fs.writeFileSync(path.join(data, '.last-version'), `${version}\n`);
}

export async function runHarness({ repo, side, onlyObservation }) {
  assert(repo, 'repo is required');
  assert(side === 'source' || side === 'target', `unknown side: ${side}`);
  const root = path.resolve(repo);
  const pkg = readPackage(root);
  const results = [];
  let buildReady = false;

  function ensureBuilt() {
    if (buildReady) return;
    ensureBuild(root);
    buildReady = true;
  }

  async function check(observationId, fn) {
    if (onlyObservation && onlyObservation !== observationId) return;
    try {
      const outcome = await fn();
      const value = outcome && outcome.value !== undefined ? outcome.value : { result: 'PASS' };
      results.push({
        id: observationId,
        observation_id: observationId,
        status: 'PASS',
        result: 'PASS',
        outcome: 'PASS',
        side,
        value,
        projection: value,
        evidence: (outcome && outcome.evidence) || {},
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      results.push({
        id: observationId,
        observation_id: observationId,
        status: 'FAIL',
        result: 'FAIL',
        outcome: 'FAIL',
        side,
        value: { result: 'FAIL' },
        projection: { result: 'FAIL' },
        evidence: { message },
        detail: { message },
      });
    }
  }

  await check('P2P-040', async () => {
    const pattern = 'stepPickRepos: no saved set, user enters paths';
    const home = tempHome();
    const repoTest = runRepoTestFile(root, 'tests/possible-wizard.test.ts', { pattern, home });
    assert(repoTest.present, 'tests/possible-wizard.test.ts is missing');
    assert(repoTest.matched, `tests/possible-wizard.test.ts did not run ${JSON.stringify(pattern)}`);
    const probe = runModuleProbe(root, WIZARD_PROBE, { home });
    assert(probe.kind === 'picked', `stepPickRepos returned ${probe.kind}`);
    assert(
      Array.isArray(probe.repos) && probe.repos.length === 1 && probe.repos[0] === '/only-one',
      `stepPickRepos returned ${JSON.stringify(probe.repos)}`,
    );
    return {
      value: {
        scenario: 'stepPickRepos-no-saved-set',
        kind: 'picked',
        repos: ['/only-one'],
      },
      evidence: {
        repoTest,
        probe,
        test: 'tests/possible-wizard.test.ts',
        testName: pattern,
      },
    };
  });

  await check('P2P-029', async () => {
    const pattern = 'refuses to shadow a built-in frame id';
    const home = tempHome();
    const repoTest = runRepoTestFile(root, 'tests/frames-registry.test.ts', { pattern, home });
    assert(repoTest.present, 'tests/frames-registry.test.ts is missing');
    assert(repoTest.matched, `tests/frames-registry.test.ts did not run ${JSON.stringify(pattern)}`);
    const probe = runModuleProbe(root, FRAMES_PROBE, { home });
    assert(probe.rejected === true, 'addUserFrame accepted a built-in frame id');
    assert(probe.errorIncludesBuiltIn === true, 'addUserFrame did not report the built-in collision');
    return {
      value: {
        scenario: 'addUserFrame-builtin-shadow',
        frameId: 'leverage-specificity',
        rejected: true,
        errorIncludesBuiltIn: true,
      },
      evidence: {
        repoTest,
        probe,
        test: 'tests/frames-registry.test.ts',
        testName: pattern,
      },
    };
  });

  await check('P2P-036', async () => {
    ensureBuilt();
    for (const flag of ['--version', '-V']) {
      const result = runCli(root, [flag]);
      assert(result.status === 0, `${flag} exited ${result.status}`);
      assert(normalize(result.stdout) === pkg.version, `${flag} printed ${JSON.stringify(normalize(result.stdout))}`);
    }
    for (const flag of ['--help', '-h']) {
      const result = runCli(root, [flag]);
      assert(result.status === 0, `${flag} exited ${result.status}`);
      for (const token of ['sync', 'search', 'possible']) {
        assert(result.combined.includes(token), `${flag} did not list ${token}`);
      }
    }
    return {
      value: {
        version: pkg.version,
        helpFlags: ['--help', '-h'],
        versionFlags: ['--version', '-V'],
        helpIncludes: ['sync', 'search', 'possible'],
      },
    };
  });

  await check('P2P-025', async () => {
    ensureBuilt();
    const possibleHelp = runCli(root, ['possible', '--help']);
    const ideasHelp = runCli(root, ['ideas', '--help']);
    const seedHelp = runCli(root, ['possible', 'seed', '--help']);
    const seedListHelp = runCli(root, ['possible', 'seed', 'list', '--help']);
    const seedsList = runCli(root, ['seeds', 'list']);
    for (const [label, result] of [
      ['possible --help', possibleHelp],
      ['ideas --help', ideasHelp],
      ['possible seed --help', seedHelp],
      ['possible seed list --help', seedListHelp],
      ['seeds list', seedsList],
    ]) {
      assert(result.status === 0, `${label} exited ${result.status}`);
    }
    // oclif may render the invoked alias in usage and command headings. Check
    // the public help surface and an action, rather than byte-identical help.
    for (const name of ['explain', 'run', 'jobs', 'job', 'nightly', 'list', 'show', 'grid', 'dots', 'prompt', 'seed']) {
      const pattern = new RegExp(`\\b${name}\\b`);
      assert(pattern.test(possibleHelp.stdout) && pattern.test(ideasHelp.stdout), `possible/ideas help is missing ${name}`);
    }
    const possibleList = runCli(root, ['possible', 'list']);
    const ideasList = runCli(root, ['ideas', 'list']);
    assert(possibleList.status === 0 && ideasList.status === 0 && normalize(possibleList.stdout).length > 0 &&
      normalize(possibleList.stdout) === normalize(ideasList.stdout), 'ideas list does not preserve possible list behavior');
    assert(/seed/i.test(seedHelp.combined), 'possible seed --help did not describe seeds');
    assert(/seeds/i.test(seedListHelp.combined), 'possible seed list --help did not resolve to seeds');
    return {
      value: {
        ideasAliasMatchesPossible: true,
        possibleSeedHelpResolves: true,
        possibleSeedListHelpResolves: true,
        seedsListResolves: true,
      },
    };
  });

  await check('P2P-026', async () => {
    ensureBuilt();
    const home = tempHome();
    writeLastVersion(home, '1.3.0');
    const sync = runCli(root, ['sync', '--cookies', 'ct0', 'auth', '--no-media', '--max-pages', '0'], {
      home,
      timeout: 120000,
    });
    assert(/F i e l d|fieldtheory\.dev\/cli/i.test(sync.combined), 'sync did not print the logo');
    assert(/What's new/i.test(sync.combined), "sync did not print What's New");
    const quiet = [
      ['search', 'q', '--json'],
      ['recent'],
      ['ls'],
      ['possible', '_run-job', 'abc'],
    ];
    for (const args of quiet) {
      const result = runCli(root, args, { home });
      assert(!hasChrome(result.combined), `${args.join(' ')} printed command chrome`);
    }
    return {
      value: {
        syncShowsLogo: true,
        syncShowsWhatsNew: true,
        quietCommands: quiet.map((args) => args.join(' ')),
        quietChrome: false,
      },
    };
  });

  await check('P2P-027', async () => {
    ensureBuilt();
    const home = tempHome();
    const indexed = runCli(root, ['index'], { home, seedFixture: true });
    assert(indexed.status === 0, `index exited ${indexed.status}: ${normalize(indexed.combined).slice(-800)}`);
    const search = runCli(root, ['search', 'machine', '--limit', '20', '--json'], { home });
    assert(search.status === 0, `search exited ${search.status}: ${normalize(search.combined).slice(-800)}`);
    const records = JSON.parse(search.stdout);
    assert(Array.isArray(records) && records.length > 0, 'search --json did not return records');
    for (const key of ['id', 'url', 'text', 'authorHandle', 'authorName', 'postedAt', 'score']) {
      assert(Object.prototype.hasOwnProperty.call(records[0], key), `search record is missing ${key}`);
    }
    assert(!hasChrome(search.stdout), 'search --json stdout contains command chrome');
    const status = runCli(root, ['status', '--json'], { home });
    assert(status.status === 0, `status --json exited ${status.status}`);
    const statusJson = JSON.parse(status.stdout);
    assert(statusJson.bookmarks && statusJson.paths, 'status --json shape changed');
    const paths = runCli(root, ['paths', '--json'], { home });
    assert(paths.status === 0, `paths --json exited ${paths.status}`);
    const pathsJson = JSON.parse(paths.stdout);
    assert(pathsJson.fieldTheoryDir && pathsJson.active, 'paths --json shape changed');
    return {
      value: {
        searchRecordFields: Object.keys(records[0]).sort(),
        searchRecordCount: records.length,
        statusFields: Object.keys(statusJson).sort(),
        pathsFields: Object.keys(pathsJson).sort(),
        searchStdoutChrome: false,
      },
    };
  });

  await check('P2P-028', async () => {
    ensureBuilt();
    const home = tempHome();
    writeUpdateCache(home, '99.99.99');
    const direct = runCli(root, ['stats'], { home });
    assert(/Update available/i.test(direct.combined), 'node dist/cli.js stats did not run the post-action update check');
    const bin = runCli(root, ['stats'], { home, entry: path.join(root, 'bin', 'ft.mjs') });
    assert(!/Update available/i.test(bin.combined), 'node bin/ft.mjs stats ran the post-action update check');
    const json = runCli(root, ['stats', '--json'], { home });
    assert(!/Update available/i.test(json.combined), 'ft stats --json ran the post-action update check');
    const tsx = runTsx(root, ['src/cli.ts', 'stats'], { home, env: { FT_DATA_DIR: path.join(home, 'data') } });
    assert(/Update available/i.test(tsx.combined), 'tsx src/cli.ts stats did not run the post-action update check');
    return {
      value: {
        distEntry: true,
        binEntry: false,
        jsonEntry: false,
        tsxEntry: true,
      },
    };
  });

  await check('P2P-030', async () => {
    ensureBuilt();
    const pathResult = runCli(root, ['path']);
    assert(pathResult.status === 0, `path exited ${pathResult.status}`);
    const search = runCli(root, ['search', 'q']);
    assert(search.status !== 0, 'search without data/index exited 0');
    const recent = runCli(root, ['recent', '--limit', '0']);
    assert(recent.status !== 0, 'recent --limit 0 exited 0');
    assert(/positive integer|invalid/i.test(recent.combined), 'recent --limit 0 did not report a validation error');
    return {
      value: {
        pathExit: 'zero',
        searchMissingDataExit: 'nonzero',
        validationExit: 'nonzero',
        validationMessage: 'positive integer',
      },
    };
  });

  await check('P2P-031', async () => {
    ensureBuilt();
    const cases = [
      [['sync', '--rebuild', '--continue'], /cannot be used together/i],
      [['sync', '--gaps', '--rebuild'], /cannot be used together/i],
      [['sync', '--folders', '--folder', 'x'], /cannot be used together/i],
      [['possible', 'run', '--effort', 'high', '--weight', 'low'], /effort.*weight|weight.*effort|either --effort/i],
      [['possible', 'run', '--repo', '.', '--repos', '.', 'x'], /Provide either|seed-artifact|either --repo|repo.*repos|repos.*repo/i],
      [['seeds', 'create'], /--artifact|required option/i],
    ];
    for (const [args, pattern] of cases) {
      const result = runCli(root, args);
      assert(result.status !== 0, `${args.join(' ')} exited 0`);
      assert(pattern.test(result.combined), `${args.join(' ')} did not report the expected validation message`);
    }
    return {
      value: {
        cases: cases.map(([args]) => args.join(' ')),
        allNonzero: true,
        messagesMatched: true,
      },
    };
  });

  await check('P2P-032', async () => {
    ensureBuilt();
    const home = tempHome();
    writeUpdateCache(home, '99.99.99');
    const present = runCli(root, ['--help'], { home });
    assert(present.status === 0, `--help exited ${present.status}`);
    assert(
      /Update available/i.test(present.stdout) && /99\.99\.99/.test(present.stdout),
      'help did not include the cached update notice',
    );
    fs.rmSync(path.join(home, 'data', '.update-check'), { force: true });
    const absent = runCli(root, ['--help'], { home });
    assert(absent.status === 0, `--help exited ${absent.status}`);
    assert(!/Update available/i.test(absent.stdout), 'help included the update notice without a cache');
    return {
      value: {
        cachedNotice: true,
        missingCacheNotice: false,
      },
    };
  });

  await check('P2P-033', async () => {
    ensureBuilt();
    const search = runCli(root, ['search', '--help']);
    assert(search.status === 0, `search --help exited ${search.status}`);
    const searchTokens = ['query', '--author', '--after', '--before', '--limit', '--json'];
    for (const token of searchTokens) {
      assert(search.combined.includes(token), `search --help is missing ${token}`);
    }
    const possible = runCli(root, ['possible', '--help']);
    assert(possible.status === 0, `possible --help exited ${possible.status}`);
    const possibleTokens = ['run', 'nightly', 'seed'];
    for (const token of possibleTokens) {
      assert(possible.combined.includes(token), `possible --help is missing ${token}`);
    }
    return {
      value: {
        searchHelpTokens: searchTokens,
        possibleHelpTokens: possibleTokens,
      },
    };
  });

  await check('P2P-034', async () => {
    ensureBuilt();
    const nested = [
      ['current', 'update', '--help'],
      ['codex', 'panel', '--help'],
      ['possible', '_run-job', '--help'],
      ['seeds', 'list', '--help'],
    ];
    for (const args of nested) {
      const result = runCli(root, args);
      assert(result.status === 0, `${args.join(' ')} exited ${result.status}`);
      assert(!/unknown command|not found/i.test(result.combined), `${args.join(' ')} reported an unknown command`);
    }
    const nightly = runCli(root, ['possible', 'nightly', 'list']);
    assert(nightly.status === 0, `possible nightly list exited ${nightly.status}`);
    const bookmarks = runCli(root, ['bookmarks', '--help']);
    assert(!/unknown command/i.test(bookmarks.combined), 'bookmarks --help reported an unknown command');
    assert(/bookmarks/i.test(bookmarks.combined), 'bookmarks --help did not resolve the bookmarks group');
    assert(/sync/.test(bookmarks.combined) && /search/.test(bookmarks.combined), 'bookmarks --help did not list alias commands');
    return {
      value: {
        nestedCommands: nested.map((args) => args.join(' ')),
        nightlyListExit: 'zero',
        bookmarksGroupResolves: true,
      },
    };
  });

  await check('P2P-035', async () => {
    ensureBuilt();
    assert(pkg.bin?.ft === 'bin/ft.mjs' && pkg.bin?.fieldtheory === 'bin/ft.mjs', 'package bin mapping changed');
    const version = runCli(root, ['--version'], { entry: path.join(root, 'bin', 'ft.mjs') });
    assert(version.status === 0, `bin/ft.mjs --version exited ${version.status}`);
    assert(
      normalize(version.stdout) === pkg.version,
      `bin/ft.mjs --version printed ${JSON.stringify(normalize(version.stdout))}`,
    );
    const help = runCli(root, ['--help'], { entry: path.join(root, 'bin', 'ft.mjs') });
    assert(help.status === 0, `bin/ft.mjs --help exited ${help.status}`);
    return {
      value: {
        bin: { ft: pkg.bin.ft, fieldtheory: pkg.bin.fieldtheory },
        version: pkg.version,
      },
    };
  });

  await check('P2P-037', async () => {
    ensureBuilt();
    const checks = [
      [['sync', '--help'], ['--cookies', '--no-media']],
      [['seeds', 'create', '--help'], ['--artifact']],
      [['possible', 'run', '--help'], ['--weight']],
      [['skill', 'install', '--help'], ['-y', '--yes']],
      [['possible', 'nightly', 'install', '--help'], ['--no-defaults', '--no-launchd', '--no-load']],
    ];
    for (const [args, tokens] of checks) {
      const result = runCli(root, args);
      assert(result.status === 0, `${args.join(' ')} exited ${result.status}`);
      for (const token of tokens) assert(result.combined.includes(token), `${args.join(' ')} is missing ${token}`);
    }
    return {
      value: {
        helpChecks: checks.map(([args, tokens]) => ({ command: args.join(' '), tokens })),
      },
    };
  });

  await check('P2P-038', async () => {
    ensureBuilt();
    const group = runCli(root, ['bookmarks', '--help']);
    for (const token of ['sync', 'search', 'list', 'show', 'stats', 'enable']) {
      assert(group.combined.includes(token), `bookmarks --help is missing ${token}`);
    }
    assert(!/unknown command/i.test(group.combined), 'bookmarks --help reported an unknown command');
    const bare = runCli(root, ['bookmarks']);
    for (const token of ['sync', 'search', 'enable']) {
      assert(bare.combined.includes(token), `bookmarks is missing ${token}`);
    }
    return {
      value: {
        bookmarksGroupResolves: true,
        listedCommands: ['sync', 'search', 'list', 'show', 'stats', 'enable'],
        bareGroupResolves: true,
      },
    };
  });

  await check('P2P-039', async () => {
    const files = [
      'tests/bookmarks-db.test.ts',
      'tests/graphql-bookmarks.test.ts',
      'tests/library.test.ts',
      'tests/commands-files.test.ts',
      'tests/skill.test.ts',
      'tests/app-install.test.ts',
    ];
    const outcomes = files.map((file) => runRepoTestFile(root, file));
    for (const outcome of outcomes) {
      assert(outcome.present, `${outcome.file} is missing`);
      assert(outcome.passing, `${outcome.file} did not pass`);
    }
    return {
      value: {
        files,
        allPassing: true,
      },
      evidence: { outcomes },
    };
  });

  const fail = results.filter((entry) => entry.status !== 'PASS').length;
  const report = {
    schema_version: 'p2p-side-report:v1',
    batch_id: BATCH_ID,
    side,
    required_runtime_observations: onlyObservation ? [onlyObservation] : [...OBSERVATION_IDS],
    observations: results,
    runtime_observations: results,
    observations_by_id: Object.fromEntries(results.map((entry) => [entry.observation_id, entry])),
    summary: { total: results.length, pass: results.length - fail, fail },
  };
  return report;
}

export const observationIds = OBSERVATION_IDS;

export default runHarness;
