import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';

const rawArgs = process.argv.slice(2);
const repo = path.resolve(rawArgs.find((arg) => !arg.startsWith('--')) ?? process.env.FT_REPO_ROOT ?? '/workspace/repo');
const expected = (rawArgs.find((arg) => arg.startsWith('--expect=')) ?? '--expect=none').slice('--expect='.length);
const distEntry = path.join(repo, 'dist', 'cli.js');
const binEntry = path.join(repo, 'bin', 'ft.mjs');

function emit(payload, code) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  process.exit(code);
}

if (!fs.existsSync(distEntry)) {
  emit({ repo, status: 'INFRA_ERROR', reason: 'dist/cli.js is missing; build the repository first' }, 2);
}

const pkg = JSON.parse(fs.readFileSync(path.join(repo, 'package.json'), 'utf8'));
const version = pkg.version;

function makeHome(cache = '1.0.0') {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-probe-'));
  const data = path.join(home, 'data');
  fs.mkdirSync(data, { recursive: true });
  if (cache !== null) fs.writeFileSync(path.join(data, '.update-check'), cache);
  return { home, data };
}

function run(args, { bin = false, cache = '1.0.0', timeout = 20000 } = {}) {
  const { home, data } = makeHome(cache);
  const entry = bin ? binEntry : distEntry;
  const result = spawnSync(process.execPath, [entry, ...args], {
    cwd: home,
    encoding: 'utf8',
    timeout,
    env: {
      ...process.env,
      HOME: home,
      FT_DATA_DIR: data,
      FT_LIBRARY_DIR: path.join(home, 'library'),
      FT_COMMANDS_DIR: path.join(home, 'commands'),
    },
  });
  fs.rmSync(home, { recursive: true, force: true });
  return { status: result.status, out: result.stdout ?? '', err: result.stderr ?? '' };
}

function read(relativePath) {
  const absolute = path.join(repo, relativePath);
  return fs.existsSync(absolute) ? fs.readFileSync(absolute, 'utf8') : '';
}

function blackboxReady() {
  // ``tests/cli-blackbox.test.ts`` is an *optional* addition (instruction.md:
  // "MAY add black-box CLI tests").  Absence therefore yields NA for the
  // observations that are *about* that optional file, instead of FAIL: a
  // candidate that legitimately did not add it must not be scored as failing.
  const text = read('tests/cli-blackbox.test.ts');
  if (!text) return { present: false, reason: 'tests/cli-blackbox.test.ts not provided (optional)' };
  const required = ["'-V'", 'Missing 1 required arg', 'required option', '--artifact', 'not specified'];
  const missing = required.filter((item) => !text.includes(item));
  return { present: true, ok: missing.length === 0, reason: missing.length ? `missing assertions: ${missing.join(', ')}` : 'present' };
}

const results = {};
function add(id, passed, detail) {
  results[id] = { pass: Boolean(passed), detail };
}

{
  const help = run(['--help'], { cache: '99.99.99' });
  const noCache = run(['--help'], { cache: null });
  const bogus = run(['sync', '--bogus']);
  add('F2P-001', help.status === 0 && /Update available/.test(help.out) && /99\.99\.99/.test(help.out) && !/Update available/.test(noCache.out) && bogus.status !== 0 && /Nonexistent flag: --bogus/.test(bogus.err) && /USAGE/.test(bogus.err), {
    helpStatus: help.status,
    notice: /Update available/.test(help.out),
    noCacheNotice: /Update available/.test(noCache.out),
    bogusStatus: bogus.status,
    bogusErr: bogus.err.slice(0, 120),
  });
}

{
  const blackbox = blackboxReady();
  const anchored = read('tests/config.test.ts').includes('loadChromeSessionConfig: unknown browser throws');
  const show = run(['show']);
  add('F2P-002', anchored && show.status !== 0 && /Missing 1 required arg/.test(show.err) && /USAGE/.test(show.err), {
    blackbox,
    anchoredTest: anchored,
    showStatus: show.status,
  });
}

{
  const binText = read('bin/ft.mjs');
  const help = run(['-h'], { bin: true });
  const shortVersion = run(['-V'], { bin: true });
  add('F2P-003', /from '\.\.\/dist\/cli\.js'/.test(binText) && help.status === 0 && /USAGE/.test(help.out) && /\$ ft/.test(help.out) && shortVersion.status === 0 && shortVersion.out.trim() === version, {
    binImportsMain: /from '\.\.\/dist\/cli\.js'/.test(binText),
    hasParseAsync: /parseAsync/.test(binText),
    helpStatus: help.status,
    helpOut: help.out.slice(0, 80),
    versionOut: shortVersion.out.trim(),
  });
}

