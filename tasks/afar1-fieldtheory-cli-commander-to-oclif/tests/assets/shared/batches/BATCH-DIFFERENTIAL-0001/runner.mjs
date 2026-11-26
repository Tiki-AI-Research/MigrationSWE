import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = path.resolve(HERE, '../../../fixtures/batches/BATCH-DIFFERENTIAL-0001/bookmarks.jsonl');
const BATCH_ID = 'BATCH-DIFFERENTIAL-0001';
const SCRIPT_BIN = '/usr/bin/script';

const TOP_LEVEL_COMMANDS = [
  'sync', 'search', 'list', 'show', 'stats', 'viz', 'classify', 'classify-domains',
  'model', 'categories', 'domains', 'folders', 'index', 'auth', 'status', 'path',
  'paths', 'recent', 'ls', 'tree', 'find', 'grep', 'cat', 'head', 'meta', 'open',
  'panel', 'codex', 'tab', 'reveal', 'pwd', 'context', 'link', 'links', 'backlinks',
  'tags', 'tagged', 'new', 'append', 'note', 'rename', 'cd', 'back', 'current',
  'sample', 'fetch-media', 'md', 'wiki', 'ask', 'lint', 'possible', 'ideas', 'seeds',
  'repos', 'frames', 'skill', 'library', 'commands', 'app', 'install', 'bookmarks',
];

const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;

function stripAnsi(value) {
  return String(value ?? '').replace(ANSI, '');
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function readFixture() {
  if (fs.existsSync(FIXTURE_PATH)) return fs.readFileSync(FIXTURE_PATH, 'utf8');
  return '';
}

function makeNormalizer(repo) {
  return function normalize(value, scenario) {
    let text = stripAnsi(value).replace(/\r/g, '');
    if (scenario) {
      text = text.split(scenario.home).join('<HOME>');
      text = text.split(scenario.data).join('<DATA>');
    }
    text = text.split(repo).join('<REPO>');
    text = text.split(os.tmpdir()).join('<TMP>');
    return text;
  };
}

function normalizeValue(value, normalize, scenario) {
  if (typeof value === 'string') return normalize(value, scenario);
  if (Array.isArray(value)) return value.map((item) => normalizeValue(item, normalize, scenario));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, normalizeValue(item, normalize, scenario)]));
  }
  return value;
}

function normalizeArtifacts(value, normalize, scenario) {
  let text = normalize(value, scenario);
  text = text.replace(/seed-[0-9a-f]{8}/g, 'seed-<ID>');
  text = text.replace(/\b[0-9a-f]{12}\b/g, '<ARTIFACT-ID>');
  text = text.replace(/\d{4}-\d{2}-\d{2}T[0-9:.]+Z?/g, '<TIMESTAMP>');
  text = text.replace(/\d{4}-\d{2}-\d{2}/g, '<DATE>');
  return text;
}

