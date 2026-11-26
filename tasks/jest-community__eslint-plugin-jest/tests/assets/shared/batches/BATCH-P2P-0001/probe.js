'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

const BATCH_ID = 'BATCH-P2P-0001';

const JEST_OBSERVATIONS = [
  { id: 'P2P-001', file: 'src/rules/__tests__/no-duplicate-hooks.test.ts', pattern: 'is fine' },
  { id: 'P2P-002', file: 'src/rules/__tests__/prefer-expect-resolves.test.ts', pattern: 'errors' },
  { id: 'P2P-003', file: 'src/rules/__tests__/prefer-lowercase-title.test.ts', pattern: 'Booleans' },
  { id: 'P2P-004', file: 'src/rules/__tests__/no-duplicate-hooks.test.ts', pattern: 'inner bar' },
  { id: 'P2P-005', file: 'src/rules/utils/__tests__/detectJestVersion.test.ts', pattern: 'throws an error' },
  { id: 'P2P-006', file: 'src/rules/__tests__/no-conditional-in-test.test.ts', pattern: 'is invalid' },
  { id: 'P2P-007', file: 'src/rules/__tests__/require-top-level-describe.test.ts', pattern: 'my other test' },
  { id: 'P2P-008', file: 'src/rules/__tests__/valid-expect-with-promise.test.ts', pattern: 'is true' },
  { id: 'P2P-009', file: 'src/rules/__tests__/padding-around-describe-blocks.test.ts', pattern: 'some other condition' },
  { id: 'P2P-010', file: 'src/rules/__tests__/require-hook.test.ts', pattern: 'when loading cities from the api' },
  { id: 'P2P-011', file: 'src/rules/__tests__/no-done-callback.test.ts', pattern: 'my test' },
  { id: 'P2P-012', file: 'src/rules/__tests__/prefer-expect-resolves.test.ts', pattern: 'is true' },
  { id: 'P2P-013', file: 'src/rules/__tests__/valid-title.test.ts', pattern: 'is correct' },
  { id: 'P2P-014', file: 'src/rules/__tests__/max-nested-describe.test.ts', pattern: 'baz2' },
  { id: 'P2P-016', file: 'src/rules/__tests__/require-to-throw-message.test.ts', pattern: 'empty rejects.toThrow' },
  { id: 'P2P-017', file: 'src/rules/__tests__/prefer-ending-with-an-expect.test.ts', pattern: 'responds with json' },
  { id: 'P2P-020', file: 'src/__tests__/rules.test.ts', pattern: 'should have a corresponding doc for each rule' },
];

const CUSTOM_OBSERVATIONS = ['P2P-015', 'P2P-018', 'P2P-019', 'P2P-021', 'P2P-022', 'P2P-023', 'P2P-024'];

const OBSERVATION_ORDER = [
  'P2P-008',
  'P2P-015',
  'P2P-018',
  'P2P-019',
  'P2P-021',
  'P2P-022',
  'P2P-023',
  'P2P-024',
  'P2P-002',
  'P2P-016',
  'P2P-017',
  'P2P-020',
  'P2P-001',
  'P2P-003',
  'P2P-004',
  'P2P-005',
  'P2P-006',
  'P2P-007',
  'P2P-009',
  'P2P-010',
  'P2P-011',
  'P2P-012',
  'P2P-013',
  'P2P-014',
];

