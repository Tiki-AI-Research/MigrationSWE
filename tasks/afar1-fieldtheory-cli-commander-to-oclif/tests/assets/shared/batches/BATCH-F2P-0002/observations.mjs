import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {
  assert,
  assertEqual,
  assertMatch,
  assertNotMatch,
  expectFailure,
  expectMissingRequiredArg,
  expectNamedTest,
  expectNonexistentFlag,
  expectOclifUsage,
  expectRequiredArtifactOption,
  expectShortHelp,
  expectSuccess,
  expectVersionFlags,
  ensureFixture,
  npmLs,
  readJson,
  runCli,
  withSandbox,
} from './cli-probe-lib.mjs';

function expectUsageFor(result, label) {
  expectOclifUsage(result, label);
}

function expectUsageOnStdout(result, label) {
  assertMatch(result.stdout, /(^|\n)USAGE(\n|$)/, `${label} must include oclif USAGE help on stdout`);
}

function checkDependencyManifest(ctx) {
  const deps = ctx.pkg.dependencies ?? {};
  assertEqual(deps['@oclif/core'], '4.0.0', 'package.json must declare @oclif/core 4.0.0');
  assert(!Object.prototype.hasOwnProperty.call(deps, 'commander'), 'package.json must not keep commander as a runtime dependency');

  const lock = readJson(ctx.lockPath);
  const locked = lock.packages?.['node_modules/@oclif/core'];
  assert(locked, 'package-lock.json must resolve the @oclif/core tree');
  assertEqual(locked.version, '4.0.0', 'package-lock.json must resolve @oclif/core 4.0.0');
  assert(!Object.keys(lock.packages ?? {}).some((entry) => /(^|\/)node_modules\/commander$/.test(entry)), 'package-lock.json must not resolve commander');

  const oclifLs = npmLs(ctx, '@oclif/core');
  assertEqual(oclifLs.resolved?.version, '4.0.0', 'npm ls @oclif/core must resolve 4.0.0');
  const commanderLs = npmLs(ctx, 'commander');
  assertEqual(commanderLs.resolved, null, 'npm ls commander must not resolve a commander runtime dependency');

  const help = runCli(ctx, ['sync', '--help']);
  expectSuccess(help, 'ft sync --help');
  assertMatch(help.stdout, /(^|\n)USAGE(\n|$)/, 'ft sync --help must be rendered by oclif');
  assertMatch(help.stdout, /--cookies/, 'ft sync --help must still list --cookies');
}

function checkVersionAndValidationHelp(ctx) {
  expectVersionFlags(ctx);
  expectMissingRequiredArg(ctx);
  expectNonexistentFlag(ctx);

  const missingValue = runCli(ctx, ['recent', '--limit']);
  expectFailure(missingValue, 'ft recent --limit');
  assertMatch(missingValue.stderr, /expects a value/, 'ft recent --limit must report a missing flag value');
  expectUsageFor(missingValue, 'ft recent --limit');

  expectRequiredArtifactOption(ctx);
}

function checkVariadicCookieParsing(ctx) {
  const help = runCli(ctx, ['sync', '--help']);
  expectSuccess(help, 'ft sync --help');
  assertMatch(help.stdout, /(^|\n)USAGE(\n|$)/, 'ft sync --help must be rendered by oclif');
  assertMatch(help.stdout, /--cookies\s+(?:<values?\.\.\.>|<values?>\.\.\.|values?\.\.\.)/, 'ft sync --help must declare variadic --cookies');
  assertMatch(help.stdout, /--no-media/, 'ft sync --help must declare --no-media');

  const parsed = runCli(ctx, ['sync', '--cookies', 'ct0', 'auth-token', '--rebuild', '--continue']);
  expectFailure(parsed, 'ft sync --cookies ct0 auth-token --rebuild --continue');
  assertMatch(parsed.stderr, /cannot be used together/, 'both --cookies values and --rebuild must reach the sync handler');
  assertNotMatch(parsed.stderr, /Unexpected argument|unknown option|Nonexistent flag/, 'variadic --cookies must not greedy-consume the following flag');
}

