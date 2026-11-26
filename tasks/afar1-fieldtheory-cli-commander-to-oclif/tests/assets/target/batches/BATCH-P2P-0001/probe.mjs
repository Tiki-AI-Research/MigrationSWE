#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const opts = {};
const positional = [];
for (let i = 0; i < argv.length; i += 1) {
  const token = argv[i];
  if (token.startsWith('--')) {
    const eq = token.indexOf('=');
    if (eq !== -1) opts[token.slice(2, eq)] = token.slice(eq + 1);
    else if (i + 1 < argv.length && !argv[i + 1].startsWith('-')) opts[token.slice(2)] = argv[++i];
    else opts[token.slice(2)] = true;
  } else {
    positional.push(token);
  }
}

const side = String(opts.side || (here.split(path.sep).includes('source') ? 'source' : 'target'));
const report = opts.out || opts.report || opts.results || opts.result
  || opts.output || opts['report-path']
  || positional.find((value) => /\.(json|jsonl)$/i.test(value))
  || null;

function isRepo(dir) {
  try {
    return Boolean(dir)
      && fs.existsSync(path.join(dir, 'package.json'))
      && fs.existsSync(path.join(dir, 'bin', 'ft.mjs'));
  } catch {
    return false;
  }
}

function walkUp(start) {
  let dir = path.resolve(start);
  for (;;) {
    if (isRepo(dir)) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function resolveRepo() {
  const explicit = opts.repo || opts['repo-root'] || opts.workspace || opts.cwd;
  const envNames = [
    'FT_REPO',
    'REPO_ROOT',
    'WORKSPACE_REPO',
    'MIGRATION_REPO',
    side === 'target' ? 'TARGET_REPO' : 'SOURCE_REPO',
  ];
  const candidates = [
    explicit,
    ...envNames.map((name) => process.env[name]),
    process.cwd(),
    walkUp(process.cwd()),
    walkUp(here),
    '/workspace/repo',
  ];
  try {
    for (const entry of fs.readdirSync('/workspace')) {
      candidates.push(path.join('/workspace', entry));
    }
  } catch {
    // ignore
  }
  for (const candidate of candidates) {
    if (candidate && isRepo(candidate)) return path.resolve(candidate);
  }
  return path.resolve(process.cwd());
}

const repo = resolveRepo();
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;

function stripAnsi(value) {
  return String(value ?? '').replace(ANSI, '').replace(/\r/g, '');
}

function makeHome(prefix = 'ft-p2p-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function removeDir(dir) {
  try {
    fs.rmSync(dir, { recursive: true, force: true });
  } catch {
    // ignore
  }
}

function cliEnv(home, extra = {}) {
  return {
    ...process.env,
    HOME: home,
    FT_DATA_DIR: path.join(home, 'data'),
    FT_LIBRARY_DIR: path.join(home, 'library'),
    FT_COMMANDS_DIR: path.join(home, 'commands'),
    ...extra,
  };
}

function runCli(args, options = {}) {
  const home = options.home ?? makeHome();
  const data = path.join(home, 'data');
  if (options.fixture) {
    fs.mkdirSync(data, { recursive: true });
    fs.writeFileSync(path.join(data, 'bookmarks.jsonl'), options.fixture);
  }
  if (options.seedFiles) {
    for (const [relative, content] of Object.entries(options.seedFiles)) {
      const target = path.join(home, relative);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, content);
    }
  }
  const result = spawnSync(process.execPath, [path.join(repo, 'bin', 'ft.mjs'), ...args], {
    cwd: options.cwd ?? home,
    input: options.input,
    encoding: 'utf8',
    timeout: options.timeout ?? 120000,
    env: cliEnv(home, options.env),
  });
  return {
    home,
    data,
    status: result.status,
    stdout: stripAnsi(result.stdout),
    stderr: stripAnsi(result.stderr),
    error: result.error,
  };
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function runTty(args, options = {}) {
  const home = options.home ?? makeHome();
  const fakeBin = path.join(home, 'fake-bin');
  fs.mkdirSync(fakeBin, { recursive: true });
  const fakeCodex = path.join(fakeBin, 'codex');
  fs.writeFileSync(fakeCodex, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  const command = [process.execPath, path.join(repo, 'bin', 'ft.mjs'), ...args].map(shellQuote).join(' ');
  const result = spawnSync('script', ['-qec', command, '/dev/null'], {
    cwd: home,
    input: options.input,
    encoding: 'utf8',
    timeout: options.timeout ?? 120000,
    env: cliEnv(home, {
      PATH: `${fakeBin}${path.delimiter}${process.env.PATH ?? ''}`,
      ...(options.env ?? {}),
    }),
  });
  return {
    home,
    status: result.status,
    stdout: stripAnsi(result.stdout),
    stderr: stripAnsi(result.stderr),
    error: result.error,
  };
}

function ensureBuilt() {
  const entry = path.join(repo, 'dist', 'cli.js');
  if (fs.existsSync(entry)) return { built: true, output: '' };
  const commands = [
    ['npm', ['run', 'build']],
    [path.join(repo, 'node_modules', '.bin', 'tsc'), ['-p', 'tsconfig.json']],
  ];
  let output = '';
  for (const [command, args] of commands) {
    if (command.includes(path.sep) && !fs.existsSync(command)) continue;
    const result = spawnSync(command, args, {
      cwd: repo,
      encoding: 'utf8',
      timeout: 300000,
    });
    output += stripAnsi(`${result.stdout ?? ''}${result.stderr ?? ''}`);
    if (fs.existsSync(entry)) return { built: true, output };
  }
  return { built: fs.existsSync(entry), output };
}

function escapeTestPattern(pattern) {
  return String(pattern).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function parseTapSummary(output) {
  const text = stripAnsi(output);
  const pass = Number((text.match(/^# pass (\d+)\s*$/m) ?? [])[1] ?? 0);
  const fail = Number((text.match(/^# fail (\d+)\s*$/m) ?? [])[1] ?? 0);
  if (pass + fail > 0) return { pass, fail, tests: pass + fail };
  const notOk = (text.match(/^not ok \d+/gm) ?? []).length;
  const ok = (text.match(/^ok \d+/gm) ?? []).length;
  return { pass: ok, fail: notOk, tests: ok + notOk };
}

function tsxCommand() {
  const local = path.join(repo, 'node_modules', '.bin', 'tsx');
  return fs.existsSync(local) ? local : 'tsx';
}

function runNamedTest(file, name) {
  const full = path.join(repo, file);
  if (!fs.existsSync(full)) {
    return { pass: false, text: `${file} :: missing test file`, summary: { tests: 0, pass: 0, fail: 1 } };
  }
  const args = ['--test', '--test-name-pattern', escapeTestPattern(name), file];
  let result = spawnSync(tsxCommand(), args, {
    cwd: repo,
    encoding: 'utf8',
    timeout: 180000,
    env: { ...process.env },
  });
  if (result.error && result.error.code === 'ENOENT') {
    result = spawnSync(process.execPath, ['--import', 'tsx', '--test', ...args], {
      cwd: repo,
      encoding: 'utf8',
      timeout: 180000,
      env: { ...process.env },
    });
  }
  const text = stripAnsi(`${result.stdout ?? ''}\n${result.stderr ?? ''}`);
  const summary = parseTapSummary(text);
  const sawName = text.includes(name);
  return {
    pass: result.status === 0 && summary.fail === 0 && summary.pass >= 1 && sawName,
    text,
    summary,
  };
}

let projectionCache = null;

function getProjections() {
  if (projectionCache) return projectionCache;
  const script = path.join(here, 'projections.mjs');
  projectionCache = { values: {}, error: '' };
  if (!fs.existsSync(script)) {
    projectionCache.error = `projection script missing: ${script}`;
    return projectionCache;
  }
  const args = [script, repo];
  let result = spawnSync(tsxCommand(), args, {
    cwd: repo,
    encoding: 'utf8',
    timeout: 180000,
    env: { ...process.env },
  });
  if (result.error && result.error.code === 'ENOENT') {
    result = spawnSync(process.execPath, ['--import', 'tsx', script, repo], {
      cwd: repo,
      encoding: 'utf8',
      timeout: 180000,
      env: { ...process.env },
    });
  }
  const text = String(result.stdout ?? '');
  try {
    projectionCache.values = JSON.parse(text);
  } catch {
    projectionCache.error = stripAnsi(`${text}\n${result.stderr ?? ''}`).slice(-1200);
  }
  return projectionCache;
}

function testProjection(id, file, name) {
  const executed = runNamedTest(file, name);
  const projections = getProjections();
  const entry = projections.values[id];
  const ready = Boolean(entry && entry.ok);
  const value = ready
    ? entry.value
    : {
      outcome: 'INCONCLUSIVE',
      error: entry?.error ?? projections.error ?? 'projection unavailable',
    };
  const pass = executed.pass && ready;
  const detailText = `${file} :: ${name} :: test=${executed.pass ? 'PASS' : 'FAIL'} :: projection=${ready ? 'VALUE' : 'MISSING'}`;
  add(id, pass, value, `${detailText}${pass ? '' : ` :: ${executed.text.slice(-500)} ${ready ? '' : value.error}`}`, {
    test: {
      file,
      name,
      status: executed.pass ? 'PASS' : 'FAIL',
      ...executed.summary,
    },
  });
}

function readJsonSafe(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

function optionDefault(helpText, option) {
  const text = stripAnsi(helpText).replace(/\s+/g, ' ');
  let from = 0;
  while (from < text.length) {
    const index = text.indexOf(option, from);
    if (index === -1) return null;
    const window = text.slice(index, index + 220);
    const match = window.match(/(?:\(|\[)?default:\s*(\d+)/i);
    if (match) return Number(match[1]);
    from = index + option.length;
  }
  return null;
}

function relativeFiles(root) {
  const files = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else files.push(path.relative(root, full).split(path.sep).join('/'));
    }
  };
  if (fs.existsSync(root)) walk(root);
  return files.sort();
}

function normalizeArtifacts(files) {
  return files.map((file) => file
    .replace(/seed-[0-9a-f]{6,}/g, 'seed-<id>')
    .replace(/\d{4}-\d{2}-\d{2}/g, '<date>')
    .replace(/[0-9a-f]{10,}\.md$/g, '<artifact>.md'));
}

const results = [];

function detail(value) {
  return String(value ?? '').split(repo).join('<repo>').slice(0, 700);
}

function add(id, pass, projection, description, extra = {}) {
  results.push({
    id,
    status: pass ? 'PASS' : 'FAIL',
    projection,
    value: projection,
    result: projection,
    detail: detail(description),
    side,
    ...extra,
  });
}

function guard(id, fn) {
  try {
    fn();
  } catch (error) {
    add(id, false, { error: String(error?.message ?? error) }, 'probe exception');
  }
}

const ALL_IDS = [
  'P2P-007', 'P2P-011', 'P2P-001', 'P2P-012', 'P2P-023', 'P2P-010',
  'P2P-013', 'P2P-014', 'P2P-016', 'P2P-017', 'P2P-018', 'P2P-019',
  'P2P-020', 'P2P-021', 'P2P-022', 'P2P-024', 'P2P-005', 'P2P-006',
  'P2P-008', 'P2P-009', 'P2P-002', 'P2P-003', 'P2P-004',
];

const fixtureFallback = [
  { id: '1', tweetId: '1', url: 'https://x.com/alice/status/1', text: 'Machine learning is transforming healthcare', authorHandle: 'alice', authorName: 'Alice Smith', syncedAt: '2026-01-01T00:00:00Z', postedAt: '2026-01-01T12:00:00Z', language: 'en', engagement: { likeCount: 100, repostCount: 10 }, mediaObjects: [], links: ['https://example.com'], tags: [], ingestedVia: 'graphql' },
  { id: '2', tweetId: '2', url: 'https://x.com/bob/status/2', text: 'Rust is a great systems programming language', authorHandle: 'bob', authorName: 'Bob Jones', syncedAt: '2026-02-01T00:00:00Z', postedAt: '2026-02-01T12:00:00Z', language: 'en', engagement: { likeCount: 50 }, mediaObjects: [], links: [], tags: [], ingestedVia: 'graphql' },
  { id: '3', tweetId: '3', url: 'https://x.com/alice/status/3', text: 'Deep learning models need massive compute', authorHandle: 'alice', authorName: 'Alice Smith', syncedAt: '2026-03-01T00:00:00Z', postedAt: '2026-03-01T12:00:00Z', language: 'en', engagement: { likeCount: 200, repostCount: 30 }, mediaObjects: [{ type: 'photo', url: 'https://img.com/1.jpg' }], links: [], tags: [], ingestedVia: 'graphql' },
];

const fixtureCandidates = [
  path.resolve(here, '../../../fixtures/batches/BATCH-P2P-0001/bookmarks-fixture.jsonl'),
  path.join(repo, 'fixtures/batches/BATCH-P2P-0001/bookmarks-fixture.jsonl'),
  '/assets/fixtures/batches/BATCH-P2P-0001/bookmarks-fixture.jsonl',
  '/workspace/repo/fixtures/batches/BATCH-P2P-0001/bookmarks-fixture.jsonl',
];
let fixture = `${fixtureFallback.map((record) => JSON.stringify(record)).join('\n')}\n`;
for (const candidate of fixtureCandidates) {
  try {
    if (candidate && fs.existsSync(candidate)) {
      fixture = fs.readFileSync(candidate, 'utf8');
      break;
    }
  } catch {
    // ignore
  }
}

const build = ensureBuilt();
const buildDetail = build.built ? 'dist/cli.js present' : `build failed: ${build.output}`;

testProjection('P2P-007', 'tests/engine-invoke.test.ts', 'invokeEngineAsync: returns trimmed stdout on zero exit');
testProjection('P2P-011', 'tests/ideas-repos.test.ts', 'listSavedRepos: returns empty when registry does not exist');
testProjection('P2P-001', 'tests/possible-wizard.test.ts', 'stepPickDepth: enter defaults to quick');
testProjection('P2P-012', 'tests/seeds-save.test.ts', 'saveSeedFromCandidates: carries tweet text + author + posted + url into the artifact body');
testProjection('P2P-023', 'tests/seeds-query.test.ts', 'formatSeedCandidates renders a compact readable list');
testProjection('P2P-005', 'tests/md.test.ts', 'stripWikiUpdatesSection: leaves answer unchanged when no section');
testProjection('P2P-006', 'tests/ideas-jobs.test.ts', 'createIdeasJob: stores job json and log beside ideas artifacts');
testProjection('P2P-008', 'tests/md.test.ts', 'MAX_CONSECUTIVE_FAILURES: is a sane positive integer');
testProjection('P2P-009', 'tests/ideas-aggregate.test.ts', 'aggregateTopDots: sorts by combined axis A + B score descending');
testProjection('P2P-002', 'tests/seeds-strategies.test.ts', 'summarizeSeedIntent produces readable compact summaries');
testProjection('P2P-003', 'tests/engine.test.ts', 'preferences: round-trip save and load');
testProjection('P2P-004', 'tests/graphql-bookmarks.test.ts', 'mergeBookmarkRecord: sparser incoming does not clobber richer existing');

guard('P2P-015', () => {
  const pkg = readJsonSafe(path.join(repo, 'package.json'));
  if (!pkg) throw new Error(`cannot read ${repo}/package.json`);
  const version = runCli(['--version']);
  const printed = version.stdout.trim();
  const bins = pkg.bin ?? {};
  const pass = build.built
    && version.status === 0
    && printed === pkg.version
    && bins.ft === 'bin/ft.mjs'
    && bins.fieldtheory === 'bin/ft.mjs';
  removeDir(version.home);
  add('P2P-015', pass, {
    version: printed,
    ft: bins.ft ?? null,
    fieldtheory: bins.fieldtheory ?? null,
  }, `${buildDetail}; version=${printed}`);
});

guard('P2P-016', () => {
  const pkg = readJsonSafe(path.join(repo, 'package.json'));
  if (!pkg) throw new Error(`cannot read ${repo}/package.json`);
  const run = runCli(['--version']);
  const entry = path.join(repo, 'bin', 'ft.mjs');
  const executable = fs.existsSync(entry) && (fs.statSync(entry).mode & 0o111) !== 0;
  const printed = run.stdout.trim();
  const pass = build.built
    && run.status === 0
    && pkg.bin?.fieldtheory === 'bin/ft.mjs'
    && executable
    && printed === pkg.version;
  removeDir(run.home);
  add('P2P-016', pass, {
    fieldtheory: pkg.bin?.fieldtheory ?? null,
    version: printed,
    executable,
  }, `${buildDetail}; fieldtheory entry`);
});

guard('P2P-010', () => {
  const cases = [
    ['recent', '--limit', '0'],
    ['ls', '--limit', '-1'],
    ['find', 'q', '--limit', '1.5'],
    ['grep', 'q', '--limit', 'abc'],
    ['context', '--limit', '0'],
    ['head', 'f', '--lines', '0'],
  ];
  const observed = [];
  let allRejected = build.built;
  for (const args of cases) {
    const run = runCli(args);
    const positiveInteger = /positive integer/i.test(run.stderr);
    const rejected = run.status !== 0 && positiveInteger;
    observed.push({
      command: args.join(' '),
      exit: run.status,
      positive_integer: positiveInteger,
      rejected,
    });
    allRejected = allRejected && rejected;
    removeDir(run.home);
  }
  add('P2P-010', allRejected, { cases: observed }, `${buildDetail}; positive-integer validation`);
});

guard('P2P-013', () => {
  const sync = runTty(['sync', '--rebuild'], { input: 'n\n' });
  const model = runTty(['model'], { input: 'codex\n' });
  const syncPrompt = /Continue\? \(y\/N\)/i.test(`${sync.stdout}${sync.stderr}`);
  const modelPrompt = /Select default:/i.test(`${model.stdout}${model.stderr}`);
  removeDir(sync.home);
  removeDir(model.home);
  add('P2P-013', build.built && syncPrompt && modelPrompt, {
    sync_rebuild_prompt: syncPrompt,
    model_engine_prompt: modelPrompt,
  }, `${buildDetail}; prompts ${syncPrompt}/${modelPrompt}`);
});

guard('P2P-014', () => {
  const index = runCli(['index'], { fixture, timeout: 180000 });
  const search = runCli(['search', 'learning', '--limit', '20', '--json'], { home: index.home });
  let ids = [];
  let parsed = false;
  try {
    ids = JSON.parse(search.stdout).map((record) => record.id).sort();
    parsed = true;
  } catch {
    parsed = false;
  }
  const clean = !/Field Theory|What's New|What.s New|spinner/i.test(search.stdout);
  const pass = build.built
    && index.status === 0
    && search.status === 0
    && parsed
    && clean
    && ids.join(',') === '1,3';
  removeDir(index.home);
  add('P2P-014', pass, {
    record_ids: ids,
    json_array: parsed,
    clean_stdout: clean,
  }, `${buildDetail}; index=${index.status} search=${search.status}`);
});

guard('P2P-017', () => {
  const cases = [
    ['show'],
    ['sync', '--bogus'],
    ['recent', '--limit'],
    ['seeds', 'create'],
  ];
  const observed = [];
  let ok = build.built;
  for (const args of cases) {
    const run = runCli(args);
    const nonzero = run.status !== 0;
    const error = /error/i.test(run.stderr);
    const help = /(?:^|\n)\s*(?:USAGE|Usage:)/i.test(run.stderr);
    const passed = nonzero && error && help;
    observed.push({ command: args.join(' '), exit: run.status, nonzero, error, help });
    ok = ok && passed;
    removeDir(run.home);
  }
  add('P2P-017', ok, { cases: observed }, `${buildDetail}; error then help`);
});

guard('P2P-018', () => {
  const search = runCli(['search', '--help']);
  const list = runCli(['list', '--help']);
  const tree = runCli(['tree', '--help']);
  const sync = runCli(['sync', '--help']);
  const searchLimit = optionDefault(search.stdout, '--limit');
  const treeLimit = optionDefault(tree.stdout, '--limit');
  const syncDelay = optionDefault(sync.stdout, '--delay-ms');
  const syncMaxMinutes = optionDefault(sync.stdout, '--max-minutes');
  removeDir(search.home);
  removeDir(list.home);
  removeDir(tree.home);
  removeDir(sync.home);

  const index = runCli(['index'], { fixture, timeout: 180000 });
  const all = runCli(['list', '--json'], { home: index.home });
  const offset = runCli(['list', '--offset', '1', '--json'], { home: index.home });
  // `list --json` is part of the public contract: which records it returns and
  // which fields each record carries must survive the migration.  The offset
  // self-consistency check alone accepts a payload that silently drops records,
  // so the record ids and the record field names are observed and compared
  // against the frozen source baseline (mirroring P2P-014/P2P-027 for `search`).
  let allRecords = [];
  let offsetIds = [];
  try {
    const parsed = JSON.parse(all.stdout);
    allRecords = Array.isArray(parsed) ? parsed : [];
    offsetIds = JSON.parse(offset.stdout).map((record) => record.id);
  } catch {
    allRecords = [];
    offsetIds = [];
  }
  const allIds = allRecords.map((record) => record.id);
  const originalRecord = allRecords[0];
  const listRecordFields = originalRecord && typeof originalRecord === 'object'
    ? Object.keys(originalRecord).sort()
    : [];
  const offsetZero = allIds.length === offsetIds.length + 1 && offsetIds[0] === allIds[1];
  const listOffset = offsetZero ? 0 : null;
  removeDir(index.home);

  const pass = build.built
    && searchLimit === 20
    && listOffset === 0
    && allIds.length > 0
    && treeLimit === 80
    && syncDelay === 600
    && syncMaxMinutes === 30;
  add('P2P-018', pass, {
    search_limit: searchLimit,
    list_offset: listOffset,
    list_record_ids: allIds,
    list_record_fields: listRecordFields,
    tree_limit: treeLimit,
    sync_delay_ms: syncDelay,
    sync_max_minutes: syncMaxMinutes,
  }, `${buildDetail}; numeric defaults + list payload`);
});

guard('P2P-019', () => {
  const expected = ['app', 'append', 'ask', 'auth', 'back', 'backlinks', 'bookmarks', 'cat', 'categories', 'cd', 'classify', 'classify-domains', 'codex', 'commands', 'context', 'current', 'domains', 'fetch-media', 'find', 'folders', 'frames', 'grep', 'head', 'index', 'install', 'library', 'link', 'links', 'lint', 'list', 'ls', 'md', 'meta', 'model', 'new', 'note', 'open', 'panel', 'path', 'paths', 'possible', 'pwd', 'recent', 'rename', 'repos', 'reveal', 'sample', 'search', 'seeds', 'show', 'skill', 'stats', 'status', 'sync', 'tab', 'tagged', 'tags', 'tree', 'viz', 'wiki'];
  const resolved = [];
  const unresolved = [];
  let bookmarksText = '';
  for (const name of expected) {
    const run = name === 'bookmarks' ? runCli(['bookmarks']) : runCli([name, '--help']);
    const text = `${run.stdout}${run.stderr}`;
    if (name === 'bookmarks') bookmarksText = text;
    const unknown = /unknown command|nonexistent|not found/i.test(text);
    const ok = name === 'bookmarks'
      ? !unknown && /bookmarks/i.test(text)
      : run.status === 0 && !unknown;
    if (ok) resolved.push(name);
    else unresolved.push(name);
    removeDir(run.home);
  }

  const nested = [
    ['current', 'update', '--help'],
    ['codex', 'panel', '--help'],
    ['possible', 'nightly', 'list', '--help'],
    ['possible', '_run-job', '--help'],
    ['seeds', 'list', '--help'],
  ];
  const nestedUnresolved = [];
  for (const args of nested) {
    const run = runCli(args);
    const text = `${run.stdout}${run.stderr}`;
    if (run.status !== 0 || /unknown command|nonexistent|not found/i.test(text)) {
      nestedUnresolved.push(args.join(' '));
    }
    removeDir(run.home);
  }

  const ideas = runCli(['ideas', '--help']);
  const aliasIdeas = ideas.status === 0
    && !/unknown command|nonexistent|not found/i.test(`${ideas.stdout}${ideas.stderr}`);
  removeDir(ideas.home);

  const bookmarksSubcommands = /sync/i.test(bookmarksText)
    && /search/i.test(bookmarksText)
    && /enable/i.test(bookmarksText);
  const pass = build.built
    && unresolved.length === 0
    && resolved.length === expected.length
    && nestedUnresolved.length === 0
    && aliasIdeas
    && bookmarksSubcommands;
  add('P2P-019', pass, {
    resolved,
    unresolved,
    nested_unresolved: nestedUnresolved,
    alias_ideas: aliasIdeas,
    bookmarks_subcommands: bookmarksSubcommands,
  }, `${buildDetail}; command tree`);
});

guard('P2P-020', () => {
  const pkg = readJsonSafe(path.join(repo, 'package.json'));
  if (!pkg) throw new Error(`cannot read ${repo}/package.json`);
  const empty = runCli([], { seedFiles: { 'data/.update-check': pkg.version } });
  const existing = runCli([], {
    seedFiles: {
      'data/.update-check': pkg.version,
      'data/bookmarks.jsonl': '{}\n',
    },
  });
  const welcome = /Get started/i.test(empty.stdout);
  const dashboard = /Sync now:/i.test(existing.stdout) && /bookmarks/i.test(existing.stdout);
  const pass = build.built
    && empty.status === 0
    && existing.status === 0
    && welcome
    && dashboard;
  removeDir(empty.home);
  removeDir(existing.home);
  add('P2P-020', pass, {
    empty: welcome ? 'welcome' : 'other',
    existing: dashboard ? 'dashboard' : 'other',
  }, `${buildDetail}; startup ${welcome}/${dashboard}`);
});

guard('P2P-021', () => {
  const home = makeHome();
  const seeds = runCli(['seeds', 'text', 'hello seed text'], { home, input: 'body\n' });
  const library = runCli(['library', 'create', 'notes/test', '--title', 'T', '--stdin'], { home, input: 'body text\n' });
  const commands = runCli(['commands', 'new', 'testcmd', '--stdin'], { home, input: 'cmd body\n' });
  const install = runCli(['install', 'app', '--json', '--install-dir', path.join(home, 'Applications')], { home });
  const skill = runCli(['skill', 'install', '-y'], { home });
  const files = normalizeArtifacts(relativeFiles(home));
  const statuses = {
    seeds_text: seeds.status,
    library_create: library.status,
    commands_new: commands.status,
    install_app: install.status,
    skill_install: skill.status,
  };
  const expectedFiles = [
    '.claude/commands/fieldtheory.md',
    'commands/testcmd.md',
    'data/.last-version',
    'data/ideas/adjacent/artifacts/<artifact>.md',
    'data/ideas/index.json',
    'data/ideas/latest.json',
    'data/ideas/seeds.json',
    'data/ideas/seeds/<date>/seed-<id>.md',
    'library/notes/test.md',
  ];
  const pass = build.built
    && statuses.seeds_text === 0
    && statuses.library_create === 0
    && statuses.commands_new === 0
    && statuses.install_app === 1
    && statuses.skill_install === 0
    && JSON.stringify(files) === JSON.stringify(expectedFiles);
  removeDir(home);
  add('P2P-021', pass, { statuses, files }, `${buildDetail}; stateful side effects`);
});

guard('P2P-022', () => {
  const run = runCli(['sync', '--cookies', 'ct0', 'auth-token', '--no-media', '--max-pages', '0'], { timeout: 120000 });
  const text = `${run.stdout}${run.stderr}`;
  const browserSession = /browser session is used|Make sure your browser is open/i.test(text);
  const paused = /page limit/i.test(text);
  const oauth = /oauth|authorization url|open the following url|visit the following url/i.test(text);
  const pass = build.built
    && run.status === 0
    && browserSession
    && paused
    && !oauth;
  removeDir(run.home);
  add('P2P-022', pass, {
    exit: run.status,
    browser_session: browserSession,
    paused_at_page_limit: paused,
    oauth_started: oauth,
  }, `${buildDetail}; sync cookies path`);
});

guard('P2P-024', () => {
  const listed = ['sync', 'search', 'possible', 'ideas', 'seeds', 'repos', 'frames', 'skill', 'library', 'commands', 'app', 'install', 'bookmarks'];
  const help = runCli(['--help']);
  const helpText = `${help.stdout}${help.stderr}`;
  const present = listed.filter((name) => new RegExp(`(^|[^A-Za-z0-9_-])${name}([^A-Za-z0-9_-]|$)`).test(helpText));
  const unresolved = [];
  for (const name of listed) {
    const run = name === 'bookmarks' ? runCli(['bookmarks']) : runCli([name, '--help']);
    const text = `${run.stdout}${run.stderr}`;
    const unknown = /unknown command|nonexistent|not found/i.test(text);
    const resolved = name === 'bookmarks'
      ? !unknown && /bookmarks/i.test(text)
      : run.status === 0 && !unknown;
    if (!resolved) unresolved.push(name);
    removeDir(run.home);
  }
  removeDir(help.home);
  const pass = build.built
    && present.length === listed.length
    && unresolved.length === 0;
  add('P2P-024', pass, {
    listed_commands: present,
    all_listed: present.length === listed.length,
    unresolved,
  }, `${buildDetail}; top-level help`);
});

for (const id of ALL_IDS) {
  if (!results.some((result) => result.id === id)) {
    add(id, false, { error: 'not-executed' }, 'observation was not executed');
  }
}

const observations = {};
for (const result of results) observations[result.id] = result;
const summary = results.map((result) => ({
  id: result.id,
  status: result.status,
  value: result.value,
}));
const payload = {
  observations,
  results: summary,
  summary,
};
const text = `${JSON.stringify(payload, null, 2)}\n`;

if (report) {
  try {
    const target = path.resolve(report);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, text);
  } catch {
    // ignore report write failures and still emit to stdout
  }
}
process.stdout.write(text);
