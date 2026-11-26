'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const BATCH_ID = 'BATCH-DIFFERENTIAL-0001';

const CONFIG_NAMES = [
  'all',
  'recommended',
  'style',
  'flat/all',
  'flat/recommended',
  'flat/style',
];

const RECOMMENDED_KEYS = [
  'jest/expect-expect',
  'jest/no-alias-methods',
  'jest/no-commented-out-tests',
  'jest/no-conditional-expect',
  'jest/no-deprecated-functions',
  'jest/no-disabled-tests',
  'jest/no-done-callback',
  'jest/no-export',
  'jest/no-focused-tests',
  'jest/no-identical-title',
  'jest/no-interpolation-in-snapshots',
  'jest/no-jasmine-globals',
  'jest/no-mocks-import',
  'jest/no-standalone-expect',
  'jest/no-test-prefixes',
  'jest/valid-describe-callback',
  'jest/valid-expect',
  'jest/valid-expect-in-promise',
  'jest/valid-title',
];

const RECOMMENDED_WARN = [
  'jest/expect-expect',
  'jest/no-commented-out-tests',
  'jest/no-disabled-tests',
];

const STYLE_RULES = {
  'jest/prefer-to-be': 'error',
  'jest/prefer-to-contain': 'error',
  'jest/prefer-to-have-length': 'error',
};

const ALLOWED_CHANGE_PATHS = new Set([
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

const sortedObject = value => {
  if (Array.isArray(value)) {
    return value.map(sortedObject);
  }

  if (value && typeof value === 'object') {
    const result = {};

    for (const key of Object.keys(value).sort()) {
      result[key] = sortedObject(value[key]);
    }

    return result;
  }

  return value;
};

const stable = value => JSON.stringify(sortedObject(value));

const has = (object, key) =>
  Object.prototype.hasOwnProperty.call(object || {}, key);

const normalizeMessage = message => ({
  ruleId: message.ruleId ?? null,
  messageId: message.messageId ?? null,
  severity: message.severity,
  message: message.message,
  line: message.line,
  column: message.column,
  endLine: message.endLine ?? null,
  endColumn: message.endColumn ?? null,
  fix: message.fix ? { range: message.fix.range, text: message.fix.text } : null,
  suggestions: (message.suggestions || []).map(suggestion => ({
    messageId: suggestion.messageId ?? null,
    desc: suggestion.desc,
    fix: suggestion.fix
      ? { range: suggestion.fix.range, text: suggestion.fix.text }
      : null,
  })),
});

const makeLinter = eslintModule => {
  const Linter = eslintModule.Linter;

  try {
    return new Linter({ configType: 'flat' });
  } catch {
    return new Linter();
  }
};

const baseConfig = options => {
  const languageOptions = {
    sourceType: options.sourceType || 'script',
  };

  if (options.parser) {
    languageOptions.parser = options.parser;
  }

  if (options.parserOptions) {
    languageOptions.parserOptions = options.parserOptions;
  }

  const config = {
    files: options.files || ['**/*'],
    plugins: { jest: options.plugin },
    rules: {},
    languageOptions,
    settings: options.settings || {},
  };

  const value = options.ruleValue || 'error';
  config.rules[`jest/${options.ruleName}`] = value;

  return config;
};

const lintRule = (plugin, eslintModule, code, ruleName, options = {}) => {
  const linter = makeLinter(eslintModule);
  const config = baseConfig({ ...options, plugin, ruleName });
  const filename = options.filename || 'case.test.js';
  const messages = linter.verify(code, [config], { filename });
  const fixed = linter.verifyAndFix(code, [config], { filename });

  return {
    messages: messages.map(normalizeMessage),
    fixed: fixed.fixed,
    output: fixed.output,
  };
};

const flattenConfig = (plugin, name) => {
  const config = plugin.configs[name];

  if (config && !has(config, 'env') && !Array.isArray(config.plugins)) {
    return config;
  }

  return {
    plugins: { jest: plugin },
    languageOptions: { globals: plugin.environments.globals.globals },
    rules: config.rules,
  };
};

const lintWithConfig = (plugin, eslintModule, config, code, filename) => {
  const linter = makeLinter(eslintModule);
  const messages = linter.verify(
    code,
    [{ files: ['**/*.js'], ...config }],
    { filename },
  );

  return messages.map(normalizeMessage);
};

const typecheckConsumer = repoRoot => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'epj-consumer-'));
  const nodeModules = path.join(directory, 'node_modules');

  fs.mkdirSync(nodeModules, { recursive: true });
  fs.symlinkSync(repoRoot, path.join(nodeModules, 'eslint-plugin-jest'), 'dir');
  fs.writeFileSync(
    path.join(directory, 'consumer.ts'),
    [
      "import plugin = require('eslint-plugin-jest');",
      "const name: keyof typeof plugin.configs = 'flat/style';",
      "const rule: keyof typeof plugin.rules = 'no-focused-tests';",
      'void name;',
      'void rule;',
      'void plugin.meta.name;',
      'void plugin.environments.globals;',
      '',
    ].join('\n'),
  );
  fs.writeFileSync(
    path.join(directory, 'tsconfig.json'),
    JSON.stringify(
      {
        compilerOptions: {
          strict: true,
          module: 'commonjs',
          target: 'es2020',
          moduleResolution: 'node',
          noEmit: true,
          skipLibCheck: true,
          esModuleInterop: true,
        },
        files: ['consumer.ts'],
      },
      null,
      2,
    ),
  );

  let tsc;

  try {
    tsc = require.resolve('typescript/bin/tsc', { paths: [repoRoot] });
  } catch {
    const fallback = path.join(repoRoot, 'node_modules', 'typescript', 'bin', 'tsc');

    if (!fs.existsSync(fallback)) {
      return { status: null, diagnostics: 'typescript not installed' };
    }

    tsc = fallback;
  }

  const result = spawnSync(process.execPath, [tsc, '-p', directory], {
    cwd: directory,
    encoding: 'utf8',
  });

  fs.rmSync(directory, { recursive: true, force: true });

  return {
    status: result.status,
    diagnostics: `${result.stdout || ''}${result.stderr || ''}`.trim(),
  };
};