function checkCompatibilityForwarding(ctx) {
  const ideas = runCli(ctx, ['ideas', '--help']);
  expectSuccess(ideas, 'ft ideas --help');
  assertMatch(ideas.stdout, /possible/i, 'ft ideas --help must display possible help');

  const seed = runCli(ctx, ['possible', 'seed', 'list']);
  expectSuccess(seed, 'ft possible seed list');
  assertMatch(seed.stdout, /seed/i, 'ft possible seed list must forward to seeds list');

  const bookmarksEnable = runCli(ctx, ['bookmarks', 'enable', '--help']);
  expectSuccess(bookmarksEnable, 'ft bookmarks enable --help');
  assertMatch(bookmarksEnable.stdout, /sync/i, 'ft bookmarks enable --help must forward to sync help');

  for (const args of [
    ['current', 'update', '--help'],
    ['codex', 'panel', '--help'],
    ['possible', 'nightly', 'list'],
    ['possible', '_run-job', '--help'],
    ['seeds', 'list', '--help'],
    ['bookmarks', '--help'],
  ]) {
    const result = runCli(ctx, args);
    expectSuccess(result, `ft ${args.join(' ')}`);
    if (args.includes('--help')) {
      expectUsageOnStdout(result, `ft ${args.join(' ')}`);
    }
  }
}

function checkBookmarksForwarding(ctx) {
  const syncHelp = runCli(ctx, ['sync', '--help']);
  expectSuccess(syncHelp, 'ft sync --help');
  const bookmarksSyncHelp = runCli(ctx, ['bookmarks', 'sync', '--help']);
  expectSuccess(bookmarksSyncHelp, 'ft bookmarks sync --help');
  assertEqual(
    bookmarksSyncHelp.stdout.replace(/\s+/g, ' ').trim(),
    syncHelp.stdout.replace(/\s+/g, ' ').trim(),
    'ft bookmarks sync --help must render the same help as ft sync --help',
  );

  const enableHelp = runCli(ctx, ['bookmarks', 'enable', '--help']);
  expectSuccess(enableHelp, 'ft bookmarks enable --help');
  assertMatch(enableHelp.stdout, /sync/i, 'ft bookmarks enable --help must render sync help');

  const bookmarksHelp = runCli(ctx, ['bookmarks', '--help']);
  expectSuccess(bookmarksHelp, 'ft bookmarks --help');
  expectUsageOnStdout(bookmarksHelp, 'ft bookmarks --help');
}

function checkNestedTopics(ctx) {
  const nightly = runCli(ctx, ['possible', 'nightly', 'list']);
  expectSuccess(nightly, 'ft possible nightly list');

  const seed = runCli(ctx, ['possible', 'seed', 'list']);
  expectSuccess(seed, 'ft possible seed list');
  assertMatch(seed.stdout, /seed/i, 'ft possible seed list must rewrite to seeds list');

  const nightlyHelp = runCli(ctx, ['possible', 'nightly', 'install', '--help']);
  expectSuccess(nightlyHelp, 'ft possible nightly install --help');
  assertMatch(nightlyHelp.stdout, /(^|\n)USAGE(\n|$)/, 'space-separated topics must be oclif-rendered');
}

