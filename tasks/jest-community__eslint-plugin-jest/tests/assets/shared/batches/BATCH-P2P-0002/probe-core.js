#!/usr/bin/env node
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { createRequire } = require('module');
const { spawnSync } = require('child_process');

const BATCH_ID = 'BATCH-P2P-0002';
const OBSERVATIONS = [
  'P2P-025',
  'P2P-027',
  'P2P-028',
  'P2P-029',
  'P2P-031',
  'P2P-032',
  'P2P-033',
  'P2P-036',
  'P2P-037',
  'P2P-039',
  'P2P-040',
  'P2P-026',
  'P2P-030',
  'P2P-035',
  'P2P-038',
  'P2P-034',
];

const ALLOWED_CHANGED_PATHS = new Set([
  'package.json',
  'yarn.lock',
  'eslint.config.js',
  'index.d.ts',
  'types.d.ts',
  'src/index.ts',
  'src/rules/prefer-importing-jest-globals.ts',
  'src/rules/__tests__/prefer-importing-jest-globals.test.ts',
  'src/rules/__tests__/test-utils.ts',
  'src/__tests__/rules.test.ts',
  'src/__tests__/__snapshots__/rules.test.ts.snap',
  'README.md',
  '.github/workflows/nodejs.yml',
]);

const isFile = file => {
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
};

const findRepoRoot = start => {
  let current = path.resolve(start || process.cwd());

  for (;;) {
    if (isFile(path.join(current, 'package.json'))) {
      try {
        const pkg = JSON.parse(
          fs.readFileSync(path.join(current, 'package.json'), 'utf8'),
        );

        if (
          pkg.name === 'eslint-plugin-jest' &&
          isFile(path.join(current, 'src/index.ts'))
        ) {
          return current;
        }
      } catch {}
    }

    const parent = path.dirname(current);

    if (parent === current) {
      break;
    }

    current = parent;
  }

  for (const candidate of [
    process.env.P2P_REPO_ROOT,
    process.env.REPO_ROOT,
    '/workspace/repo',
    '/input/source',
    '/input/target',
  ]) {
    if (candidate && isFile(path.join(candidate, 'src/index.ts'))) {
      return path.resolve(candidate);
    }
  }

  throw new Error('Unable to locate the eslint-plugin-jest repository root');
};

const requireFromRepo = (repo, name) =>
  createRequire(path.join(repo, 'package.json'))(name);

const installTypeScriptRequireHook = repo => {
  const babel = requireFromRepo(repo, '@babel/core');
  const previous = require.extensions['.ts'];

  require.extensions['.ts'] = (module, filename) => {
    const source = fs.readFileSync(filename, 'utf8');
    const result = babel.transformSync(source, {
      filename,
      cwd: repo,
      configFile: path.join(repo, 'babel.config.js'),
      ast: false,
      sourceMaps: 'inline',
      sourceFileName: filename,
    });

    module._compile(result.code, filename);
  };

  return () => {
    if (previous) {
      require.extensions['.ts'] = previous;
    } else {
      delete require.extensions['.ts'];
    }
  };
};

const normalizePlugin = exported => {
  if (exported && exported.meta && exported.rules) {
    return exported;
  }

  if (exported && exported.default && exported.default.meta) {
    return exported.default;
  }

  if (exported && exported.default && exported.default.default) {
    return exported.default.default;
  }

  return exported;
};

const loadPlugin = repo => {
  const restoreHook = installTypeScriptRequireHook(repo);
  const entry = path.join(repo, 'src/index.ts');

  try {
    try {
      delete require.cache[require.resolve(entry)];
    } catch {}
    return normalizePlugin(require(entry));
  } finally {
    restoreHook();
  }
};

const makeLinter = repo => {
  const { Linter } = requireFromRepo(repo, 'eslint');

  return new Linter({ configType: 'flat', cwd: repo });
};

