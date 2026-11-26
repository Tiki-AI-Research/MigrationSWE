import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const BATCH_ID = 'BATCH-F2P-0003';
export const OBSERVATION_IDS = [
  'F2P-050', 'F2P-066', 'F2P-049', 'F2P-051', 'F2P-052', 'F2P-053',
  'F2P-054', 'F2P-055', 'F2P-056', 'F2P-057', 'F2P-058', 'F2P-059',
  'F2P-060', 'F2P-061', 'F2P-062', 'F2P-063', 'F2P-064', 'F2P-065',
  'F2P-067', 'F2P-068', 'F2P-069', 'F2P-070', 'F2P-071', 'F2P-072',
];

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PRELOAD = path.join(HERE, 'preload.mjs');
const TSX_FALLBACKS = [
  process.env.FT_TSX,
  '/opt/migration/dependencies/node_modules/.bin/tsx',
].filter(Boolean);

const TOP_LEVEL_COMMANDS = [
  'sync', 'search', 'list', 'show', 'stats', 'viz', 'classify',
  'classify-domains', 'model', 'categories', 'domains', 'folders', 'index',
  'auth', 'status', 'path', 'paths', 'recent', 'ls', 'tree', 'find', 'grep',
  'cat', 'head', 'meta', 'open', 'panel', 'codex', 'tab', 'reveal', 'pwd',
  'context', 'link', 'links', 'backlinks', 'tags', 'tagged', 'new', 'append',
  'note', 'rename', 'cd', 'back', 'current', 'sample', 'fetch-media', 'md',
  'wiki', 'ask', 'lint', 'possible', 'ideas', 'seeds', 'repos', 'frames',
  'skill', 'library', 'commands', 'app', 'install', 'bookmarks',
];

const POSITIVE_INTEGER_COMMANDS = [
  ['recent', '--limit', '0'],
  ['ls', '--limit', '-1'],
  ['find', 'q', '--limit', '1.5'],
  ['grep', 'q', '--limit', 'abc'],
  ['context', '--limit', '0'],
  ['head', 'f', '--lines', '0'],
];

function findTsx(repo) {
  const local = path.join(repo, 'node_modules', '.bin', 'tsx');
  return [local, ...TSX_FALLBACKS].find((candidate) => fs.existsSync(candidate)) || null;
}

function findCliEntry(repo) {
  const dist = path.join(repo, 'dist', 'cli.js');
  if (fs.existsSync(dist)) return { kind: 'dist', path: dist };
  const src = path.join(repo, 'src', 'cli.ts');
  if (fs.existsSync(src)) return { kind: 'src', path: src };
  return null;
}

function parseModules(logPath) {
  let text = '';
  try {
    text = fs.readFileSync(logPath, 'utf8');
  } catch {
    return [];
  }
  return text
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}

function frameworkFromModules(entries) {
  const commander = entries.find(
    (entry) => entry.specifier === 'commander' || /\/node_modules\/commander\//.test(entry.url || ''),
  );
  const oclif = entries.find(
    (entry) => entry.specifier === '@oclif/core' || /\/node_modules\/@oclif\/core\//.test(entry.url || ''),
  );
  let oclifVersion = null;
  if (oclif?.url) {
    try {
      const resolved = fileURLToPath(oclif.url);
      const marker = `${path.sep}node_modules${path.sep}@oclif${path.sep}core${path.sep}`;
      const index = resolved.indexOf(marker);
      if (index >= 0) {
        const pkgPath = `${resolved.slice(0, index)}${marker}package.json`;
        oclifVersion = JSON.parse(fs.readFileSync(pkgPath, 'utf8')).version || null;
      }
    } catch {
      oclifVersion = null;
    }
  }
  return { commander: Boolean(commander), oclif: Boolean(oclif), oclifVersion };
}