{
  const lock = read('package-lock.json');
  const imports = ['src/cli.ts', 'src/companion-cli.ts', 'bin/ft.mjs'].filter((file) => /from ['"]commander['"]|require\(['"]commander['"]\)/.test(read(file)));
  add('F2P-004', pkg.dependencies?.['@oclif/core'] === '4.0.0' && !pkg.dependencies?.commander && imports.length === 0, {
    oclif: pkg.dependencies?.['@oclif/core'],
    commander: pkg.dependencies?.commander,
    lockOclif: lock.includes('node_modules/@oclif/core'),
    imports,
  });
}

{
  const show = run(['show']);
  add('F2P-006', show.status !== 0 && /Missing 1 required arg/.test(show.err) && /USAGE/.test(show.err), {
    status: show.status,
    err: show.err.slice(0, 120),
  });
}

{
  const help = run(['sync', '--help']);
  const bogus = run(['sync', '--bogus']);
  add('F2P-008', help.status === 0 && /USAGE/.test(help.out) && /\$ ft sync/.test(help.out) && /--no-media/.test(help.out) && /--cookies/.test(help.out) && bogus.status !== 0 && /Nonexistent flag: --bogus/.test(bogus.err), {
    helpStatus: help.status,
    hasUsage: /USAGE/.test(help.out),
    hasFlags: /--no-media/.test(help.out) && /--cookies/.test(help.out),
    bogusErr: bogus.err.slice(0, 100),
  });
}

{
  const negated = run(['sync', '--no-media', '--max-pages', '0']);
  const positive = run(['sync', '--media']);
  add('F2P-010', !/Nonexistent flag: --no-media|unknown option '--no-media'/.test(negated.err) && positive.status !== 0 && /Nonexistent flag: --media/.test(positive.err) && /--no-media/.test(positive.err), {
    negatedStatus: negated.status,
    negatedErr: negated.err.slice(0, 120),
    positiveStatus: positive.status,
    positiveErr: positive.err.slice(0, 160),
  });
}

{
  const help = run(['--help']);
  const welcome = run([]);
  add('F2P-011', pkg.dependencies?.['@oclif/core'] === '4.0.0' && !pkg.dependencies?.commander && help.status === 0 && /USAGE/.test(help.out) && /\$ ft/.test(help.out) && welcome.status === 0 && /Get started/.test(welcome.out), {
    oclif: pkg.dependencies?.['@oclif/core'],
    commander: pkg.dependencies?.commander,
    helpStatus: help.status,
    welcome: /Get started/.test(welcome.out),
  });
}

{
  const probes = [
    ['current', 'update', '--help'],
    ['codex', 'panel', '--help'],
    ['possible', 'nightly', 'list'],
    ['possible', '_run-job', '--help'],
    ['seeds', 'list', '--help'],
    ['bookmarks', '--help'],
  ];
  const nested = probes.map((args) => ({ args: args.join(' '), ...run(args) }));
  add('F2P-012', nested.every((result) => result.status === 0 && !/unknown command|unknown option/i.test(result.err)), nested.map((result) => ({ args: result.args, status: result.status, err: result.err.slice(0, 60) })));
}

{
  const show = run(['show']);
  const bogus = run(['sync', '--bogus']);
  const recent = run(['recent', '--limit']);
  const seeds = run(['seeds', 'create']);
  add('F2P-016', [show, bogus, recent, seeds].every((result) => result.status !== 0 && /USAGE/.test(result.err)) && /required option '--artifact <id\.\.\.>' not specified/.test(seeds.err), {
    statuses: [show.status, bogus.status, recent.status, seeds.status],
    seedErr: seeds.err.slice(0, 120),
  });
}

{
  const help = run(['--help'], { cache: '99.99.99' });
  const bogus = run(['sync', '--bogus'], { cache: '99.99.99' });
  add('F2P-018', /Update available/.test(help.out) && /99\.99\.99/.test(help.out) && bogus.status !== 0 && /Nonexistent flag: --bogus/.test(bogus.err) && !/Update available/.test(bogus.err), {
    helpNotice: /Update available/.test(help.out),
    bogusErr: bogus.err.slice(0, 120),
    bogusNotice: /Update available/.test(bogus.err),
  });
}

{
  const help = run(['--help']);
  const searchHelp = run(['search', '--help']);
  let commanderResolvable = true;
  try {
    createRequire(path.join(repo, 'package.json')).resolve('commander');
  } catch {
    commanderResolvable = false;
  }
  const commands = ['sync', 'search', 'list', 'show', 'stats', 'viz', 'classify', 'possible', 'ideas', 'seeds', 'repos', 'frames', 'skill', 'library', 'commands', 'app', 'install', 'bookmarks'];
  const missing = commands.filter((command) => !new RegExp(`\\b${command}\\b`).test(help.out));
  add('F2P-015', help.status === 0 && missing.length === 0 && searchHelp.status === 0 && /query/.test(searchHelp.out) && /--limit/.test(searchHelp.out) && /--json/.test(searchHelp.out) && !commanderResolvable, {
    helpStatus: help.status,
    missingCommands: missing,
    searchHelpStatus: searchHelp.status,
    commanderResolvable,
  });
}

{
  const shortVersion = run(['--version'], { bin: true });
  let commanderResolvable = true;
  try {
    createRequire(path.join(repo, 'package.json')).resolve('commander');
  } catch {
    commanderResolvable = false;
  }
  add('F2P-022', pkg.bin?.ft === 'bin/ft.mjs' && pkg.bin?.fieldtheory === 'bin/ft.mjs' && shortVersion.status === 0 && shortVersion.out.trim() === version && !commanderResolvable, {
    bin: pkg.bin,
    versionStatus: shortVersion.status,
    versionOut: shortVersion.out.trim(),
    commanderResolvable,
  });
}

{
  const nightly = run(['possible', 'nightly', 'list']);
  const seed = run(['possible', 'seed', 'list']);
  const direct = run(['seeds', 'list']);
  add('F2P-023', nightly.status === 0 && seed.status === 0 && direct.status === 0 && seed.out === direct.out, {
    nightlyStatus: nightly.status,
    seedStatus: seed.status,
    directStatus: direct.status,
    sameOutput: seed.out === direct.out,
    seedErr: seed.err.slice(0, 100),
  });
}

{
  const blackbox = blackboxReady();
  const anchors = {
    'F2P-005': ['tests/engine.test.ts', 'invokeEngine (sync): child stdin is closed with EOF'],
    'F2P-007': ['tests/engine.test.ts', 'preferences: round-trip save and load'],
    'F2P-009': ['tests/engine.test.ts', 'resolveEngine: codex args include skip-git-repo-check'],
    'F2P-013': ['tests/engine.test.ts', 'invokeEngineAsync: child stdin is closed with EOF'],
    'F2P-014': ['tests/config.test.ts', 'loadChromeSessionConfig reads chrome user data dir and profile directory from env'],
    'F2P-017': ['tests/engine.test.ts', 'ft model: direct set persists preference'],
    'F2P-019': ['tests/engine.test.ts', 'invokeEngineAsync: timeout throws EngineInvocationError with reason=timeout, killed=true'],
    'F2P-020': ['tests/cli.test.ts', 'ft navigation commands cover links tags writes app targets and location state'],
    'F2P-021': ['tests/cli.test.ts', 'parseCookieOption: with only ct0, builds ct0-only header'],
    'F2P-024': ['tests/cli.test.ts', 'parseCookieOption: with ct0 and auth_token, joins both'],
  };
  for (const [id, [file, testName]] of Object.entries(anchors)) {
    const present = read(file).includes(testName);
    // The optional black-box file must not gate these: the observation asserts
    // the anchored test still exists, which is the task-specified behaviour.
    // On the pre-migration tree the runner forces FAIL (technology inactive),
    // so the fail-to-pass pairing is preserved without a file-existence gate.
    add(id, present, { blackbox, testFile: file, testNamePresent: present });
  }
}

const failed = Object.entries(results).filter(([, value]) => !value.pass).map(([id]) => id);
const allPass = failed.length === 0;
emit({ repo, version, status: allPass ? 'PASS' : 'FAIL', failed, results }, expected === 'none' || (expected === 'pass' && allPass) || (expected === 'fail' && !allPass) ? 0 : 1);