const flatConfig = (plugin, extra = {}) => ({
  ...(extra.files ? { files: extra.files } : {}),
  plugins: { jest: plugin },
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: 'script',
    ...(extra.languageOptions || {}),
  },
  rules: extra.rules || {},
  ...(extra.settings ? { settings: extra.settings } : {}),
});

const asConfigArray = config => (Array.isArray(config) ? config : [config]);

const verify = (linter, code, config, filename) =>
  linter.verify(code, asConfigArray(config), {
    filename: filename || 'sample.js',
    allowInlineConfig: false,
    reportUnusedDisableDirectives: false,
  });

const verifyAndFix = (linter, code, config, filename) =>
  linter.verifyAndFix(code, asConfigArray(config), {
    filename: filename || 'sample.js',
    allowInlineConfig: false,
    reportUnusedDisableDirectives: false,
  });

const messageProjection = message => ({
  ruleId: message.ruleId,
  severity: message.severity,
  messageId: message.messageId,
  message: message.message,
  line: message.line,
  column: message.column,
  endLine: message.endLine,
  endColumn: message.endColumn,
});

const stableJson = value => {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(',')}]`;
  }

  if (value && typeof value === 'object') {
    return `{${Object.keys(value)
      .sort()
      .map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`)
      .join(',')}}`;
  }

  return JSON.stringify(value);
};

const sha256 = value =>
  `sha256:${crypto.createHash('sha256').update(value).digest('hex')}`;

const runCommand = (command, args, options = {}) => {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: { ...process.env, CI: '1', FORCE_COLOR: '0', NO_COLOR: '1', ...options.env },
    encoding: 'utf8',
    timeout: options.timeout || 1_800_000,
    maxBuffer: 32 * 1024 * 1024,
  });

  return {
    status: result.status === null ? 124 : result.status,
    signal: result.signal || null,
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    error: result.error ? String(result.error.message || result.error) : null,
  };
};

const yarnInvocation = repo => {
  const release = path.join(repo, '.yarn/releases/yarn-4.18.0.cjs');

  if (isFile(release)) {
    return [process.execPath, [release]];
  }

  return ['yarn', []];
};

const runYarn = (repo, script) => {
  const [command, prefix] = yarnInvocation(repo);

  return runCommand(command, [...prefix, script], { cwd: repo });
};

const runJest = (repo, testPath, testNamePattern) => {
  const jest = path.join(repo, 'node_modules/jest/bin/jest.js');
  const args = [
    jest,
    '--ci',
    '--runInBand',
    '--selectProjects',
    'test',
    '--runTestsByPath',
    testPath,
  ];

  if (testNamePattern) {
    args.push('--testNamePattern', testNamePattern);
  }

  return runCommand(process.execPath, args, { cwd: repo });
};

const lintRule = (repo, plugin, code, ruleName, extra = {}) => {
  const linter = makeLinter(repo);
  const config = flatConfig(plugin, {
    rules: { [`jest/${ruleName}`]: 'error' },
    settings: extra.settings,
    languageOptions: extra.languageOptions,
  });

  return verify(linter, code, config, extra.filename);
};

const fixRule = (repo, plugin, code, ruleName, extra = {}) => {
  const linter = makeLinter(repo);
  const config = flatConfig(plugin, {
    rules: { [`jest/${ruleName}`]: 'error' },
    settings: extra.settings,
    languageOptions: extra.languageOptions,
  });

  return verifyAndFix(linter, code, config, extra.filename);
};

const ruleMetaProjection = plugin => {
  const rules = Object.keys(plugin.rules)
    .sort()
    .map(name => {
      const { meta } = plugin.rules[name];

      return {
        name,
        type: meta.type,
        fixable: meta.fixable,
        hasSuggestions: Boolean(meta.hasSuggestions),
        deprecated: Boolean(meta.deprecated),
        requiresTypeChecking: Boolean(meta.docs && meta.docs.requiresTypeChecking),
        messages: Object.keys(meta.messages || {}).sort(),
        schema: meta.schema,
      };
    });

  return { count: rules.length, hash: sha256(stableJson(rules)) };
};