export function runCli(repo, args, options = {}) {
  const home = options.home || fs.mkdtempSync(path.join(os.tmpdir(), 'ft-f2p-0003-'));
  const data = path.join(home, 'data');
  const moduleLog = path.join(home, 'modules.log');
  const entry = findCliEntry(repo);
  if (!entry) throw new Error(`no CLI entry under ${repo}`);
  const env = {
    ...process.env,
    HOME: home,
    FT_DATA_DIR: data,
    FT_LIBRARY_DIR: path.join(home, 'library'),
    FT_COMMANDS_DIR: path.join(home, 'commands'),
    FT_MODULE_LOG: moduleLog,
    ...(options.env || {}),
  };
  let result;
  if (entry.kind === 'dist') {
    result = spawnSync(process.execPath, ['--import', PRELOAD, entry.path, ...args], {
      cwd: home,
      encoding: 'utf8',
      env,
      input: options.input,
      timeout: options.timeout || 60000,
    });
  } else {
    const tsx = findTsx(repo);
    if (!tsx) throw new Error(`no tsx available to execute ${entry.path}`);
    env.NODE_OPTIONS = [env.NODE_OPTIONS, `--import=${PRELOAD}`].filter(Boolean).join(' ');
    result = spawnSync(tsx, [entry.path, ...args], {
      cwd: home,
      encoding: 'utf8',
      env,
      input: options.input,
      timeout: options.timeout || 60000,
    });
  }
  const modules = parseModules(moduleLog);
  const framework = frameworkFromModules(modules);
  if (!options.keepHome) fs.rmSync(home, { recursive: true, force: true });
  return {
    status: result.status,
    signal: result.signal,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    framework,
    entry: entry.kind,
    home: options.keepHome ? home : undefined,
  };
}

function packageFacts(repo) {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(repo, 'package.json'), 'utf8'));
    return {
      oclifDependency: pkg.dependencies?.['@oclif/core'] || null,
      commanderDependency: pkg.dependencies?.commander || null,
      bin: pkg.bin || {},
    };
  } catch {
    return { oclifDependency: null, commanderDependency: null, bin: {} };
  }
}