const stripAnsi = value => value.replace(/\u001B\[[0-9;]*m/gu, '');

const runJestObservation = (root, observation) => {
  const jest = path.join(root, 'node_modules', 'jest', 'bin', 'jest.js');
  const result = spawnSync(
    process.execPath,
    [
      jest,
      '--ci',
      '--rootDir',
      root,
      '--runTestsByPath',
      path.join(root, observation.file),
      '--selectProjects',
      'test',
      '--testNamePattern',
      observation.pattern,
      '--cacheDirectory',
      path.join('/tmp', `${BATCH_ID}-${observation.id}`),
    ],
    {
      cwd: root,
      encoding: 'utf8',
      env: { ...process.env, CI: '1', FORCE_COLOR: '0', NO_COLOR: '1' },
      timeout: 120000,
    },
  );
  const output = stripAnsi(`${result.stdout}\n${result.stderr}`);
  const summary = (output.match(/Tests:\s+([^\n]+)/u) || [])[1] || '';
  const passed = Number.parseInt((summary.match(/(\d+) passed/u) || [])[1] || '0', 10);
  const failed = Number.parseInt((summary.match(/(\d+) failed/u) || [])[1] || '0', 10);

  return {
    observation_id: observation.id,
    mode: 'jest',
    source_path: observation.file,
    test_name_pattern: observation.pattern,
    passed,
    failed,
    raw_status: result.status,
    status: result.status === 0 && failed === 0 && passed > 0 ? 'PASS' : 'FAIL',
    projection: {
      file: observation.file,
      test_name_pattern: observation.pattern,
      passed: result.status === 0 && failed === 0 && passed > 0,
    },
  };
};

const loadPlugin = root => {
  process.env.TS_NODE_PROJECT = process.env.TS_NODE_PROJECT || path.join(root, 'tsconfig.json');
  process.env.TS_NODE_TRANSPILE_ONLY = 'true';
  require(require.resolve('ts-node/register/transpile-only', { paths: [root] }));
  return require(path.join(root, 'src', 'index.ts'));
};

const createLinter = root => {
  const eslint = require(require.resolve('eslint', { paths: [root] }));
  return new eslint.Linter();
};

const sorted = values => [...values].sort();
const sortedObject = object =>
  Object.fromEntries(
    Object.entries(object).sort(([left], [right]) => left.localeCompare(right)),
  );

const runCustomObservations = root => {
  const plugin = loadPlugin(root);
  const linter = createLinter(root);
  const configNames = ['all', 'recommended', 'style', 'flat/all', 'flat/recommended', 'flat/style'];
  const ruleKeys = sorted(Object.keys(plugin.rules));
  const allKeys = sorted(Object.keys(plugin.configs.all.rules));
  const flatAllKeys = sorted(Object.keys(plugin.configs['flat/all'].rules));
  const deprecated = sorted(
    Object.entries(plugin.rules)
      .filter(([, rule]) => rule.meta && rule.meta.deprecated)
      .map(([name]) => name),
  );
  const nonDeprecated = sorted(
    ruleKeys
      .filter(name => !(plugin.rules[name].meta && plugin.rules[name].meta.deprecated))
      .map(name => `jest/${name}`),
  );
  const tables = Object.fromEntries(
    configNames.map(name => [name, sortedObject(plugin.configs[name].rules)]),
  );

  const toMessages = messages =>
    messages.map(message => ({
      ruleId: message.ruleId,
      messageId: message.messageId,
      message: message.message,
      line: message.line,
      column: message.column,
      fix: message.fix
        ? { range: message.fix.range, text: message.fix.text }
        : undefined,
      suggestions: message.suggestions
        ? message.suggestions.map(suggestion => ({
            messageId: suggestion.messageId,
            desc: suggestion.desc,
            fix: suggestion.fix
              ? { range: suggestion.fix.range, text: suggestion.fix.text }
              : undefined,
          }))
        : undefined,
    }));

  const lint = (code, options) => {
    const config = {
      plugins: { jest: plugin },
      languageOptions: { sourceType: options.sourceType || 'script' },
      rules: options.rules,
      ...(options.settings ? { settings: options.settings } : {}),
    };

    return toMessages(linter.verify(code, config, { filename: 'fixture.test.js' }));
  };

  const fix = (code, options) => {
    const config = {
      plugins: { jest: plugin },
      languageOptions: { sourceType: options.sourceType || 'script' },
      rules: options.rules,
      ...(options.settings ? { settings: options.settings } : {}),
    };
    const result = linter.verifyAndFix(code, config, { filename: 'fixture.test.js' });

    return {
      fixed: result.fixed,
      output: result.output,
      messages: toMessages(result.messages),
    };
  };

  const preferRule = { 'jest/prefer-importing-jest-globals': 'error' };
  const deprecatedRule = { 'jest/no-deprecated-functions': 'error' };

  const observations = [
    {
      observation_id: 'P2P-015',
      mode: 'custom',
      source_path: 'src/index.ts',
      status:
        JSON.stringify(allKeys) === JSON.stringify(flatAllKeys) &&
        JSON.stringify(allKeys) === JSON.stringify(nonDeprecated) &&
        deprecated.length === 0
          ? 'PASS'
          : 'FAIL',
      projection: {
        allKeys,
        flatAllKeys,
        nonDeprecated,
        deprecated,
        allEqualsFlatAll: JSON.stringify(allKeys) === JSON.stringify(flatAllKeys),
        allEqualsNonDeprecated: JSON.stringify(allKeys) === JSON.stringify(nonDeprecated),
      },
    },
    {
      observation_id: 'P2P-018',
      mode: 'custom',
      source_path: 'src/index.ts',
      status: ruleKeys.length === 71 ? 'PASS' : 'FAIL',
      projection: {
        ruleKeys,
        all: tables.all,
        recommended: tables.recommended,
        style: tables.style,
      },
    },
    {
      observation_id: 'P2P-019',
      mode: 'custom',
      source_path: 'src/rules/prefer-importing-jest-globals.ts',
      status: 'PASS',
      projection: {
        module: {
          messages: lint("test('x', () => {});", { sourceType: 'module', rules: preferRule }),
          fix: fix("test('x', () => {});", { sourceType: 'module', rules: preferRule }),
        },
        script: {
          messages: lint("test('x', () => {});", { sourceType: 'script', rules: preferRule }),
          fix: fix("test('x', () => {});", { sourceType: 'script', rules: preferRule }),
        },
      },
    },
    {
      observation_id: 'P2P-021',
      mode: 'custom',
      source_path: 'src/rules/prefer-importing-jest-globals.ts',
      status: 'PASS',
      projection: {
        messages: lint("test('x', () => {});", { sourceType: 'script', rules: preferRule }),
        fix: fix("test('x', () => {});", { sourceType: 'script', rules: preferRule }),
      },
    },
    {
      observation_id: 'P2P-022',
      mode: 'custom',
      source_path: 'src/index.ts',
      status:
        allKeys.length === 71 &&
        Object.values(tables.all).every(severity => severity === 'error') &&
        Object.keys(tables.recommended).length === 19 &&
        Object.keys(tables.style).length === 3
          ? 'PASS'
          : 'FAIL',
      projection: {
        allKeys,
        allValues: sorted(Object.values(tables.all)),
        recommended: tables.recommended,
        style: tables.style,
      },
    },
    {
      observation_id: 'P2P-023',
      mode: 'custom',
      source_path: 'src/rules/prefer-importing-jest-globals.ts',
      status: 'PASS',
      projection: {
        moduleImportMerge: fix(
          "import { expect } from '@jest/globals';\ntest('x', () => { expect(1).toBe(1); });",
          { sourceType: 'module', rules: preferRule },
        ),
        scriptRequireMerge: fix(
          "const { expect } = require('@jest/globals');\ntest('x', () => { expect(1).toBe(1); });",
          { sourceType: 'script', rules: preferRule },
        ),
        useStrictModule: fix("'use strict';\ntest('x', () => {});", {
          sourceType: 'module',
          rules: preferRule,
        }),
        useStrictScript: fix("'use strict';\ntest('x', () => {});", {
          sourceType: 'script',
          rules: preferRule,
        }),
        useStrictWithExistingImport: fix(
          "'use strict';\nimport { expect } from '@jest/globals';\ntest('x', () => { expect(1).toBe(1); });",
          { sourceType: 'module', rules: preferRule },
        ),
      },
    },
    {
      observation_id: 'P2P-024',
      mode: 'custom',
      source_path: 'src/rules/no-deprecated-functions.ts',
      status: 'PASS',
      projection: {
        v25: {
          messages: lint("jest.genMockFromModule('x')", {
            settings: { jest: { version: 25 } },
            rules: deprecatedRule,
          }),
          fix: fix("jest.genMockFromModule('x')", {
            settings: { jest: { version: 25 } },
            rules: deprecatedRule,
          }),
        },
        v26: {
          messages: lint("jest.genMockFromModule('x')", {
            settings: { jest: { version: 26 } },
            rules: deprecatedRule,
          }),
          fix: fix("jest.genMockFromModule('x')", {
            settings: { jest: { version: 26 } },
            rules: deprecatedRule,
          }),
        },
        auto: {
          messages: lint("jest.genMockFromModule('x')", { rules: deprecatedRule }),
          fix: fix("jest.genMockFromModule('x')", { rules: deprecatedRule }),
        },
      },
    },
  ];

  return observations;
};

const run = ({ root, side }) => {
  const results = [
    ...JEST_OBSERVATIONS.map(observation => runJestObservation(root, observation)),
    ...runCustomObservations(root),
  ];
  const byId = Object.fromEntries(results.map(result => [result.observation_id, result]));

  return {
    schema_version: 'migration-p2p-probe-report:v1',
    batch_id: BATCH_ID,
    side,
    root,
    observations: OBSERVATION_ORDER,
    results: OBSERVATION_ORDER.map(id => byId[id]),
    all_pass: results.every(result => result.status === 'PASS'),
  };
};

module.exports = {
  BATCH_ID,
  CUSTOM_OBSERVATIONS,
  JEST_OBSERVATIONS,
  run,
};

if (require.main === module) {
  const root = process.argv[2] || process.cwd();
  const side = process.argv[3] || 'unknown';
  const report = run({ root, side });

  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.exitCode = report.all_pass ? 0 : 1;
}