const detectFixture = repoRoot => {
  const compiled = path.join(
    repoRoot,
    'lib',
    'rules',
    'utils',
    'detectJestVersion.js',
  );

  if (!fs.existsSync(compiled)) {
    return { error: `compiled detector not found at ${compiled}` };
  }

  const contents = fs.readFileSync(compiled, 'utf8');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'eslint-plugin-jest-'));
  const relative = 'eslint-plugin-jest/lib/rules/detectJestVersion.js';

  const write = (file, value) => {
    fs.mkdirSync(path.dirname(path.join(directory, file)), { recursive: true });
    fs.writeFileSync(path.join(directory, file), value);
  };

  for (const [project, version] of [
    ['backend', '24.0.0'],
    ['frontend', '15.0.0'],
  ]) {
    write(`${project}/node_modules/${relative}`, contents);
    write(
      `${project}/node_modules/jest/package.json`,
      JSON.stringify({ name: 'jest', version }),
    );
  }

  write(`empty/node_modules/${relative}`, contents);

  const run = (cwd, script) =>
    spawnSync(process.execPath, ['-e', script], {
      cwd: path.join(directory, cwd),
      encoding: 'utf8',
      env: { ...process.env, NODE_PATH: '' },
    });

  const detectScript = `const { detectJestVersion } = require('${relative}'); process.stdout.write(String(detectJestVersion()));`;
  const cacheScript = `const { detectJestVersion } = require('${relative}'); const fs = require('fs'); const first = detectJestVersion(); fs.writeFileSync('node_modules/jest/package.json', JSON.stringify({ name: 'jest', version: '25.0.0' })); const second = detectJestVersion(); process.stdout.write(first + ',' + second);`;
  const missingScript = `try { require('${relative}').detectJestVersion(); } catch (error) { process.stdout.write(error.message); }`;

  const backend = run('backend', detectScript);
  const frontend = run('frontend', detectScript);
  const cached = run('backend', cacheScript);
  const missing = run('empty', missingScript);

  fs.rmSync(directory, { recursive: true, force: true });

  return {
    backend: backend.stdout.trim(),
    frontend: frontend.stdout.trim(),
    cached: cached.stdout.trim(),
    missing: missing.stdout.trim(),
  };
};