function repeatedScalarChecks(repo) {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-repeated-options-'));
  try {
    for (const name of ['a.md', 'b.md', 'c.md']) fs.writeFileSync(path.join(fixture, name), name);
    const repeated = runCli(repo, ['recent', '--repo', fixture, '--limit', '1', '--limit', '2', '--json']);
    let recentCount = null;
    try { recentCount = JSON.parse(repeated.stdout).recentFiles.length; } catch {}
    const invalid = runCli(repo, ['recent', '--repo', fixture, '--limit', '0', '--limit', '2', '--json']);
    return [
      ['repeated scalar options preserve the last value', repeated.status === 0 && recentCount === 2],
      ['overridden non-positive values still fail validation with help', invalid.status !== 0 &&
        /positive integer|greater than or equal to 1|at least 1/i.test(invalid.stderr) && /usage/i.test(invalid.stderr)],
    ];
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
}

function pairedBooleanChecks(repo) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-paired-options-'));
  try {
    const prefix = ['possible', 'nightly', 'install', '--no-launchd', '--no-load'];
    const enabled = runCli(repo, [...prefix, '--no-defaults', '--defaults'], { home, keepHome: true });
    const disabled = runCli(repo, [...prefix, '--defaults', '--no-defaults'], { home, keepHome: true });
    return [
      ['paired boolean options honor a final --defaults', enabled.status === 0 &&
        /Saved nightly Possible schedule/.test(enabled.stdout)],
      ['paired boolean options honor a final --no-defaults', disabled.status !== 0 &&
        /--no-defaults requires --seed or --seed-artifact/.test(disabled.stdout)],
    ];
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
}

export function usageBoundaryChecks(repo) {
  const checks = [];
  const hasUsageError = (result) => {
    const help = result.stderr.search(/^\s*usage(?:\s*:|\s*$)/im);
    return Number.isInteger(result.status) && result.status !== 0 && result.stdout.trim() === '' &&
      help > 0 && result.stderr.slice(0, help).trim().length > 0;
  };
  // These errors happen before a known command's run/catch methods. They
  // still need stderr help, regardless of the framework's error wording.
  for (const args of [['unknown-ft-command'], ['--unknown-ft-flag'], ['seeds', 'unknown-ft-command']]) {
    const result = runCli(repo, args);
    checks.push([`${args.join(' ')} reports an error followed by help`,
      hasUsageError(result)]);
  }
  // A boolean cannot consume an attached value as a positional argument.
  for (const args of [['open', '--no-launch=false'], ['show', '--json=false']]) {
    const result = runCli(repo, args);
    checks.push([`${args.join(' ')} rejects the boolean value before its action`,
      hasUsageError(result)]);
  }
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-literal-option-'));
  try {
    const library = path.join(home, 'library');
    const name = '--json=literal.md';
    const content = '# Literal filename\n\nFlag-like text is document data.\n';
    fs.mkdirSync(library, { recursive: true });
    fs.writeFileSync(path.join(library, name), content);
    const result = runCli(repo, ['cat', '--json', '--', name], { home, keepHome: true });
    let actual = null;
    try { actual = JSON.parse(result.stdout).content; } catch {}
    checks.push(['-- preserves a flag-like filename as document data',
      result.status === 0 && actual === content]);
    const appended = runCli(repo,
      ['append', path.join(library, name), '--content', '--json=body', '--json'], { home, keepHome: true });
    checks.push(['value-taking flags preserve flag-like document content',
      appended.status === 0 && fs.readFileSync(path.join(library, name), 'utf8') === `${content}--json=body\n`]);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
  return checks;
}

function targetFramework(repo) {
  const facts = packageFacts(repo);
  let trace;
  try {
    trace = runCli(repo, ['--version'], { timeout: 60000 });
  } catch (error) {
    return { ok: false, detail: String(error?.message || error) };
  }
  const version = String(facts.oclifDependency || '');
  const ok =
    trace.status === 0 &&
    /^\d+\.\d+\.\d+$/.test(trace.stdout.trim()) &&
    trace.framework.oclif &&
    !trace.framework.commander &&
    version.startsWith('4.0.0') &&
    !facts.commanderDependency;
  return {
    ok,
    detail: ok
      ? `oclif ${trace.framework.oclifVersion || version} loaded from ${trace.entry} execution; commander absent`
      : `status=${trace.status} stdout=${JSON.stringify(trace.stdout.trim())} framework=${JSON.stringify(trace.framework)} manifest=${JSON.stringify({ oclif: facts.oclifDependency, commander: facts.commanderDependency })}`,
    trace,
  };
}

function runTsxTest(repo, file, pattern, timeout = 180000) {
  const tsx = findTsx(repo);
  if (!tsx) return { ok: false, detail: 'tsx runner is unavailable' };
  if (!fs.existsSync(path.join(repo, file))) return { ok: false, detail: `${file} does not exist` };
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-f2p-0003-test-'));
  const args = ['--test'];
  if (pattern) args.push('--test-name-pattern', pattern);
  args.push(file);
  const result = spawnSync(tsx, args, {
    cwd: repo,
    encoding: 'utf8',
    timeout,
    env: {
      ...process.env,
      HOME: home,
      FT_DATA_DIR: path.join(home, 'data'),
      FT_LIBRARY_DIR: path.join(home, 'library'),
      FT_COMMANDS_DIR: path.join(home, 'commands'),
    },
  });
  fs.rmSync(home, { recursive: true, force: true });
  const pass = Number((/# pass (\d+)/.exec(result.stdout || '') || [])[1] || 0);
  const fail = Number((/# fail (\d+)/.exec(result.stdout || '') || [])[1] || 0);
  const ok = result.status === 0 && pass >= 1 && fail === 0;
  return {
    ok,
    detail: ok
      ? `${file}${pattern ? ` [${pattern}]` : ''} passed (${pass} passing)`
      : `${file}${pattern ? ` [${pattern}]` : ''} failed: status=${result.status} pass=${pass} fail=${fail} stderr=${JSON.stringify((result.stderr || '').slice(-800))}`,
  };
}

function requiredCliContract(repo) {
  const checks = [];
  const version = runCli(repo, ['-V']);
  checks.push(['-V prints package version', version.status === 0 && /^\d+\.\d+\.\d+$/.test(version.stdout.trim())]);
  const missingArg = runCli(repo, ['show']);
  checks.push([
    'show reports Missing 1 required arg',
    missingArg.status !== 0 && /Missing 1 required arg/.test(missingArg.stderr),
  ]);
  const requiredOption = runCli(repo, ['seeds', 'create']);
  checks.push([
    "seeds create reports required --artifact",
    requiredOption.status !== 0 && /required option '--artifact <id\.\.\.>' not specified/.test(requiredOption.stderr),
  ]);
  const failed = checks.filter(([, condition]) => !condition).map(([label]) => label);
  return {
    ok: failed.length === 0,
    detail: failed.length === 0
      ? 'built-CLI version, missing-argument, and required-option assertions passed'
      : `built-CLI assertions failed: ${failed.join(', ')}`,
  };
}

function makeMemo() {
  const values = new Map();
  return (key, factory) => {
    if (!values.has(key)) values.set(key, factory());
    return values.get(key);
  };
}

function pipedCurrentUpdate(repo) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-stdin-contract-'));
  try {
    const source = path.join(home, 'library', 'notes', 'stdin.md');
    const manifest = path.join(home, 'context.json');
    const content = '# Updated\n\nPiped Markdown stays intact.\n';
    fs.mkdirSync(path.dirname(source), { recursive: true });
    fs.writeFileSync(source, '# Before\n');
    fs.writeFileSync(manifest, JSON.stringify({
      updatedAt: '2026-01-02T00:00:00.000Z',
      activeDocument: { title: 'stdin', path: source, kind: 'wiki', contentMode: 'rendered', contentPath: source },
    }));
    const updated = runCli(repo, ['current', 'update', '--manifest', manifest, '--stdin', '--force'], {
      home, keepHome: true, input: content, timeout: 30000,
    });
    return updated.status === 0 && fs.readFileSync(source, 'utf8') === content;
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
}

function summarize(frameworks, checks) {
  const failed = checks.filter(([, condition]) => !condition).map(([label]) => label);
  if (failed.length > 0) {
    return {
      relation: 'fail',
      detail: `${frameworks.ok ? frameworks.detail : frameworks.detail}; failed: ${failed.join('; ')}`,
    };
  }
  return { relation: 'pass', detail: frameworks.detail };
}

function combine(frameworks, checks) {
  const outcome = summarize(frameworks, checks);
  const ok = frameworks.ok && outcome.relation === 'pass';
  return { status: ok ? 'PASS' : 'FAIL', detail: outcome.detail };
}

function directObservation(factory) {
  return factory;
}

function suiteObservation(file, pattern, extra = []) {
  return (repo, memo) => {
    const framework = memo('framework', () => targetFramework(repo));
    if (!framework.ok) return { status: 'FAIL', detail: framework.detail };
    const cliContract = memo('cliContract', () => requiredCliContract(repo));
    if (!cliContract.ok) return { status: 'FAIL', detail: cliContract.detail };
    const test = runTsxTest(repo, file, pattern);
    if (!test.ok) return { status: 'FAIL', detail: test.detail };
    const checks = [];
    for (const [label, fn] of extra) {
      try {
        checks.push([label, Boolean(fn(repo))]);
      } catch (error) {
        checks.push([label, false, String(error?.message || error)]);
      }
    }
    const failed = checks.filter(([, condition]) => !condition);
    if (failed.length > 0) {
      return { status: 'FAIL', detail: `${test.detail}; extra checks failed: ${failed.map(([label]) => label).join(', ')}` };
    }
    return { status: 'PASS', detail: `${framework.detail}; ${cliContract.detail}; ${test.detail}` };
  };
}

const OBSERVATIONS = {
  'F2P-050': suiteObservation('tests/cli.test.ts', 'ft search, stats, and status expose --json'),
  'F2P-066': suiteObservation('tests/cli.test.ts', 'compareVersions: minor beats patch'),
  'F2P-052': suiteObservation('tests/cli.test.ts', 'ft paths, current.*command groups are registered'),
  'F2P-053': suiteObservation('tests/cli.test.ts', 'ft install app command is registered'),
  'F2P-054': suiteObservation('tests/cli.test.ts', 'ft navigation aliases inspect Field Theory library markdown'),
  'F2P-057': suiteObservation('tests/engine.test.ts', 'resolveEngine: override rejects unknown engine'),
  'F2P-059': suiteObservation('tests/engine.test.ts', 'invokeEngineAsync: stderr is bounded under STDERR_TAIL_BYTES'),
  'F2P-060': suiteObservation('tests/engine.test.ts', 'resolveEngine: override returns named engine when binary is on PATH'),
  'F2P-062': suiteObservation('tests/engine.test.ts', 'redactSecrets: leaves normal error text untouched'),
  'F2P-063': suiteObservation('tests/cli.test.ts', 'ft path: prints only the data directory'),
  'F2P-064': suiteObservation('tests/engine.test.ts', 'resolveEngine: override rejects prototype keys like __proto__'),
  'F2P-067': suiteObservation('tests/cli.test.ts', 'repo workflow state is not a CLI command'),
  'F2P-068': suiteObservation('tests/cli.test.ts', 'ft current reports missing context without a stack trace'),
  'F2P-069': suiteObservation('tests/cli.test.ts', 'ft paths --json prints canonical roots'),
  'F2P-070': suiteObservation('tests/cli.test.ts', 'compareVersions: older patch returns negative'),
  'F2P-071': suiteObservation('tests/cli.test.ts', 'ft current update edits the active Library document without passing its path'),
  'F2P-072': suiteObservation('tests/cli.test.ts', 'ft current update rejects Field Theory session cache files as editable sources'),

  'F2P-049': directObservation((repo, memo) => {
    const framework = memo('framework', () => targetFramework(repo));
    const ideas = runCli(repo, ['ideas', '--help']);
    const seed = runCli(repo, ['possible', 'seed', 'list']);
    const bookmarks = runCli(repo, ['bookmarks', 'enable', '--help']);
    return combine(framework, [
      ['ft ideas --help resolves possible help', ideas.status === 0 && /possible/i.test(ideas.stdout)],
      ['ft possible seed list forwards to seeds list', seed.status === 0 && /seeds/i.test(seed.stdout)],
      ['ft bookmarks enable --help resolves sync help', bookmarks.status === 0 && /sync bookmarks/i.test(bookmarks.stdout)],
    ]);
  }),

  'F2P-051': directObservation((repo, memo) => {
    const framework = memo('framework', () => targetFramework(repo));
    const noMedia = runCli(repo, ['sync', '--no-media']);
    const media = runCli(repo, ['sync', '--media']);
    return combine(framework, [
      ['ft sync --no-media is accepted and reaches the handler', !/(?:nonexistent|unknown|unexpected)[^\n]{0,20}(?:flag|option)[^\n]{0,20}--media/i.test(noMedia.stderr) && /x\.com|browser/i.test(`${noMedia.stdout}\n${noMedia.stderr}`)],
      ['ft sync --media is rejected as an oclif nonexistent flag naming both spellings', media.status !== 0 && /(?:nonexistent|unknown|unexpected)[^\n]{0,20}(?:flag|option)[^\n]{0,20}--media/i.test(media.stderr) && /--no-media/.test(media.stderr)],
    ]);
  }),

  'F2P-055': directObservation((repo, memo) => {
    const framework = memo('framework', () => targetFramework(repo));
    const nightly = runCli(repo, ['possible', 'nightly', 'list']);
    const seed = runCli(repo, ['possible', 'seed', 'list']);
    const possibleHelp = runCli(repo, ['possible', '--help']);
    return combine(framework, [
      ['ft possible nightly list resolves the space-separated topic', nightly.status === 0],
      ['ft possible seed list rewrites to ft seeds list', seed.status === 0],
      ['possible help uses space-separated topics', /possible nightly/.test(possibleHelp.stdout) && !/possible:nightly/.test(possibleHelp.stdout)],
    ]);
  }),

  'F2P-056': directObservation((repo, memo) => {
    const framework = memo('framework', () => targetFramework(repo));
    const sync = runCli(repo, ['sync', '--cookies', 'ct0', 'auth-token', '--rebuild', '--continue']);
    const seed = runCli(repo, ['seeds', 'create', '--artifact', 'artifact-one', 'artifact-two']);
    return combine(framework, [
      ['variadic --cookies keeps the following --rebuild flag', sync.status !== 0 && /cannot be used together/.test(sync.stderr) && !/Unexpected argument|too many arguments/i.test(sync.stderr)],
      ['sync handler is reached with browser-session cookies', /x\.com|browser/i.test(`${sync.stdout}\n${sync.stderr}`)],
      ['variadic --artifact consumes multiple values', seed.status !== 0 && /Artifact not found: artifact-one/.test(`${seed.stdout}\n${seed.stderr}`) && !/Unexpected argument|too many arguments/i.test(seed.stderr)],
      ['current update --stdin preserves Markdown for its action', pipedCurrentUpdate(repo)],
      ...repeatedScalarChecks(repo),
      ...pairedBooleanChecks(repo),
      ...usageBoundaryChecks(repo),
    ]);
  }),

  'F2P-058': directObservation((repo, memo) => {
    const framework = memo('framework', () => targetFramework(repo));
    const help = runCli(repo, ['--help']);
    const missing = TOP_LEVEL_COMMANDS.filter((command) => !new RegExp(`\\b${command.replace(/[-]/g, '\\-')}\\b`).test(help.stdout));
    const noMedia = runCli(repo, ['sync', '--no-media']);
    const media = runCli(repo, ['sync', '--media']);
    return combine(framework, [
      ['top-level help exits 0 and lists every command', help.status === 0 && missing.length === 0],
      ['ft sync --no-media is accepted', !/Nonexistent flag: --media/.test(noMedia.stderr)],
      ['ft sync --media is an oclif nonexistent flag naming both spellings', media.status !== 0 && /Nonexistent flag: --media/.test(media.stderr) && /--no-media/.test(media.stderr)],
    ]);
  }),

  'F2P-065': directObservation((repo, memo) => {
    const framework = memo('framework', () => targetFramework(repo));
    const bogus = runCli(repo, ['sync', '--bogus']);
    const invalid = POSITIVE_INTEGER_COMMANDS.map((args) => {
      const result = runCli(repo, args);
      return [`ft ${args.join(' ')} rejects non-positive integer`, result.status !== 0 && /positive integer|greater than or equal to 1|must be (?:an? )?(?:positive )?integer|invalid[^\n]{0,20}integer|at least 1/i.test(result.stderr)];
    });
    return combine(framework, [
      ['ft sync --bogus reports an oclif nonexistent flag with USAGE', bogus.status !== 0 && /Nonexistent flag: --bogus/.test(bogus.stderr) && /USAGE/.test(bogus.stderr)],
      ...invalid,
    ]);
  }),

  'F2P-061': directObservation((repo, memo) => {
    const framework = memo('framework', () => targetFramework(repo));
    const cliContract = memo('cliContract', () => requiredCliContract(repo));
    const missingArg = runCli(repo, ['show']);
    const unknown = runCli(repo, ['sync', '--bogus']);
    const missingValue = runCli(repo, ['recent', '--limit']);
    const requiredOption = runCli(repo, ['seeds', 'create']);
    return combine(framework, [
      ['built-CLI version, argument, and required-option assertions pass', cliContract.ok],
      ['ft show error is followed by USAGE', missingArg.status !== 0 && /Missing 1 required arg/.test(missingArg.stderr) && /USAGE/.test(missingArg.stderr)],
      ['ft sync --bogus error is followed by USAGE', unknown.status !== 0 && /Nonexistent flag: --bogus/.test(unknown.stderr) && /USAGE/.test(unknown.stderr)],
      ['ft recent --limit error is followed by USAGE', missingValue.status !== 0 && /expects a value/.test(missingValue.stderr) && /USAGE/.test(missingValue.stderr)],
      ['ft seeds create error is followed by USAGE', requiredOption.status !== 0 && /required option '--artifact <id\.\.\.>' not specified/.test(requiredOption.stderr) && /USAGE/.test(requiredOption.stderr)],
    ]);
  }),
};

export function runProbe(repo, side) {
  const memo = makeMemo();
  const results = OBSERVATION_IDS.map((observationId) => {
    const handler = OBSERVATIONS[observationId];
    if (!handler) {
      return { observation_id: observationId, status: 'INFRA_ERROR', detail: 'no probe is registered' };
    }
    try {
      const result = handler(repo, memo);
      return { observation_id: observationId, status: result.status, detail: result.detail };
    } catch (error) {
      return { observation_id: observationId, status: 'INFRA_ERROR', detail: String(error?.stack || error) };
    }
  });
  return {
    schema_version: 'migration-asset-probe:v1',
    batch_id: BATCH_ID,
    side,
    repo,
    results,
  };
}

export function writeProbeReport(report, outputPath) {
  fs.writeFileSync(outputPath, `${JSON.stringify(report, null, 2)}\n`);
}