const noDeprecatedProjection = (repo, plugin, version) => {
  const code = "jest.genMockFromModule('x')";
  const settings = { jest: { version } };
  const messages = lintRule(repo, plugin, code, 'no-deprecated-functions', {
    settings,
    filename: 'settings.js',
  });
  const fixed = fixRule(repo, plugin, code, 'no-deprecated-functions', {
    settings,
    filename: 'settings.js',
  });

  return {
    version,
    messages: messages.map(messageProjection),
    fixedOutput: fixed.output,
  };
};

const settingsProjection = (repo, plugin, detection) => {
  const aliasMessages = lintRule(
    repo,
    plugin,
    'context.only()',
    'no-focused-tests',
    {
      settings: { jest: { globalAliases: { describe: ['context'] } } },
      filename: 'alias.js',
    },
  );
  const globalPackageCode = [
    "const { test } = require('bun:test');",
    '',
    "test('x', () => { expect(1).toBe(1); });",
  ].join('\n');
  const withoutGlobalPackage = lintRule(
    repo,
    plugin,
    globalPackageCode,
    'no-standalone-expect',
    { filename: 'global-package.js' },
  );
  const withGlobalPackage = lintRule(
    repo,
    plugin,
    globalPackageCode,
    'no-standalone-expect',
    {
      settings: { jest: { globalPackage: 'bun:test' } },
      filename: 'global-package.js',
    },
  );

  return {
    versionNumber: noDeprecatedProjection(repo, plugin, 27),
    versionString: noDeprecatedProjection(repo, plugin, '27.4.0'),
    aliasMessages: aliasMessages.map(messageProjection),
    globalPackage: {
      withoutSetting: withoutGlobalPackage.map(messageProjection),
      withSetting: withGlobalPackage.map(messageProjection),
    },
    detection,
  };
};

const compileDetectJestVersion = repo => {
  const babel = requireFromRepo(repo, '@babel/core');
  const file = path.join(repo, 'src/rules/utils/detectJestVersion.ts');
  const result = babel.transformSync(fs.readFileSync(file, 'utf8'), {
    filename: file,
    cwd: repo,
    configFile: path.join(repo, 'babel.config.js'),
    ast: false,
    sourceMaps: false,
  });

  return result.code;
};

const writeFile = (root, relative, contents) => {
  const file = path.join(root, relative);

  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, contents);
};

const runDetectJestVersionFixtures = repo => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'p2p-jest-version-'));
  const compiled = compileDetectJestVersion(repo);
  const modulePath =
    'node_modules/eslint-plugin-jest/lib/rules/detectJestVersion.js';

  try {
    for (const [project, version] of [
      ['backend', '24.0.0'],
      ['frontend', '15.0.0'],
    ]) {
      writeFile(root, `${project}/package.json`, JSON.stringify({ name: project }));
      writeFile(root, `${project}/${modulePath}`, compiled);
      writeFile(
        root,
        `${project}/node_modules/jest/package.json`,
        JSON.stringify({ name: 'jest', version }),
      );
    }

    const script = [
      "const { detectJestVersion } = require('eslint-plugin-jest/lib/rules/detectJestVersion.js');",
      'try { console.log(detectJestVersion()); } catch (error) { console.error(error.message); process.exitCode = 1; }',
    ].join('\n');

    const backend = runCommand(process.execPath, ['-e', script], {
      cwd: path.join(root, 'backend'),
    });
    const frontend = runCommand(process.execPath, ['-e', script], {
      cwd: path.join(root, 'frontend'),
    });

    const cacheRoot = path.join(root, 'cache');
    writeFile(cacheRoot, 'package.json', JSON.stringify({ name: 'cache' }));
    writeFile(cacheRoot, modulePath, compiled);
    writeFile(
      cacheRoot,
      'node_modules/jest/package.json',
      JSON.stringify({ name: 'jest', version: '26.0.0' }),
    );
    const cacheScript = [
      "const fs = require('fs');",
      "const { detectJestVersion } = require('eslint-plugin-jest/lib/rules/detectJestVersion.js');",
      "console.log(detectJestVersion());",
      "fs.writeFileSync('node_modules/jest/package.json', JSON.stringify({ name: 'jest', version: '25.0.0' }));",
      'console.log(detectJestVersion());',
    ].join('\n');
    const cached = runCommand(process.execPath, ['-e', cacheScript], {
      cwd: cacheRoot,
    });

    const missingRoot = path.join(root, 'missing');
    writeFile(missingRoot, 'package.json', JSON.stringify({ name: 'missing' }));
    writeFile(missingRoot, modulePath, compiled);
    writeFile(
      missingRoot,
      'node_modules/placeholder/package.json',
      JSON.stringify({ name: 'placeholder' }),
    );
    const missingScript = [
      "const { detectJestVersion } = require('eslint-plugin-jest/lib/rules/detectJestVersion.js');",
      'try { console.log(detectJestVersion()); } catch (error) { console.error(error.message); }',
    ].join('\n');
    const missing = runCommand(process.execPath, ['-e', missingScript], {
      cwd: missingRoot,
    });

    return {
      backend: Number((backend.stdout || '').trim()),
      frontend: Number((frontend.stdout || '').trim()),
      cached: (cached.stdout || '')
        .trim()
        .split(/\s+/u)
        .filter(Boolean)
        .map(Number),
      missingError: /Unable to detect Jest version/u.test(missing.stderr || ''),
    };
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
};