const gitChangeSet = repoRoot => {
  const base = process.env.BASE_COMMIT || process.env.DIFFERENTIAL_BASE_COMMIT;
  const args = ['-C', repoRoot, 'status', '--porcelain=v1'];

  const result = spawnSync('git', args, { encoding: 'utf8' });

  if (result.error || result.status !== 0) {
    return { checked: false, files: [], disallowed: [] };
  }

  const files = result.stdout
    .split('\n')
    .filter(Boolean)
    .map(line => line.slice(3).trim())
    .map(file => file.replace(/^"|"$/gu, ''))
    .filter(Boolean);

  let changed = files;

  if (base) {
    const diff = spawnSync(
      'git',
      ['-C', repoRoot, 'diff', '--name-only', base],
      { encoding: 'utf8' },
    );

    if (!diff.error && diff.status === 0) {
      changed = diff.stdout.split('\n').filter(Boolean);
    }
  }

  const unique = Array.from(new Set(changed)).sort();
  const disallowed = unique.filter(file => !ALLOWED_CHANGE_PATHS.has(file));

  return { checked: true, files: unique, disallowed };
};

const publicCommands = repoRoot => {
  const scripts = ['test', 'typecheck', 'lint', 'prettier:check'];
  const results = {};

  for (const script of scripts) {
    const result = spawnSync('yarn', [script], {
      cwd: repoRoot,
      encoding: 'utf8',
      timeout: 900000,
      env: {
        ...process.env,
        COREPACK_ENABLE_NETWORK: '0',
        COREPACK_DEFAULT_TO_LATEST: '0',
      },
    });

    results[script] = {
      status: result.status,
      timedOut: Boolean(result.error && result.error.code === 'ETIMEDOUT'),
    };
  }

  return results;
};