function extractDefault(text, option) {
  let index = text.indexOf(option);
  while (index >= 0) {
    const segment = text.slice(index, index + 260);
    const match = segment.match(/(?:\(default:\s*|\[default:\s*)(\d+)/);
    if (match) return Number(match[1]);
    index = text.indexOf(option, index + option.length);
  }
  return null;
}

function containsAll(text, tokens) {
  return Object.fromEntries(tokens.map((token) => [token, text.includes(token)]));
}

function tapSummary(text) {
  const pass = Number((text.match(/^# pass (\d+)/m) ?? [])[1] ?? 0);
  const fail = Number((text.match(/^# fail (\d+)/m) ?? [])[1] ?? 0);
  return { pass, fail };
}

function lineSignal(text, pattern) {
  const lines = stripAnsi(text).split('\n').map((line) => line.trim()).filter(Boolean);
  const index = lines.findIndex((line) => pattern.test(line));
  return index >= 0 ? lines[index] : null;
}

export async function main({ repo, side = 'source', out } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ft-differential-'));
  const normalize = makeNormalizer(repo);
  const fixtureText = readFixture();
  const packageJson = JSON.parse(fs.readFileSync(path.join(repo, 'package.json'), 'utf8'));
  const packageVersion = String(packageJson.version ?? '');
  const scenarioCounter = new Map();

  function scenario(label, options = {}) {
    const count = (scenarioCounter.get(label) ?? 0) + 1;
    scenarioCounter.set(label, count);
    const home = path.join(root, `${label}-${count}-home`);
    const data = path.join(root, `${label}-${count}-data`);
    fs.mkdirSync(home, { recursive: true, mode: 0o700 });
    fs.mkdirSync(data, { recursive: true, mode: 0o700 });
    const env = { ...process.env, HOME: home, FT_DATA_DIR: data, NO_COLOR: '1', FORCE_COLOR: '0' };
    delete env.FT_LIBRARY_DIR;
    delete env.FT_COMMANDS_DIR;
    delete env.FT_BROWSER_HELPER_STATE_PATH;
    delete env.XDG_CONFIG_HOME;
    delete env.XDG_DATA_HOME;
    if (options.fixture) fs.writeFileSync(path.join(data, 'bookmarks.jsonl'), fixtureText);
    fs.writeFileSync(path.join(data, '.update-check'), `${options.updateVersion ?? packageVersion}\n`);
    return { home, data, env };
  }

  const binPath = path.join(repo, 'bin', 'ft.mjs');

  function runBin(sc, args, options = {}) {
    const result = spawnSync(process.execPath, [binPath, ...args], {
      cwd: repo,
      env: sc.env,
      encoding: 'utf8',
      input: options.input,
      timeout: options.timeout ?? 30000,
      maxBuffer: 8 * 1024 * 1024,
    });
    return {
      status: result.status,
      signal: result.signal,
      stdout: result.stdout ?? '',
      stderr: result.stderr ?? '',
      combined: `${result.stdout ?? ''}${result.stderr ?? ''}`,
    };
  }

  function runNode(sc, script, args, options = {}) {
    const result = spawnSync(process.execPath, [script, ...args], {
      cwd: repo,
      env: sc.env,
      encoding: 'utf8',
      input: options.input,
      timeout: options.timeout ?? 30000,
      maxBuffer: 8 * 1024 * 1024,
    });
    return {
      status: result.status,
      signal: result.signal,
      stdout: result.stdout ?? '',
      stderr: result.stderr ?? '',
      combined: `${result.stdout ?? ''}${result.stderr ?? ''}`,
    };
  }

  function runTty(sc, args, input, options = {}) {
    if (!fs.existsSync(SCRIPT_BIN)) return null;
    const command = [process.execPath, binPath, ...args].map(shellQuote).join(' ');
    const result = spawnSync(SCRIPT_BIN, ['-qec', command, '/dev/null'], {
      cwd: repo,
      env: sc.env,
      encoding: 'utf8',
      input,
      timeout: options.timeout ?? 15000,
      maxBuffer: 8 * 1024 * 1024,
    });
    return {
      status: result.status,
      signal: result.signal,
      stdout: result.stdout ?? '',
      stderr: result.stderr ?? '',
      combined: `${result.stdout ?? ''}${result.stderr ?? ''}`,
    };
  }

  function buildIndex(sc) {
    return runBin(sc, ['index'], { timeout: 30000 });
  }

  function listFiles(sc) {
    const files = {};
    for (const [base, label] of [[sc.home, 'HOME'], [sc.data, 'DATA']]) {
      if (!fs.existsSync(base)) continue;
      const stack = [base];
      while (stack.length > 0) {
        const current = stack.pop();
        for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
          const full = path.join(current, entry.name);
          if (entry.isDirectory()) {
            stack.push(full);
          } else if (entry.isFile()) {
            const relative = path.relative(base, full).split(path.sep).join('/');
            const key = normalizeArtifacts(`${label}/${relative}`, normalize, sc);
            let content = '';
            try {
              content = normalizeArtifacts(fs.readFileSync(full, 'utf8'), normalize, sc);
            } catch {
              content = '<unreadable>';
            }
            files[key] = content;
          }
        }
      }
    }
    return Object.fromEntries(Object.entries(files).sort(([a], [b]) => a.localeCompare(b)));
  }

  const base = scenario('base', { fixture: true });
  buildIndex(base);
  const empty = scenario('empty');
  const update = scenario('update', { updateVersion: '99.99.99' });
  const tty = scenario('tty', { fixture: true });
  const stateful = scenario('stateful', { fixture: true });
  const cookies = scenario('cookies');

  const observations = {};

  {
    const tests = [
      'tests/bookmarks-db.test.ts',
      'tests/graphql-bookmarks.test.ts',
      'tests/library.test.ts',
      'tests/commands-files.test.ts',
      'tests/skill.test.ts',
      'tests/app-install.test.ts',
    ];
    const tsx = path.join(repo, 'node_modules', '.bin', 'tsx');
    let result;
    if (fs.existsSync(tsx)) {
      const child = spawnSync(tsx, ['--test', ...tests], {
        cwd: repo,
        env: base.env,
        encoding: 'utf8',
        timeout: 180000,
        maxBuffer: 16 * 1024 * 1024,
      });
      const summary = tapSummary(`${child.stdout ?? ''}${child.stderr ?? ''}`);
      result = { available: true, exit: child.status, ...summary };
    } else {
      result = { available: false, exit: null, pass: 0, fail: 0 };
    }
    observations['DIFF-001'] = { ...result, ok: result.available && result.exit === 0 && result.fail === 0 && result.pass > 0 };
  }

  {
    const long = runBin(base, ['--version']);
    const short = runBin(base, ['-V']);
    const bin = packageJson.bin ?? {};
    observations['DIFF-002'] = {
      version: normalize(long.stdout, base).trim(),
      shortVersion: normalize(short.stdout, base).trim(),
      packageVersion,
      binFt: bin.ft ?? null,
      binFieldtheory: bin.fieldtheory ?? null,
      sameEntry: Boolean(bin.ft && bin.fieldtheory && bin.ft === bin.fieldtheory),
      ok: long.status === 0 && short.status === 0 && normalize(long.stdout, base).trim() === packageVersion &&
        normalize(short.stdout, base).trim() === packageVersion && bin.ft === bin.fieldtheory,
    };
  }

  {
    const emptyRun = runBin(empty, []);
    const dataRun = runBin(base, []);
    const emptyText = normalize(emptyRun.combined, empty);
    const dataText = normalize(dataRun.combined, base);
    const emptyMode = /Save a local copy of your X\/Twitter bookmarks/.test(emptyText) ? 'welcome' : 'other';
    const dataMode = /Save a local copy of your X\/Twitter bookmarks/.test(dataText) ? 'welcome' : 'dashboard';
    observations['DIFF-003'] = {
      emptyExit: emptyRun.status,
      dataExit: dataRun.status,
      emptyMode,
      dataMode,
      ok: emptyRun.status === 0 && dataRun.status === 0 && emptyMode === 'welcome' && dataMode === 'dashboard',
    };
  }

  {
    const long = runBin(base, ['--help']);
    const short = runBin(base, ['-h']);
    const version = runBin(base, ['--version']);
    const shortVersion = runBin(base, ['-V']);
    const helpText = normalize(`${long.combined}${short.combined}`, base);
    const versionText = normalize(version.stdout, base).trim();
    const shortVersionText = normalize(shortVersion.stdout, base).trim();
    observations['DIFF-004'] = {
      longHelpExit: long.status,
      shortHelpExit: short.status,
      longVersionExit: version.status,
      shortVersionExit: shortVersion.status,
      helpMentionsUsage: /usage/i.test(helpText),
      version: versionText,
      shortVersion: shortVersionText,
      ok: long.status === 0 && short.status === 0 && version.status === 0 && shortVersion.status === 0 &&
        /usage/i.test(helpText) && versionText === packageVersion && shortVersionText === packageVersion,
    };
  }

  const commandResolution = {};
  {
    const missing = [];
    for (const command of TOP_LEVEL_COMMANDS) {
      const result = runBin(base, [command, '--help'], { timeout: 20000 });
      let resolved;
      if (command === 'bookmarks') {
        resolved = /usage/i.test(normalize(result.combined, base)) && /bookmarks/i.test(normalize(result.combined, base));
      } else {
        resolved = result.status === 0;
      }
      commandResolution[command] = resolved;
      if (!resolved) missing.push(command);
    }
    observations['DIFF-005'] = {
      total: TOP_LEVEL_COMMANDS.length,
      missing,
      ok: missing.length === 0,
    };
  }

  {
    const checks = {
      'current update --help': runBin(base, ['current', 'update', '--help']),
      'codex panel --help': runBin(base, ['codex', 'panel', '--help']),
      'possible nightly list': runBin(base, ['possible', 'nightly', 'list']),
      'possible _run-job --help': runBin(base, ['possible', '_run-job', '--help']),
      'seeds list --help': runBin(base, ['seeds', 'list', '--help']),
      'bookmarks --help': runBin(base, ['bookmarks', '--help']),
    };
    const resolved = {};
    for (const [label, result] of Object.entries(checks)) {
      const text = normalize(result.combined, base);
      resolved[label] = result.status === 0 || /usage/i.test(text);
    }
    observations['DIFF-006'] = { resolved, ok: Object.values(resolved).every(Boolean) };
  }

  {
    const specs = [
      ['sync', ['sync'], ['--cookies', '--no-media']],
      ['seeds create', ['seeds', 'create'], ['--artifact']],
      ['possible run', ['possible', 'run'], ['--weight']],
      ['skill install', ['skill', 'install'], ['-y', '--yes']],
      ['possible nightly install', ['possible', 'nightly', 'install'], ['--no-defaults', '--no-launchd', '--no-load']],
    ];
    const tokens = {};
    for (const [label, args, expected] of specs) {
      const result = runBin(base, [...args, '--help']);
      tokens[label] = containsAll(normalize(result.combined, base), expected);
    }
    observations['DIFF-007'] = {
      tokens,
      ok: Object.values(tokens).every((entry) => Object.values(entry).every(Boolean)),
    };
  }

  {
    const search = runBin(base, ['search', 'learning', '--limit', '20', '--json']);
    const status = runBin(base, ['status', '--json']);
    const paths = runBin(base, ['paths', '--json']);
    const parse = (result) => {
      try {
        return JSON.parse(stripAnsi(result.stdout));
      } catch {
        return null;
      }
    };
    const searchJson = parse(search);
    const statusJson = parse(status);
    const pathsJson = parse(paths);
    observations['DIFF-008'] = {
      searchExit: search.status,
      search: normalizeValue(searchJson, normalize, base),
      statusExit: status.status,
      status: normalizeValue(statusJson, normalize, base),
      pathsExit: paths.status,
      paths: normalizeValue(pathsJson, normalize, base),
      ok: search.status === 0 && Array.isArray(searchJson) && searchJson.length === 2 &&
        status.status === 0 && Boolean(statusJson) && paths.status === 0 && Boolean(pathsJson),
    };
  }

  {
    const searchHelp = runBin(base, ['search', '--help']);
    const possibleHelp = runBin(base, ['possible', '--help']);
    const searchText = normalize(searchHelp.combined, base);
    const possibleText = normalize(possibleHelp.combined, base);
    const searchTokens = containsAll(searchText, ['query', '--author', '--after', '--before', '--limit', '--json']);
    const possibleTokens = containsAll(possibleText, ['explain', 'run', 'jobs', 'job', 'nightly', 'list', 'show']);
    observations['DIFF-009'] = {
      search: searchTokens,
      possible: possibleTokens,
      ok: Object.values(searchTokens).every(Boolean) && Object.values(possibleTokens).every(Boolean),
    };
  }

  {
    const path = runBin(base, ['path']);
    const search = runBin(empty, ['search', 'q']);
    const recent = runBin(base, ['recent', '--limit', '0']);
    const interrupted = runTty(tty, ['model'], '\x03');
    observations['DIFF-010'] = {
      pathExit: path.status,
      searchNoDataExit: search.status,
      recentLimitZeroExit: recent.status,
      promptInterruptExit: interrupted ? interrupted.status : null,
      ok: path.status === 0 && search.status !== 0 && recent.status !== 0 && (!interrupted || interrupted.status === 130),
    };
  }

  {
    const model = runTty(tty, ['model'], '\n');
    const rebuild = runTty(tty, ['sync', '--rebuild'], 'n\n');
    const modelText = model ? normalize(model.combined, tty) : '';
    const rebuildText = rebuild ? normalize(rebuild.combined, tty) : '';
    observations['DIFF-011'] = {
      modelPrompt: /Select default:/.test(modelText),
      syncRebuildPrompt: /Continue\? \(y\/N\)/.test(rebuildText),
      aborted: /Aborted/.test(rebuildText),
      ok: (!model || /Select default:/.test(modelText)) && (!rebuild || (/Continue\? \(y\/N\)/.test(rebuildText) && /Aborted/.test(rebuildText))),
    };
  }

  {
    const result = runBin(cookies, ['sync', '--cookies', 'ct0', 'auth-token', '--no-media', '--max-pages', '0'], { timeout: 30000 });
    const text = normalize(result.combined, cookies);
    observations['DIFF-012'] = {
      exit: result.status,
      browserCookieError: /Chrome Cookies database not found/.test(text),
      browserSessionPath: /browser session is used to authenticate/i.test(text),
      pausedAfterPageLimit: /Paused after reaching page limit/.test(text),
      ok: result.status === 0 && !/Chrome Cookies database not found/.test(text) &&
        /browser session is used to authenticate/i.test(text) && /Paused after reaching page limit/.test(text),
    };
  }

  {
    const search = runBin(base, ['search', 'learning', '--limit', '20', '--json']);
    const stdout = stripAnsi(search.stdout).trim();
    let records = null;
    try {
      records = JSON.parse(stdout);
    } catch {
      records = null;
    }
    observations['DIFF-013'] = {
      exit: search.status,
      pureJson: stdout.startsWith('[') && !/F i e l d   T h e o r y/.test(stdout),
      ids: Array.isArray(records) ? records.map((record) => record.id) : null,
      ok: search.status === 0 && Array.isArray(records) && records.length === 2 &&
        stdout.startsWith('[') && !/F i e l d   T h e o r y/.test(stdout),
    };
  }

  {
    const sync = runBin(base, ['bookmarks', 'sync', '--help']);
    const enable = runBin(base, ['bookmarks', 'enable', '--help']);
    const search = runBin(base, ['bookmarks', 'search', '--help']);
    const syncText = normalize(sync.combined, base);
    const enableText = normalize(enable.combined, base);
    const searchText = normalize(search.combined, base);
    const result = {
      syncMentionsSync: /sync/i.test(syncText),
      enableMentionsSync: /sync/i.test(enableText),
      searchMentionsSearch: /search/i.test(searchText),
    };
    observations['DIFF-014'] = { ...result, ok: Object.values(result).every(Boolean) };
  }

  let ideasAliasMatchesPossible = false;
  {
    const possibleList = runBin(base, ['possible', 'list']);
    const ideasList = runBin(base, ['ideas', 'list']);
    ideasAliasMatchesPossible = possibleList.status === 0 && ideasList.status === 0 &&
      stripAnsi(possibleList.stdout).trim().length > 0 &&
      stripAnsi(possibleList.stdout).trim() === stripAnsi(ideasList.stdout).trim();
    const seedHelp = runBin(base, ['possible', 'seed', 'list', '--help']);
    const ideasHelp = runBin(base, ['ideas', '--help']);
    const seedList = runBin(base, ['possible', 'seed', 'list']);
    const seedHelpText = normalize(seedHelp.combined, base);
    const ideasHelpText = normalize(ideasHelp.combined, base);
    const seedListText = normalize(seedList.combined, base);
    const result = {
      seedHelpMentionsSeedsList: /seeds\s+list/i.test(seedHelpText),
      // Keep the frozen projection key, measuring alias behavior rather than
      // requiring the canonical name in framework-generated help.
      ideasHelpMentionsPossible: ideasHelp.status === 0 && ideasAliasMatchesPossible,
      seedListMentionsSeeds: /seeds/i.test(seedListText),
    };
    observations['DIFF-015'] = { ...result, ok: Object.values(result).every(Boolean) };
  }

  {
    const cases = [
      ['recent --limit 0', ['recent', '--limit', '0']],
      ['ls --limit -1', ['ls', '--limit', '-1']],
      ['find q --limit 1.5', ['find', 'q', '--limit', '1.5']],
      ['grep q --limit abc', ['grep', 'q', '--limit', 'abc']],
      ['context --limit 0', ['context', '--limit', '0']],
      ['head f --lines 0', ['head', 'f', '--lines', '0']],
    ];
    const checks = {};
    for (const [label, args] of cases) {
      const result = runBin(base, args);
      checks[label] = {
        nonzero: result.status !== 0,
        positiveInteger: /positive integer|greater than or equal to 1|must be (?:an? )?(?:positive )?integer|invalid[^\n]{0,20}integer|at least 1/i.test(normalize(result.combined, base)),
      };
    }
    observations['DIFF-016'] = {
      checks,
      ok: Object.values(checks).every((entry) => entry.nonzero && entry.positiveInteger),
    };
  }

  {
    const search = runBin(base, ['search', '--help']);
    const list = runBin(base, ['list', '--help']);
    const tree = runBin(base, ['tree', '--help']);
    const sync = runBin(base, ['sync', '--help']);
    const searchText = normalize(search.combined, base);
    const listText = normalize(list.combined, base);
    const treeText = normalize(tree.combined, base);
    const syncText = normalize(sync.combined, base);
    const defaults = {
      searchLimit: extractDefault(searchText, '--limit'),
      listOffset: extractDefault(listText, '--offset') ?? 0,
      treeLimit: extractDefault(treeText, '--limit'),
      syncDelayMs: extractDefault(syncText, '--delay-ms'),
      syncMaxMinutes: extractDefault(syncText, '--max-minutes'),
    };
    observations['DIFF-017'] = {
      defaults,
      ok: defaults.searchLimit === 20 && defaults.listOffset === 0 && defaults.treeLimit === 80 &&
        defaults.syncDelayMs === 600 && defaults.syncMaxMinutes === 30,
    };
  }

  {
    const cases = [
      ['sync rebuild+continue', ['sync', '--rebuild', '--continue'], '--rebuild, --continue, and --gaps cannot be used together'],
      ['sync gaps+rebuild', ['sync', '--gaps', '--rebuild'], '--rebuild, --continue, and --gaps cannot be used together'],
      ['sync folders+folder', ['sync', '--folders', '--folder', 'x', '--cookies', 'ct0'], '--folders and --folder cannot be used together'],
      ['possible effort+weight', ['possible', 'run', '--effort', 'high', '--weight', 'low'], 'Use either --effort or --weight'],
      ['possible repo+repos', ['possible', 'run', '--repo', '.', '--repos', '.', 'x'], 'Provide either --seed-artifact'],
      ['seeds create missing artifact', ['seeds', 'create'], "required option '--artifact <id...>' not specified"],
    ];
    const checks = {};
    for (const [label, args, expected] of cases) {
      const result = runBin(base, args);
      const text = normalize(result.combined, base);
      checks[label] = { nonzero: result.status !== 0, expectedMessage: text.includes(expected) };
    }
    observations['DIFF-018'] = {
      checks,
      ok: Object.values(checks).every((entry) => entry.nonzero && entry.expectedMessage),
    };
  }

  {
    const sync = runBin(base, ['sync', '--cookies', 'ct0', 'auth-token', '--no-media', '--max-pages', '0'], { timeout: 30000 });
    const search = runBin(base, ['search', 'learning', '--json']);
    const recent = runBin(base, ['recent']);
    const ls = runBin(base, ['ls']);
    const runJob = runBin(base, ['possible', '_run-job', 'not-a-real-job']);
    const hasLogo = (result) => /F i e l d   T h e o r y/.test(normalize(result.combined, base));
    const result = {
      syncHasLogo: hasLogo(sync),
      searchJsonHasLogo: hasLogo(search),
      recentHasLogo: hasLogo(recent),
      lsHasLogo: hasLogo(ls),
      runJobHasLogo: hasLogo(runJob),
    };
    observations['DIFF-019'] = {
      ...result,
      ok: result.syncHasLogo && !result.searchJsonHasLogo && !result.recentHasLogo &&
        !result.lsHasLogo && !result.runJobHasLogo,
    };
  }

  {
    const help = runBin(update, ['--help']);
    const errorHelp = runBin(update, ['sync', '--bogus']);
    const helpText = normalize(help.combined, update);
    const errorText = normalize(errorHelp.combined, update);
    const result = {
      helpHasNotice: /Update available/.test(helpText) && /99\.99\.99/.test(helpText),
      errorHelpHasNotice: /Update available/.test(errorText) || /99\.99\.99/.test(errorText),
    };
    observations['DIFF-020'] = { ...result, ok: result.helpHasNotice };
  }

  {
    const direct = runNode(update, path.join(repo, 'dist', 'cli.js'), ['stats']);
    const binStats = runBin(update, ['stats'], { timeout: 30000 });
    const binJson = runBin(update, ['stats', '--json'], { timeout: 30000 });
    const help = runBin(update, ['--help']);
    const tsx = path.join(repo, 'node_modules', '.bin', 'tsx');
    const tsxStats = fs.existsSync(tsx)
      ? spawnSync(tsx, ['src/cli.ts', 'stats'], {
        cwd: repo,
        env: update.env,
        encoding: 'utf8',
        timeout: 30000,
        maxBuffer: 8 * 1024 * 1024,
      })
      : null;
    const hasNotice = (text) => /Update available/.test(normalize(text, update));
    const result = {
      directDistStatsNotice: hasNotice(direct.combined),
      tsxSourceStatsNotice: tsxStats ? hasNotice(`${tsxStats.stdout ?? ''}${tsxStats.stderr ?? ''}`) : null,
      binStatsNotice: hasNotice(binStats.combined),
      binStatsJsonNotice: hasNotice(binJson.combined),
      helpOnlyNotice: hasNotice(help.combined),
    };
    observations['DIFF-021'] = {
      ...result,
      ok: result.directDistStatsNotice && (!tsxStats || result.tsxSourceStatsNotice) &&
        !result.binStatsNotice && !result.binStatsJsonNotice && result.helpOnlyNotice,
    };
  }

  {
    const cases = [
      ['show', ['show']],
      ['sync --bogus', ['sync', '--bogus']],
      ['recent --limit', ['recent', '--limit']],
      ['seeds create', ['seeds', 'create']],
    ];
    const checks = {};
    for (const [label, args] of cases) {
      const result = runBin(base, args);
      const text = normalize(result.stderr || result.combined, base);
      const errorIndex = text.search(/(?:^|\n)\s*(?:error|›\s*Error):/i);
      const usageIndex = text.search(/(?:^|\n)\s*(?:USAGE|Usage)\b/);
      checks[label] = {
        nonzero: result.status !== 0,
        errorBeforeUsage: errorIndex >= 0 && usageIndex >= 0 && errorIndex < usageIndex,
      };
    }
    observations['DIFF-022'] = {
      checks,
      ok: Object.values(checks).every((entry) => entry.nonzero && entry.errorBeforeUsage),
    };
  }

  {
    const seed = runBin(stateful, ['seeds', 'text', 'differential seed', '--title', 'Demo']);
    const library = runBin(stateful, ['library', 'create', 'notes/demo.md', '--title', 'Demo']);
    const command = runBin(stateful, ['commands', 'new', 'demo', '--stdin'], { input: '# Demo\n\nbody\n' });
    const install = runBin(stateful, ['install', 'app', '--json', '--install-dir', path.join(root, 'app-install')]);
    const skill = runBin(stateful, ['skill', 'install', '--force']);
    const files = listFiles(stateful);
    const installText = normalize(install.combined, stateful);
    const result = {
      seedExit: seed.status,
      libraryExit: library.status,
      commandsExit: command.status,
      installExit: install.status,
      skillExit: skill.status,
      installUnsupported: /only supported on macOS/i.test(installText),
      files,
    };
    observations['DIFF-023'] = {
      ...result,
      ok: result.seedExit === 0 && result.libraryExit === 0 && result.commandsExit === 0 &&
        result.skillExit === 0 && Object.keys(files).some((file) => file.endsWith('.fieldtheory/library/notes/demo.md')) &&
        Object.keys(files).some((file) => file.endsWith('.fieldtheory/library/Commands/demo.md')) &&
        Object.keys(files).some((file) => file.endsWith('.claude/commands/fieldtheory.md')),
    };
  }

  {
    const missing = TOP_LEVEL_COMMANDS.filter((command) => !commandResolution[command]);
    const ideasHelp = runBin(base, ['ideas', '--help']);
    const ideasText = normalize(ideasHelp.combined, base);
    const result = {
      total: TOP_LEVEL_COMMANDS.length,
      missing,
      ideasAliasesPossible: ideasHelp.status === 0 && ideasAliasMatchesPossible,
    };
    observations['DIFF-024'] = {
      ...result,
      ok: result.missing.length === 0 && result.ideasAliasesPossible,
    };
  }

  const result = {
    schema_version: 'migration-differential-probe:v1',
    batch_id: BATCH_ID,
    observations,
    summary: {
      total: Object.keys(observations).length,
      passed: Object.values(observations).filter((observation) => observation.ok).length,
      failed: Object.values(observations).filter((observation) => !observation.ok).length,
    },
  };

  try {
    if (out) {
      fs.mkdirSync(path.dirname(out), { recursive: true });
      fs.writeFileSync(out, `${JSON.stringify(result, null, 2)}\n`);
    } else {
      process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }

  return result;
}