function checkSearchJsonAndStatus(ctx) {
  withSandbox((box) => {
    ensureFixture(ctx, 'bookmarks.jsonl', box);

    const index = runCli(ctx, ['index'], { box });
    expectSuccess(index, 'ft index');

    const search = runCli(ctx, ['search', 'Machine', '--limit', '20', '--json'], { box });
    expectSuccess(search, 'ft search Machine --limit 20 --json');
    assertNotMatch(search.stdout, /F i e l d|What's New|What’s New/, 'search --json stdout must not contain logo or What’s New chrome');
    const records = JSON.parse(search.stdout);
    assert(Array.isArray(records), 'search --json must emit a JSON array');
    assertEqual(records.length, 1, 'search --json must return the indexed fixture record');
    for (const key of ['id', 'url', 'text', 'authorHandle', 'authorName', 'postedAt', 'score']) {
      assert(Object.prototype.hasOwnProperty.call(records[0], key), `search --json record must keep the ${key} field`);
    }

    for (const command of ['status', 'paths']) {
      const result = runCli(ctx, [command, '--json'], { box });
      expectSuccess(result, `ft ${command} --json`);
      const payload = JSON.parse(result.stdout);
      assert(payload && typeof payload === 'object' && !Array.isArray(payload), `ft ${command} --json must emit a JSON object`);
    }
  });
}

function checkNumericDefaults(ctx) {
  const expectations = [
    { args: ['search', '--help'], patterns: [/--limit/, /\[default: 20\]/] },
    { args: ['list', '--help'], patterns: [/--offset/] },
    { args: ['tree', '--help'], patterns: [/--limit/, /\[default: 80\]/] },
    { args: ['sync', '--help'], patterns: [/--delay-ms/, /\[default: 600\]/, /--max-minutes/, /\[default: 30\]/] },
  ];
  for (const { args, patterns } of expectations) {
    const result = runCli(ctx, args);
    expectSuccess(result, `ft ${args.join(' ')}`);
    for (const pattern of patterns) {
      assertMatch(result.stdout, pattern, `ft ${args.join(' ')} must keep numeric option metadata ${pattern}`);
    }
  }

  for (const args of [
    ['recent', '--limit', '0'],
    ['ls', '--limit', '-1'],
    ['find', 'q', '--limit', '1.5'],
    ['grep', 'q', '--limit', 'abc'],
    ['context', '--limit', '0'],
    ['head', 'f', '--lines', '0'],
  ]) {
    const result = runCli(ctx, args);
    expectFailure(result, `ft ${args.join(' ')}`);
    assertMatch(result.stderr, /positive integer|greater than or equal to 1|must be (?:an? )?(?:positive )?integer|invalid[^\n]{0,20}integer|at least 1/i, `ft ${args.join(' ')} must keep positive-integer validation`);
    expectUsageFor(result, `ft ${args.join(' ')}`);
  }

  checkNestedTopics(ctx);
}

function legacyTest(ctx, file, name, extra) {
  extra(ctx);
  expectNamedTest(ctx, file, name);
}

export const OBSERVATIONS = [
  {
    id: 'F2P-028',
    requirement_ids: ['R029'],
    description: 'package.json and lockfile declare oclif 4.0.0 and no commander runtime dependency, and the built help is oclif-rendered',
    run: checkDependencyManifest,
  },
  {
    id: 'F2P-029',
    requirement_ids: ['R032'],
    description: 'version flags print exactly the package version and validation failures show oclif USAGE help',
    run: checkVersionAndValidationHelp,
  },
  {
    id: 'F2P-030',
    requirement_ids: ['R035'],
    description: 'variadic --cookies parses two values before --rebuild without greedy flag consumption',
    run: checkVariadicCookieParsing,
  },
  {
    id: 'F2P-033',
    requirement_ids: ['R041'],
    description: 'nested topics, ideas/possible seed aliases, and bookmarks forwarding resolve through oclif argv rewriting',
    run: checkCompatibilityForwarding,
  },
  {
    id: 'F2P-025',
    requirement_ids: ['R031'],
    description: 'loadChromeSessionConfig --browser brave plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/config.test.ts', 'loadChromeSessionConfig: --browser brave resolves to Brave', (c) => {
      expectVersionFlags(c);
      expectMissingRequiredArg(c);
    }),
  },
  {
    id: 'F2P-026',
    requirement_ids: ['R031'],
    description: 'compareVersions double-digit segments plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/cli.test.ts', 'compareVersions: handles double-digit segments', (c) => {
      expectMissingRequiredArg(c);
    }),
  },
  {
    id: 'F2P-027',
    requirement_ids: ['R031'],
    description: 'redactSecrets Bearer tokens plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/engine.test.ts', 'redactSecrets: masks Bearer auth tokens', (c) => {
      expectRequiredArtifactOption(c);
    }),
  },
  {
    id: 'F2P-031',
    requirement_ids: ['R031'],
    description: 'parseCookieOption coercion plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/cli.test.ts', 'parseCookieOption: coerces non-string array elements to strings', (c) => {
      expectVersionFlags(c);
      expectMissingRequiredArg(c);
    }),
  },
  {
    id: 'F2P-032',
    requirement_ids: ['R031'],
    description: 'hasCommandOnPath PATHEXT plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/engine.test.ts', 'hasCommandOnPath: honors PATHEXT on win32', (c) => {
      expectMissingRequiredArg(c);
      expectShortHelp(c);
    }),
  },
  {
    id: 'F2P-034',
    requirement_ids: ['R031'],
    description: 'detectAvailableEngines plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/engine.test.ts', 'detectAvailableEngines: returns array of available engines', (c) => {
      expectVersionFlags(c);
      expectRequiredArtifactOption(c);
    }),
  },
  {
    id: 'F2P-035',
    requirement_ids: ['R031'],
    description: 'ft current edit protocol plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/cli.test.ts', 'ft current prints the single current-document edit protocol', (c) => {
      expectMissingRequiredArg(c);
    }),
  },
  {
    id: 'F2P-036',
    requirement_ids: ['R041'],
    description: 'bookmarks sync/enable/search compatibility forwarding',
    run: checkBookmarksForwarding,
  },
  {
    id: 'F2P-037',
    requirement_ids: ['R035'],
    description: 'business modules keep contracts and variadic cookies still parse under oclif',
    run: (ctx) => {
      for (const file of [
        'tests/bookmarks-db.test.ts',
        'tests/graphql-bookmarks.test.ts',
        'tests/library.test.ts',
        'tests/commands-files.test.ts',
        'tests/skill.test.ts',
        'tests/app-install.test.ts',
      ]) {
        const result = runSuite(ctx, file);
        assertEqual(result.status, 0, `${file} must pass after the framework swap`);
      }
      checkVariadicCookieParsing(ctx);
    },
  },
  {
    id: 'F2P-038',
    requirement_ids: ['R036'],
    description: 'seeds create required option is translated and followed by oclif USAGE help',
    run: (ctx) => {
      expectShortHelp(ctx);
      expectVersionFlags(ctx);
      const help = runCli(ctx, ['--help']);
      expectSuccess(help, 'ft --help');
      for (const command of ['sync', 'search', 'possible', 'ideas', 'bookmarks', 'install', 'skill']) {
        assertMatch(help.stdout, new RegExp(`\\b${command}\\b`), `ft --help must list ${command}`);
      }
      expectRequiredArtifactOption(ctx);
    },
  },
  {
    id: 'F2P-039',
    requirement_ids: ['R040'],
    description: 'search/status/paths JSON contracts plus space-separated nested possible topics',
    run: (ctx) => {
      checkSearchJsonAndStatus(ctx);
      checkNestedTopics(ctx);
    },
  },
  {
    id: 'F2P-040',
    requirement_ids: ['R031'],
    description: 'runWithSpinner error cleanup plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/cli.test.ts', 'runWithSpinner: stops spinner after error', (c) => {
      expectRequiredArtifactOption(c);
      expectNonexistentFlag(c);
    }),
  },
  {
    id: 'F2P-041',
    requirement_ids: ['R031'],
    description: 'loadChromeSessionConfig profile default plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/config.test.ts', 'loadChromeSessionConfig defaults profile to Default', (c) => {
      expectVersionFlags(c);
      expectMissingRequiredArg(c);
    }),
  },
  {
    id: 'F2P-042',
    requirement_ids: ['R031'],
    description: 'FT_BROWSER config plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/config.test.ts', 'loadChromeSessionConfig: FT_BROWSER env is honored', (c) => {
      expectMissingRequiredArg(c);
    }),
  },
  {
    id: 'F2P-043',
    requirement_ids: ['R031'],
    description: 'ft wiki --engine registration plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/cli.test.ts', 'ft wiki: --engine option is registered', (c) => {
      expectRequiredArtifactOption(c);
      expectNonexistentFlag(c);
    }),
  },
  {
    id: 'F2P-044',
    requirement_ids: ['R031'],
    description: 'SIGKILL escalation plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/engine.test.ts', 'invokeEngineAsync: SIGTERM-resistant child is killed via SIGKILL escalation', (c) => {
      expectVersionFlags(c);
      expectMissingRequiredArg(c);
    }),
  },
  {
    id: 'F2P-045',
    requirement_ids: ['R031'],
    description: 'firefox config resolution plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/config.test.ts', 'loadChromeSessionConfig: --browser firefox resolves correctly', (c) => {
      expectMissingRequiredArg(c);
    }),
  },
  {
    id: 'F2P-046',
    requirement_ids: ['R031'],
    description: 'runWithSpinner success cleanup plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/cli.test.ts', 'runWithSpinner: stops spinner after success', (c) => {
      expectVersionFlags(c);
      expectRequiredArtifactOption(c);
    }),
  },
  {
    id: 'F2P-047',
    requirement_ids: ['R040'],
    description: 'numeric defaults and validation plus nested possible topic forwarding',
    run: checkNumericDefaults,
  },
  {
    id: 'F2P-048',
    requirement_ids: ['R031'],
    description: 'GitHub token redaction plus black-box oclif CLI assertions',
    run: (ctx) => legacyTest(ctx, 'tests/engine.test.ts', 'redactSecrets: masks GitHub-style prefixed tokens', (c) => {
      expectMissingRequiredArg(c);
    }),
  },
];

function runSuite(ctx, testFile) {
  const tsx = path.join(ctx.repoRoot, 'node_modules', '.bin', 'tsx');
  if (!fs.existsSync(tsx)) {
    throw new Error(`tsx executable missing at ${tsx}`);
  }
  const result = spawnSync(tsx, ['--test', testFile], {
    cwd: ctx.repoRoot,
    encoding: 'utf8',
    env: { ...process.env, NO_COLOR: '1' },
    timeout: 180_000,
  });
  if (result.error) throw new Error(`failed to run ${testFile}: ${result.error.message}`);
  return { status: result.status, stdout: result.stdout ?? '', stderr: result.stderr ?? '' };
}