const runAll = async ({ repoRoot, plugin, eslintModule }) => {
  const observations = [];
  const record = (id, requirementIds, evaluate) => {
    let result;

    try {
      result = evaluate();
    } catch (error) {
      result = {
        pass: false,
        checks: [{ name: 'probe-execution', ok: false, detail: error.stack }],
        normalized: null,
      };
    }

    observations.push({
      id,
      requirement_ids: requirementIds,
      pass: Boolean(result.pass),
      checks: result.checks || [],
      normalized: result.normalized ?? null,
    });
  };

  record('DIFF-001', ['R003'], () => {
    const meta = plugin.meta || {};
    const globals = plugin.environments?.globals?.globals || {};
    const ruleCount = Object.keys(plugin.rules || {}).length;
    const checks = [
      { name: 'meta.name', ok: meta.name === 'eslint-plugin-jest' },
      {
        name: 'meta.version-semver',
        ok: /^\d+\.\d+\.\d+(?:[-+].*)?$/u.test(meta.version || ''),
      },
      {
        name: 'six-configs',
        ok: Object.keys(plugin.configs || {}).length === 6,
      },
      { name: 'global-describe', ok: has(globals, 'describe') },
      { name: 'global-it', ok: has(globals, 'it') },
      { name: 'global-expect', ok: has(globals, 'expect') },
      { name: 'rule-count', ok: ruleCount === 71 },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: {
        name: meta.name,
        version: meta.version,
        configCount: Object.keys(plugin.configs || {}).length,
        globals: {
          describe: has(globals, 'describe'),
          it: has(globals, 'it'),
          expect: has(globals, 'expect'),
        },
        ruleCount,
      },
    };
  });

  record('DIFF-002', ['R004'], () => {
    const keys = Object.keys(plugin.configs || {});
    const checks = [{ name: 'config-keys-exact', ok: stable(keys) === stable(CONFIG_NAMES) }];

    return { pass: checks.every(check => check.ok), checks, normalized: { keys } };
  });

  record('DIFF-003', ['R007'], () => {
    const all = plugin.configs.all.rules;
    const recommended = plugin.configs.recommended.rules;
    const style = plugin.configs.style.rules;
    const recommendedExpected = Object.fromEntries(
      RECOMMENDED_KEYS.map(key => [
        key,
        RECOMMENDED_WARN.includes(key) ? 'warn' : 'error',
      ]),
    );
    const checks = [
      {
        name: 'all-count-and-severity',
        ok:
          Object.keys(all).length === 71 &&
          Object.values(all).every(level => level === 'error'),
      },
      {
        name: 'recommended-table',
        ok: stable(recommended) === stable(recommendedExpected),
      },
      { name: 'style-table', ok: stable(style) === stable(STYLE_RULES) },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: {
        counts: {
          all: Object.keys(all).length,
          recommended: Object.keys(recommended).length,
          style: Object.keys(style).length,
        },
        recommended: sortedObject(recommended),
        style: sortedObject(style),
      },
    };
  });

  record('DIFF-004', ['R008'], () => {
    const deprecated = Object.keys(plugin.rules)
      .filter(name => plugin.rules[name].meta?.deprecated)
      .map(name => `jest/${name}`);
    const nonDeprecatedCount = Object.keys(plugin.rules).length - deprecated.length;
    const checkConfig = config => {
      const keys = Object.keys(config.rules || {});
      const presentDeprecated = keys.filter(key =>
        deprecated.includes(key),
      );

      return {
        count: keys.length,
        presentDeprecated,
      };
    };
    const all = checkConfig(plugin.configs.all);
    const flatAll = checkConfig(plugin.configs['flat/all']);
    const checks = [
      { name: 'all-count', ok: all.count === nonDeprecatedCount },
      { name: 'flat-all-count', ok: flatAll.count === nonDeprecatedCount },
      { name: 'all-no-deprecated', ok: all.presentDeprecated.length === 0 },
      {
        name: 'flat-all-no-deprecated',
        ok: flatAll.presentDeprecated.length === 0,
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { deprecated, nonDeprecatedCount, all, flatAll },
    };
  });

  record('DIFF-005', ['R009'], () => {
    const recommendedExpected = Object.fromEntries(
      RECOMMENDED_KEYS.map(key => [
        key,
        RECOMMENDED_WARN.includes(key) ? 'warn' : 'error',
      ]),
    );
    const checks = [
      {
        name: 'recommended-fixed-table',
        ok: stable(plugin.configs.recommended.rules) === stable(recommendedExpected),
      },
      {
        name: 'style-fixed-table',
        ok: stable(plugin.configs.style.rules) === stable(STYLE_RULES),
      },
      {
        name: 'recommended-warns',
        ok: RECOMMENDED_WARN.every(
          key => plugin.configs.recommended.rules[key] === 'warn',
        ),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: {
        recommended: sortedObject(plugin.configs.recommended.rules),
        style: sortedObject(plugin.configs.style.rules),
      },
    };
  });

  record('DIFF-006', ['R010'], () => {
    const code = "test.only('focused', () => { expect(true).toBe(true); });";
    const recommendedDiagnostics = lintWithConfig(
      plugin,
      eslintModule,
      flattenConfig(plugin, 'recommended'),
      code,
      'fixture.test.js',
    );
    const flatDiagnostics = lintWithConfig(
      plugin,
      eslintModule,
      flattenConfig(plugin, 'flat/recommended'),
      code,
      'fixture.test.js',
    );
    const checks = [
      {
        name: 'same-rule-tables',
        ok:
          stable(plugin.configs.recommended.rules) ===
          stable(plugin.configs['flat/recommended'].rules),
      },
      {
        name: 'same-diagnostics',
        ok: stable(recommendedDiagnostics) === stable(flatDiagnostics),
      },
      { name: 'diagnostic-present', ok: flatDiagnostics.length > 0 },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: {
        recommendedDiagnostics,
        flatDiagnostics,
      },
    };
  });

  record('DIFF-007', ['R011'], () => {
    const aliased = lintRule(
      plugin,
      eslintModule,
      "context('', () => {});",
      'valid-title',
      {
        sourceType: 'module',
        settings: { jest: { globalAliases: { describe: ['context'] } } },
      },
    );
    const packaged = lintRule(
      plugin,
      eslintModule,
      "import { describe } from 'bun:test';\ndescribe('', () => {});",
      'valid-title',
      {
        sourceType: 'module',
        settings: { jest: { globalPackage: 'bun:test' } },
      },
    );
    const deprecated = lintRule(
      plugin,
      eslintModule,
      "jest.genMockFromModule('x')",
      'no-deprecated-functions',
      { settings: { jest: { version: 27 } } },
    );
    const checks = [
      {
        name: 'globalAliases-describe',
        ok: aliased.messages.some(message => message.messageId === 'emptyTitle'),
      },
      {
        name: 'globalPackage-import',
        ok: packaged.messages.some(message => message.messageId === 'emptyTitle'),
      },
      {
        name: 'version-27-mapping',
        ok:
          deprecated.messages.some(
            message => message.messageId === 'deprecatedFunction',
          ) &&
          deprecated.output === "jest.createMockFromModule('x')",
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: {
        aliased: aliased.messages,
        packaged: packaged.messages,
        deprecated,
      },
    };
  });

  record('DIFF-008', ['R012'], () => {
    const typecheck = typecheckConsumer(repoRoot);
    const checks = [{ name: 'consumer-typecheck', ok: typecheck.status === 0 }];

    return { pass: checks.every(check => check.ok), checks, normalized: typecheck };
  });

  record('DIFF-009', ['R014'], () => {
    const code = "test('x', () => {});";
    const moduleResult = lintRule(
      plugin,
      eslintModule,
      code,
      'prefer-importing-jest-globals',
      { sourceType: 'module' },
    );
    const scriptResult = lintRule(
      plugin,
      eslintModule,
      code,
      'prefer-importing-jest-globals',
      { sourceType: 'script' },
    );
    const checks = [
      {
        name: 'module-message',
        ok:
          moduleResult.messages.length === 1 &&
          moduleResult.messages[0].messageId === 'preferImportingJestGlobal' &&
          moduleResult.messages[0].message.includes('test'),
      },
      {
        name: 'module-fix',
        ok: moduleResult.output.includes(
          "import { test } from '@jest/globals';",
        ),
      },
      {
        name: 'script-fix',
        ok: scriptResult.output.includes(
          "const { test } = require('@jest/globals');",
        ),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { moduleResult, scriptResult },
    };
  });

  record('DIFF-010', ['R017'], () => {
    const allKeys = Object.keys(plugin.configs.all.rules);
    const flatKeys = Object.keys(plugin.configs['flat/all'].rules);
    const nonDeprecated = Object.keys(plugin.rules)
      .filter(name => !plugin.rules[name].meta?.deprecated)
      .map(name => `jest/${name}`)
      .sort();
    const checks = [
      { name: 'all-flat-all-same-keys', ok: stable(allKeys) === stable(flatKeys) },
      {
        name: 'exact-non-deprecated',
        ok: stable([...allKeys].sort()) === stable(nonDeprecated),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { allKeys, flatKeys, nonDeprecated },
    };
  });

  record('DIFF-011', ['R019'], () => {
    const result = lintRule(
      plugin,
      eslintModule,
      "test('x', () => {});",
      'prefer-importing-jest-globals',
      { sourceType: 'script' },
    );
    const checks = [
      {
        name: 'require-fix',
        ok: result.output.includes(
          "const { test } = require('@jest/globals');",
        ),
      },
      {
        name: 'no-import-declaration',
        ok: !result.output.includes("import { test } from '@jest/globals';"),
      },
    ];

    return { pass: checks.every(check => check.ok), checks, normalized: result };
  });

  record('DIFF-012', ['R020'], () => {
    const moduleMerged = lintRule(
      plugin,
      eslintModule,
      "import { expect } from '@jest/globals';\ntest('x', () => { expect(1).toBe(1); });",
      'prefer-importing-jest-globals',
      { sourceType: 'module' },
    );
    const requireMerged = lintRule(
      plugin,
      eslintModule,
      "const { expect } = require('@jest/globals');\ntest('x', () => { expect(1).toBe(1); });",
      'prefer-importing-jest-globals',
      { sourceType: 'script' },
    );
    const moduleStrict = lintRule(
      plugin,
      eslintModule,
      "'use strict';\nimport { expect } from '@jest/globals';\ntest('x', () => { expect(1).toBe(1); });",
      'prefer-importing-jest-globals',
      { sourceType: 'module' },
    );
    const requireStrict = lintRule(
      plugin,
      eslintModule,
      "'use strict';\nconst { expect } = require('@jest/globals');\ntest('x', () => { expect(1).toBe(1); });",
      'prefer-importing-jest-globals',
      { sourceType: 'script' },
    );
    const checks = [
      {
        name: 'module-merge',
        ok: moduleMerged.output.includes(
          "import { expect, test } from '@jest/globals';",
        ),
      },
      {
        name: 'require-merge',
        ok: requireMerged.output.includes(
          "const { expect, test } = require('@jest/globals');",
        ),
      },
      {
        name: 'module-use-strict',
        ok: moduleStrict.output.startsWith(
          "'use strict';\nimport { test } from '@jest/globals';",
        ),
      },
      {
        name: 'require-use-strict',
        ok: requireStrict.output.startsWith(
          "'use strict';\nconst { test } = require('@jest/globals');",
        ),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { moduleMerged, requireMerged, moduleStrict, requireStrict },
    };
  });

  record('DIFF-013', ['R021'], () => {
    const version25 = lintRule(
      plugin,
      eslintModule,
      "jest.genMockFromModule('x')",
      'no-deprecated-functions',
      { settings: { jest: { version: 25 } } },
    );
    const version26 = lintRule(
      plugin,
      eslintModule,
      "jest.genMockFromModule('x')",
      'no-deprecated-functions',
      { settings: { jest: { version: 26 } } },
    );
    const detected = lintRule(
      plugin,
      eslintModule,
      "jest.genMockFromModule('x')",
      'no-deprecated-functions',
      {},
    );
    const checks = [
      { name: 'version-25-quiet', ok: version25.messages.length === 0 },
      {
        name: 'version-26-report-and-fix',
        ok:
          version26.messages.some(
            message => message.messageId === 'deprecatedFunction',
          ) &&
          version26.output === "jest.createMockFromModule('x')",
      },
      {
        name: 'detected-report',
        ok: detected.messages.some(
          message => message.messageId === 'deprecatedFunction',
        ),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { version25, version26, detected },
    };
  });

  record('DIFF-014', ['R022'], () => {
    const detection = detectFixture(repoRoot);
    const checks = [
      { name: 'backend-nearest', ok: detection.backend === '24' },
      { name: 'frontend-nearest', ok: detection.frontend === '15' },
      { name: 'cached', ok: detection.cached === '24,24' },
      {
        name: 'missing-jest-message',
        ok:
          typeof detection.missing === 'string' &&
          detection.missing.includes('Unable to detect Jest version'),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: detection,
    };
  });

  record('DIFF-015', ['R023'], () => {
    const typeAware = [
      'no-error-equal',
      'no-unnecessary-assertion',
      'valid-expect-with-promise',
    ];
    const flags = Object.fromEntries(
      typeAware.map(name => [
        name,
        plugin.rules[name]?.meta?.docs?.requiresTypeChecking,
      ]),
    );
    const nonTypeAware = plugin.rules['no-focused-tests']?.meta?.docs
      ?.requiresTypeChecking;
    const checks = [
      {
        name: 'type-aware-flags',
        ok: typeAware.every(name => flags[name] === true),
      },
      {
        name: 'non-type-aware-flag-absent',
        ok: nonTypeAware !== true,
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { flags, nonTypeAware: nonTypeAware ?? null },
    };
  });

  record('DIFF-016', ['R024'], () => {
    const parser = require(require.resolve('@typescript-eslint/parser', {
      paths: [repoRoot],
    }));
    const fixturesRoot = path.join(
      repoRoot,
      'src',
      'rules',
      '__tests__',
      'fixtures',
    );
    const run = directory =>
      lintRule(
        plugin,
        eslintModule,
        'expect(1).toBeNull();',
        'no-unnecessary-assertion',
        {
          files: ['**/*.ts'],
          filename: path.join(directory, 'file.ts'),
          parser,
          parserOptions: {
            project: './tsconfig.json',
            tsconfigRootDir: directory,
            disallowAutomaticSingleRunInference: true,
          },
        },
      );
    const unstrict = run(path.join(fixturesRoot, 'unstrict'));
    const strict = run(fixturesRoot);
    const checks = [
      {
        name: 'unstrict-noStrictNullCheck',
        ok: unstrict.messages.some(
          message => message.messageId === 'noStrictNullCheck',
        ),
      },
      {
        name: 'strict-no-noStrictNullCheck',
        ok: !strict.messages.some(
          message => message.messageId === 'noStrictNullCheck',
        ),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { unstrict: unstrict.messages, strict: strict.messages },
    };
  });

  record('DIFF-017', ['R025'], () => {
    const metadata = {};

    for (const name of Object.keys(plugin.rules).sort()) {
      const meta = plugin.rules[name].meta || {};
      metadata[name] = {
        type: meta.type,
        fixable: meta.fixable ?? null,
        messages: meta.messages,
        schema: meta.schema,
        deprecated: Boolean(meta.deprecated),
        docs: {
          description: meta.docs?.description ?? null,
          requiresTypeChecking:
            meta.docs?.requiresTypeChecking === true ? true : null,
        },
      };
    }

    const focused = lintRule(
      plugin,
      eslintModule,
      "test.only('x', () => {})",
      'no-focused-tests',
      {},
    );
    const checks = [
      { name: 'rule-count', ok: Object.keys(metadata).length === 71 },
      {
        name: 'representative-message',
        ok:
          focused.messages.length === 1 &&
          focused.messages[0].messageId === 'focusedTest' &&
          focused.messages[0].severity === 2,
      },
      {
        name: 'representative-suggestion',
        ok:
          focused.messages[0]?.suggestions.some(
            suggestion => suggestion.messageId === 'suggestRemoveFocus',
          ) === true,
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { metadata, focused },
    };
  });

  record('DIFF-018', ['R026'], () => {
    const withoutPrefix = key => key.replace(/^jest\//u, '');
    const ruleKeys = Object.keys(plugin.rules).sort();
    const allKeys = Object.keys(plugin.configs.all.rules)
      .map(withoutPrefix)
      .sort();
    const recommendedKeys = Object.keys(plugin.configs.recommended.rules)
      .map(withoutPrefix)
      .sort();
    const styleKeys = Object.keys(plugin.configs.style.rules)
      .map(withoutPrefix)
      .sort();
    const checks = [
      { name: 'rule-count', ok: ruleKeys.length === 71 },
      { name: 'all-matches-rules', ok: stable(allKeys) === stable(ruleKeys) },
      {
        name: 'recommended-subset',
        ok: recommendedKeys.every(key => ruleKeys.includes(key)),
      },
      {
        name: 'style-subset',
        ok: styleKeys.every(key => ruleKeys.includes(key)),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { ruleKeys, allKeys, recommendedKeys, styleKeys },
    };
  });

  record('DIFF-019', ['R027'], () => {
    const numeric = lintRule(
      plugin,
      eslintModule,
      "jest.genMockFromModule('x')",
      'no-deprecated-functions',
      { settings: { jest: { version: 27 } } },
    );
    const textual = lintRule(
      plugin,
      eslintModule,
      "jest.genMockFromModule('x')",
      'no-deprecated-functions',
      { settings: { jest: { version: '27.4.0' } } },
    );
    const aliased = lintRule(
      plugin,
      eslintModule,
      "context('', () => {});",
      'valid-title',
      {
        sourceType: 'module',
        settings: { jest: { globalAliases: { describe: ['context'] } } },
      },
    );
    const packaged = lintRule(
      plugin,
      eslintModule,
      "import { describe } from 'bun:test';\ndescribe('', () => {});",
      'valid-title',
      {
        sourceType: 'module',
        settings: { jest: { globalPackage: 'bun:test' } },
      },
    );
    const detection = detectFixture(repoRoot);
    const checks = [
      {
        name: 'number-and-string-version',
        ok: stable(numeric.messages) === stable(textual.messages),
      },
      {
        name: 'globalAliases',
        ok: aliased.messages.some(message => message.messageId === 'emptyTitle'),
      },
      {
        name: 'globalPackage',
        ok: packaged.messages.some(message => message.messageId === 'emptyTitle'),
      },
      { name: 'detection-cache', ok: detection.cached === '24,24' },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { numeric, textual, aliased, packaged, detection },
    };
  });

  record('DIFF-020', ['R032'], () => {
    const changeSet = gitChangeSet(repoRoot);
    const checks = [
      {
        name: 'only-allowed-paths',
        ok: !changeSet.checked || changeSet.disallowed.length === 0,
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: changeSet,
    };
  });

  record('DIFF-021', ['R033'], () => {
    const file = path.join(repoRoot, 'src', '__tests__', 'rules.test.ts');
    const text = fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
    const skipped = /(?:it|test|describe)\.skip\s*\(/u.test(text);
    const sixNames = CONFIG_NAMES.every(name => text.includes(`'${name}'`));
    const flatProperties =
      text.includes('plugins.jest') &&
      text.includes('languageOptions.globals') &&
      /not\.toHaveProperty\(\s*['"]env['"]\s*\)/u.test(text);
    const checks = [
      { name: 'no-skipped-checks', ok: !skipped },
      { name: 'six-config-names', ok: sixNames },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { skipped, sixNames, flatProperties },
    };
  });

  record('DIFF-022', ['R036'], () => {
    const packageJsonPath = path.join(repoRoot, 'package.json');
    const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'));
    const required = ['test', 'typecheck', 'lint', 'prettier:check'];
    const scriptsPresent = required.every(
      script => typeof packageJson.scripts?.[script] === 'string',
    );
    const shouldRun = process.env.DIFFERENTIAL_RUN_PUBLIC_COMMANDS === '1';
    const results = shouldRun ? publicCommands(repoRoot) : null;
    const checks = [
      { name: 'scripts-declared', ok: scriptsPresent },
      {
        name: 'commands-exit-zero',
        ok:
          !results ||
          Object.values(results).every(result => result.status === 0),
      },
    ];

    return {
      pass: checks.every(check => check.ok),
      checks,
      normalized: { scriptsPresent, ran: shouldRun, results },
    };
  });

  return {
    schema_version: 'migration-asset-fragment:v2',
    batch_id: BATCH_ID,
    observations,
    pass: observations.every(observation => observation.pass),
  };
};

module.exports = { runAll, BATCH_ID, CONFIG_NAMES };