const noUnnecessaryAssertionProjection = (repo, plugin) => {
  const parser = requireFromRepo(repo, '@typescript-eslint/parser');
  const fixtures = path.join(repo, 'src/rules/__tests__/fixtures');

  const lintFixture = fixtureDirectory => {
    const linter = makeLinter(repo);
    const filename = path.join(fixtureDirectory, 'file.ts');
    const config = flatConfig(plugin, {
      files: ['**/*.ts'],
      rules: { 'jest/no-unnecessary-assertion': 'error' },
      languageOptions: {
        parser,
        parserOptions: {
          project: './tsconfig.json',
          tsconfigRootDir: fixtureDirectory,
        },
        ecmaVersion: 2022,
        sourceType: 'module',
      },
    });
    const messages = verify(linter, 'expect(1).toBeNull();', config, filename);

    return messages.map(messageProjection);
  };

  const unstrict = lintFixture(path.join(fixtures, 'unstrict'));
  const strict = lintFixture(fixtures);

  return {
    unstrict,
    strict,
    unstrictHasNoStrictNullCheck: unstrict.some(
      message => message.messageId === 'noStrictNullCheck',
    ),
    strictHasNoStrictNullCheck: strict.some(
      message => message.messageId === 'noStrictNullCheck',
    ),
  };
};

const declarationProjection = repo => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'p2p-types-'));
  const typescript = path.join(repo, 'node_modules/typescript/bin/tsc');

  try {
    fs.symlinkSync(
      path.join(repo, 'node_modules'),
      path.join(temp, 'node_modules'),
      'dir',
    );
    fs.writeFileSync(
      path.join(temp, 'consumer.ts'),
      [
        "import plugin = require('eslint-plugin-jest');",
        "const name: keyof typeof plugin.configs = 'flat/style';",
        "plugin.rules['no-focused-tests'];",
        'export { name };',
        '',
      ].join('\n'),
    );
    fs.writeFileSync(
      path.join(temp, 'tsconfig.json'),
      JSON.stringify(
        {
          compilerOptions: {
            strict: true,
            noEmit: true,
            skipLibCheck: false,
            module: 'commonjs',
            moduleResolution: 'node',
            target: 'es2020',
            baseUrl: '.',
            paths: { 'eslint-plugin-jest': [path.join(repo, 'index.d.ts')] },
            types: ['node'],
          },
          files: ['consumer.ts'],
        },
        null,
        2,
      ),
    );
    const result = runCommand(process.execPath, [typescript, '-p', 'tsconfig.json'], {
      cwd: temp,
      timeout: 300_000,
    });
    const declaration = fs.readFileSync(path.join(repo, 'index.d.ts'), 'utf8');
    const configKeys = [
      'all',
      'recommended',
      'style',
      'flat/all',
      'flat/recommended',
      'flat/style',
    ].filter(key => {
      const escaped = key.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');

      return new RegExp(`['"]?${escaped}['"]?:`, 'u').test(declaration);
    });

    return {
      typecheckExitCode: result.status,
      configKeys,
      hasMeta: /\bmeta:\s*\{/u.test(declaration),
      hasEnvironmentGlobals: /environments:\s*\{[\s\S]*?globals:/u.test(
        declaration,
      ),
      hasRuleIndexSignature: /rules:\s*\{[\s\S]*?\[key:\s*string\]:/u.test(
        declaration,
      ),
    };
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
};

const ruleBehaviourProjection = (repo, plugin) => {
  const code = "test.only('x', () => {})";
  const messages = lintRule(repo, plugin, code, 'no-focused-tests', {
    filename: 'focused.js',
  });
  const meta = ruleMetaProjection(plugin);

  return {
    meta,
    messages: messages.map(message => ({
      ...messageProjection(message),
      suggestions: (message.suggestions || []).map(suggestion => ({
        messageId: suggestion.messageId,
        desc: suggestion.desc,
      })),
    })),
  };
};

const configRulesProjection = (repo, plugin) => {
  const linter = makeLinter(repo);
  const code = "test.only('focused', () => { expect(true).toBe(true); });";
  const run = rules => {
    const config = flatConfig(plugin, { rules });
    return verify(linter, code, config, 'recommended.js').map(
      messageProjection,
    );
  };
  const recommended = plugin.configs.recommended.rules;
  const flatRecommended = plugin.configs['flat/recommended'].rules;
  const recommendedMessages = run(recommended);
  const flatRecommendedMessages = run(flatRecommended);

  return {
    rulesEqual: stableJson(recommended) === stableJson(flatRecommended),
    diagnosticsEqual:
      stableJson(recommendedMessages) === stableJson(flatRecommendedMessages),
    messages: recommendedMessages,
  };
};

const tableProjection = plugin => {
  const entries = table =>
    Object.keys(table)
      .sort()
      .map(key => [key, table[key]]);

  return {
    recommended: entries(plugin.configs.recommended.rules),
    style: entries(plugin.configs.style.rules),
  };
};

const configKeysProjection = plugin => ({
  configs: Object.keys(plugin.configs),
  ruleCount: Object.keys(plugin.rules).length,
});

const allRulesProjection = plugin => {
  const all = Object.keys(plugin.configs.all.rules).sort();
  const flatAll = Object.keys(plugin.configs['flat/all'].rules).sort();
  const expected = Object.keys(plugin.rules)
    .filter(name => !plugin.rules[name].meta.deprecated)
    .map(name => `jest/${name}`)
    .sort();
  const deprecated = Object.keys(plugin.rules)
    .filter(name => plugin.rules[name].meta.deprecated)
    .map(name => `jest/${name}`)
    .sort();

  return {
    allCount: all.length,
    flatAllCount: flatAll.length,
    allMatchesNonDeprecated: stableJson(all) === stableJson(expected),
    flatAllMatchesNonDeprecated: stableJson(flatAll) === stableJson(expected),
    deprecatedRulesIncluded: all.filter(name => deprecated.includes(name)),
  };
};

const exportProjection = plugin => {
  const version = String(plugin.meta && plugin.meta.version);

  return {
    name: plugin.meta && plugin.meta.name,
    hasSemverVersion: /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/u.test(version),
    configKeys: Object.keys(plugin.configs),
    globalKeysPresent: ['describe', 'it', 'expect'].every(key =>
      Boolean(plugin.environments.globals.globals[key] !== undefined),
    ),
    ruleCount: Object.keys(plugin.rules).length,
  };
};

const typeAwareProjection = plugin => {
  const expected = [
    'no-error-equal',
    'no-unnecessary-assertion',
    'valid-expect-with-promise',
  ];
  const flagged = Object.keys(plugin.rules)
    .filter(name => plugin.rules[name].meta.docs.requiresTypeChecking === true)
    .sort();

  return {
    expected,
    expectedAllTrue: expected.every(
      name => plugin.rules[name].meta.docs.requiresTypeChecking === true,
    ),
    flagged,
    flaggedCount: flagged.length,
  };
};

const protectedFilesProjection = repo => {
  const manifestPath = path.join(
    __dirname,
    '../../../fixtures/batches/BATCH-P2P-0002/protected-hashes.json',
  );
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const changed = [];

  for (const [relative, expected] of Object.entries(manifest.files)) {
    const file = path.join(repo, relative);

    if (!isFile(file)) {
      changed.push({ path: relative, reason: 'missing' });
      continue;
    }

    const actual = sha256(fs.readFileSync(file));
    if (actual !== expected) {
      changed.push({ path: relative, reason: 'content' });
    }
  }

  const unexpected = changed
    .map(item => item.path)
    .filter(relative => !ALLOWED_CHANGED_PATHS.has(relative))
    .sort();

  return {
    checkedCount: Object.keys(manifest.files).length,
    changedCount: changed.length,
    unexpectedChanged: unexpected,
  };
};

const testProjections = repo => {
  const rulesTest = runJest(repo, 'src/__tests__/rules.test.ts');
  const lazyTypeScriptTest = runJest(
    repo,
    'src/rules/__tests__/no-unnecessary-assertion.test.ts',
    'does not require typescript until the rule is actually created',
  );

  return {
    rulesTest: {
      exitCode: rulesTest.status,
      passed: rulesTest.status === 0,
    },
    lazyTypeScriptTest: {
      exitCode: lazyTypeScriptTest.status,
      passed: lazyTypeScriptTest.status === 0,
    },
  };
};

const validationCommandsProjection = repo => {
  const results = {
    test: runYarn(repo, 'test'),
    typecheck: runYarn(repo, 'typecheck'),
    lint: runYarn(repo, 'lint'),
    prettierCheck: runYarn(repo, 'prettier:check'),
  };

  return Object.fromEntries(
    Object.entries(results).map(([name, result]) => [name, result.status]),
  );
};

const evaluate = (repo, side, selected = OBSERVATIONS) => {
  const plugin = loadPlugin(repo);
  const cache = {};

  const memo = (key, compute) => {
    if (!(key in cache)) {
      cache[key] = compute();
    }

    return cache[key];
  };

  const detection = () =>
    memo('detection', () => runDetectJestVersionFixtures(repo));
  const tests = () => memo('tests', () => testProjections(repo));
  const validation = () =>
    memo('validation', () => validationCommandsProjection(repo));

  const values = {
    'P2P-025': () => settingsProjection(repo, plugin, detection()),
    'P2P-027': () => validation(),
    'P2P-028': () => exportProjection(plugin),
    'P2P-029': () => detection(),
    'P2P-031': () => settingsProjection(repo, plugin, detection()),
    'P2P-032': () => configKeysProjection(plugin),
    'P2P-033': () => allRulesProjection(plugin),
    'P2P-036': () => declarationProjection(repo),
    'P2P-037': () => ruleBehaviourProjection(repo, plugin),
    'P2P-039': () => tableProjection(plugin),
    'P2P-040': () => configRulesProjection(repo, plugin),
    'P2P-026': () => tests().rulesTest,
    'P2P-030': () => noUnnecessaryAssertionProjection(repo, plugin),
    'P2P-035': () => typeAwareProjection(plugin),
    'P2P-038': () => protectedFilesProjection(repo),
    'P2P-034': () => tests().lazyTypeScriptTest,
  };

  const checks = {
    'P2P-025': value =>
      value.versionNumber.messages.length === 1 &&
      value.versionString.messages.length === 1 &&
      value.globalPackage.withSetting.length === 0 &&
      value.detection.backend === 24 &&
      value.detection.frontend === 15,
    'P2P-027': value => Object.values(value).every(code => code === 0),
    'P2P-028': value =>
      value.name === 'eslint-plugin-jest' &&
      value.hasSemverVersion &&
      value.configKeys.length === 6 &&
      value.globalKeysPresent &&
      value.ruleCount === 71,
    'P2P-029': value =>
      value.backend === 24 &&
      value.frontend === 15 &&
      value.cached.length === 2 &&
      value.cached[0] === value.cached[1] &&
      value.missingError,
    'P2P-031': value =>
      value.versionNumber.messages.length === 1 &&
      value.versionString.messages.length === 1 &&
      value.aliasMessages.length === 1 &&
      value.globalPackage.withSetting.length === 0,
    'P2P-032': value =>
      stableJson(value.configs) ===
      stableJson([
        'all',
        'recommended',
        'style',
        'flat/all',
        'flat/recommended',
        'flat/style',
      ]),
    'P2P-033': value =>
      value.allMatchesNonDeprecated &&
      value.flatAllMatchesNonDeprecated &&
      value.deprecatedRulesIncluded.length === 0,
    'P2P-036': value =>
      value.typecheckExitCode === 0 &&
      value.configKeys.length === 6 &&
      value.hasMeta &&
      value.hasEnvironmentGlobals &&
      value.hasRuleIndexSignature,
    'P2P-037': value =>
      value.messages.length === 1 &&
      value.messages[0].messageId === 'focusedTest',
    'P2P-039': value =>
      value.recommended.length === 19 && value.style.length === 3,
    'P2P-040': value => value.rulesEqual && value.diagnosticsEqual,
    'P2P-026': value => value.passed,
    'P2P-030': value =>
      value.unstrictHasNoStrictNullCheck && !value.strictHasNoStrictNullCheck,
    'P2P-035': value =>
      value.expectedAllTrue &&
      !value.flagged.includes('no-focused-tests') &&
      !value.flagged.includes('prefer-to-be'),
    'P2P-038': value => value.unexpectedChanged.length === 0,
    'P2P-034': value => value.passed,
  };

  const results = {};

  for (const observation of selected) {
    const value = values[observation]();

    results[observation] = {
      status: checks[observation](value) ? 'PASS' : 'FAIL',
      value,
    };
  }

  return {
    side,
    batch_id: BATCH_ID,
    status: Object.values(results).every(result => result.status === 'PASS')
      ? 'PASS'
      : 'FAIL',
    results,
  };
};

const parseArguments = argv => {
  const options = {
    repo: null,
    observation: null,
    list: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];

    if (argument === '--list') {
      options.list = true;
    } else if (argument === '--observation') {
      options.observation = argv[(index += 1)];
    } else if (argument === '--repo') {
      options.repo = argv[(index += 1)];
    } else if (!argument.startsWith('-') && !options.repo) {
      options.repo = argument;
    }
  }

  return options;
};

const main = (side, argv = process.argv.slice(2)) => {
  const options = parseArguments(argv);

  if (options.list) {
    process.stdout.write(`${JSON.stringify(OBSERVATIONS)}\n`);
    return 0;
  }

  const repo = findRepoRoot(options.repo);
  const selected = options.observation ? [options.observation] : OBSERVATIONS;
  const evaluation = evaluate(repo, side, selected);

  if (options.observation) {
    const result = evaluation.results[options.observation];

    if (!result) {
      process.stderr.write(`Unknown observation: ${options.observation}\n`);
      return 2;
    }

    process.stdout.write(`${JSON.stringify(result)}\n`);
    return result.status === 'PASS' ? 0 : 1;
  }

  process.stdout.write(`${JSON.stringify(evaluation, null, 2)}\n`);

  return evaluation.status === 'PASS' ? 0 : 1;
};

module.exports = {
  BATCH_ID,
  OBSERVATIONS,
  evaluate,
  findRepoRoot,
  main,
};
