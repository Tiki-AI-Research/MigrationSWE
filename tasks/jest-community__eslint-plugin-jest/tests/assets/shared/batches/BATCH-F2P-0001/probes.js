'use strict';

const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');
const { spawnSync } = require('child_process');

const OBSERVATION_IDS = [
  'F2P-001',
  'F2P-002',
  'F2P-004',
  'F2P-005',
  'F2P-006',
  'F2P-007',
  'F2P-008',
  'F2P-011',
  'F2P-012',
  'F2P-014',
  'F2P-015',
  'F2P-022',
  'F2P-009',
  'F2P-010',
  'F2P-003',
  'F2P-016',
  'F2P-017',
  'F2P-018',
  'F2P-019',
  'F2P-020',
  'F2P-021',
  'F2P-013',
  'F2P-023',
  'F2P-024',
];

const CONFIG_NAMES = [
  'all',
  'recommended',
  'style',
  'flat/all',
  'flat/recommended',
  'flat/style',
];

const LEGACY_CONFIG_NAMES = ['all', 'recommended', 'style'];

class ProbeFailure extends Error {
  constructor(message) {
    super(message);
    this.name = 'ProbeFailure';
  }
}

function fail(message) {
  throw new ProbeFailure(message);
}

function assert(condition, message) {
  if (!condition) {
    fail(message);
  }
}

function readFile(root, relativePath) {
  return fs.readFileSync(path.join(root, relativePath), 'utf8');
}

function runCommand(root, bin, args) {
  const nodePath = [path.join(root, 'node_modules'), process.env.NODE_PATH]
    .filter(Boolean)
    .join(path.delimiter);

  return spawnSync(
    process.execPath,
    [path.join(root, 'node_modules', '.bin', bin), ...args],
    {
      cwd: root,
      encoding: 'utf8',
      maxBuffer: 64 * 1024 * 1024,
      env: { ...process.env, NODE_PATH: nodePath },
    },
  );
}

function ensureBuilt(root) {
  const entry = path.join(root, 'lib', 'index.js');

  if (fs.existsSync(entry)) {
    return;
  }

  const build = runCommand(root, 'babel', [
    '--extensions',
    '.js,.ts',
    'src',
    '--out-dir',
    'lib',
    '--copy-files',
  ]);

  if (build.status !== 0) {
    const error = new Error(
      `Unable to build plugin for ${root}: ${build.stderr || build.stdout}`,
    );
    error.code = 'INFRA';
    throw error;
  }

  const cleanup = runCommand(root, 'rimraf', [
    '--glob',
    'lib/__tests__',
    'lib/**/__tests__',
  ]);

  if (cleanup.status !== 0) {
    const error = new Error(
      `Unable to clean built tests for ${root}: ${cleanup.stderr || cleanup.stdout}`,
    );
    error.code = 'INFRA';
    throw error;
  }
}

function createContext(root) {
  ensureBuilt(root);

  const requireFromRoot = createRequire(path.join(root, 'package.json'));
  const plugin = requireFromRoot(path.join(root, 'lib', 'index.js'));
  const eslintPackage = requireFromRoot('eslint/package.json');
  const { ESLint, Linter } = requireFromRoot('eslint');
  let parser;

  try {
    parser = requireFromRoot('@typescript-eslint/parser');
  } catch (error) {
    parser = null;
  }

  return {
    root,
    plugin,
    ESLint,
    Linter,
    parser,
    requireFromRoot,
    packageJson: JSON.parse(readFile(root, 'package.json')),
    eslintVersion: eslintPackage.version,
  };
}

async function lintText(context, config, code, filePath, options) {
  const eslint = new context.ESLint({
    overrideConfigFile: true,
    overrideConfig: [config],
    cwd: context.root,
    ...(options || {}),
  });

  const [result] = await eslint.lintText(code, { filePath });

  return result;
}

function fixtureRoot(context) {
  return path.join(context.root, 'src', 'rules', '__tests__', 'fixtures');
}

function typedConfig(context, config, rules) {
  const root = fixtureRoot(context);

  return {
    files: ['**/*.ts'],
    ...config,
    languageOptions: {
      ...(config.languageOptions || {}),
      globals: context.plugin.environments.globals.globals,
      parser: context.parser,
      parserOptions: {
        project: './tsconfig.json',
        tsconfigRootDir: root,
      },
    },
    rules: {
      ...(config.rules || {}),
      ...(rules || {}),
    },
  };
}

function configShapeViolations(context, names) {
  const violations = [];

  for (const name of names) {
    const config = context.plugin.configs[name];

    if (!config) {
      violations.push(`${name}: missing config`);
      continue;
    }

    if (Object.prototype.hasOwnProperty.call(config, 'env')) {
      violations.push(`${name}: still exposes env`);
    }

    if (
      !config.plugins ||
      Array.isArray(config.plugins) ||
      config.plugins.jest !== context.plugin
    ) {
      violations.push(`${name}: plugins is not the flat object { jest: plugin }`);
    }

    if (
      !config.languageOptions ||
      !config.languageOptions.globals ||
      typeof config.languageOptions.globals !== 'object'
    ) {
      violations.push(`${name}: missing languageOptions.globals`);
    }

    if (!config.rules || typeof config.rules !== 'object') {
      violations.push(`${name}: missing rules table`);
    }
  }

  return violations;
}

async function assertConfigsLoad(context, names) {
  assert(context.parser, 'TypeScript parser is unavailable');

  for (const name of names) {
    const config = typedConfig(context, context.plugin.configs[name]);
    const filePath = path.join(fixtureRoot(context), 'file.ts');
    let result;

    try {
      result = await lintText(
        context,
        config,
        "test.only('focused', () => { expect(true).toBe(true); });",
        filePath,
      );
    } catch (error) {
      fail(`${name}: ESLint 10 rejected the config: ${error.message}`);
    }

    assert(
      result && Array.isArray(result.messages),
      `${name}: ESLint did not return a messages array`,
    );
  }
}

async function assertFlatConfigShapes(context, names) {
  const violations = configShapeViolations(context, names);

  assert(
    violations.length === 0,
    `flat config shape violations: ${violations.join('; ')}`,
  );
  await assertConfigsLoad(context, names);
}

async function probeAllConfigs(context) {
  await assertFlatConfigShapes(context, CONFIG_NAMES);
}

async function probeLegacyNames(context) {
  await assertFlatConfigShapes(context, LEGACY_CONFIG_NAMES);
}

async function probeAllAndFlatAll(context) {
  await assertFlatConfigShapes(context, ['all', 'flat/all']);
}

function probeCiMatrix(context) {
  const workflow = readFile(context.root, '.github/workflows/nodejs.yml');
  const matrix = /eslint-version:\s*\[([^\]]*)\]/.exec(workflow);

  assert(matrix, 'CI workflow has no eslint-version matrix');
  assert(
    matrix[1]
      .split(',')
      .map(value => value.trim())
      .includes('10.5.0'),
    `CI matrix does not pin 10.5.0: ${matrix[1].trim()}`,
  );
  assert(
    workflow.includes('eslint@${{ matrix.eslint-version }}'),
    'CI workflow does not install the matrix eslint version',
  );
  assert(
    context.packageJson.devDependencies &&
      context.packageJson.devDependencies.eslint === '10.5.0',
    'package.json does not dev-depend on eslint 10.5.0',
  );
  assert(
    !(
      context.packageJson.devDependencies &&
      context.packageJson.devDependencies['@types/eslint']
    ),
    'package.json still dev-depends on @types/eslint',
  );
}

function probeTypeDeclarations(context) {
  const declarations = readFile(context.root, 'types.d.ts');

  assert(
    /(?:ESLint\.)?Linter\.Config\b/.test(declarations),
    'types.d.ts does not declare plugin configs with ESLint 10 Linter.Config',
  );
  assert(
    !declarations.includes('LegacyConfig'),
    'types.d.ts still references LegacyConfig',
  );
  assert(
    !declarations.includes('FlatConfig'),
    'types.d.ts still references FlatConfig',
  );

  const typecheck = runCommand(context.root, 'tsc', ['-p', '.']);

  assert(
    typecheck.status === 0,
    `tsc failed: ${(typecheck.stderr || typecheck.stdout || '').trim()}`,
  );
}

async function probeGlobals(context) {
  assert(context.parser, 'TypeScript parser is unavailable');

  const filePath = path.join(fixtureRoot(context), 'file.ts');
  const code =
    "describe('d', () => { it('i', () => { expect(1).toBe(1); }); });";

  for (const name of CONFIG_NAMES) {
    const config = typedConfig(context, context.plugin.configs[name], {
      'no-undef': 'error',
    });
    let result;

    try {
      result = await lintText(context, config, code, filePath);
    } catch (error) {
      fail(`${name}: ESLint 10 could not load the config: ${error.message}`);
    }

    const undef = result.messages.filter(message => message.ruleId === 'no-undef');

    assert(
      undef.length === 0,
      `${name}: Jest globals were not injected: ${undef
        .map(message => message.message)
        .join('; ')}`,
    );
  }

  const withoutGlobals = {
    files: ['**/*.ts'],
    languageOptions: {
      parser: context.parser,
      parserOptions: {
        project: './tsconfig.json',
        tsconfigRootDir: fixtureRoot(context),
      },
    },
    rules: {
      'no-undef': 'error',
    },
  };
  const control = await lintText(context, withoutGlobals, code, filePath);
  const controlUndef = control.messages.filter(
    message => message.ruleId === 'no-undef',
  );

  assert(
    controlUndef.length >= 3,
    'negative control did not report describe/it/expect as undefined',
  );
}

async function probeRecommendedDiagnostic(context) {
  const config = {
    files: ['**/*.test.js'],
    ...context.plugin.configs.recommended,
  };
  let result;

  try {
    result = await lintText(
      context,
      config,
      "test.only('focused', () => { expect(true).toBe(true); });",
      'sample.test.js',
    );
  } catch (error) {
    fail(`ESLint 10 rejected plugin.configs.recommended: ${error.message}`);
  }

  const focused = result.messages.find(
    message => message.ruleId === 'jest/no-focused-tests',
  );

  assert(focused, 'recommended did not report jest/no-focused-tests');
  assert(focused.severity === 2, 'jest/no-focused-tests is not an error');
}

function probeDependencyPolicy(context) {
  const packageJson = context.packageJson;
  const lockfile = readFile(context.root, 'yarn.lock');

  assert(
    packageJson.devDependencies &&
      packageJson.devDependencies.eslint === '10.5.0',
    'package.json does not dev-depend on eslint 10.5.0',
  );
  assert(
    !(
      packageJson.devDependencies &&
      packageJson.devDependencies['@types/eslint']
    ),
    'package.json still dev-depends on @types/eslint',
  );
  assert(
    lockfile.includes('resolution: "eslint@npm:10.5.0"'),
    'yarn.lock does not resolve eslint@npm:10.5.0',
  );
  assert(
    !/resolution: "eslint@npm:9\./.test(lockfile),
    'yarn.lock still resolves an eslint 9 package',
  );
  assert(
    context.eslintVersion === '10.5.0',
    `installed eslint is ${context.eslintVersion}, expected 10.5.0`,
  );
}

async function probeInstalledDependency(context) {
  probeDependencyPolicy(context);
  await probeRecommendedDiagnostic(context);
}

function probeEslintConfig(context) {
  const source = readFile(context.root, 'eslint.config.js');

  assert(
    /@type\s+\{\s*import\(['"]eslint['"]\)\.Linter\.Config\[\]\s*\}/.test(
      source,
    ),
    'eslint.config.js does not use the Linter.Config[] JSDoc type',
  );
  assert(
    !/\bLinter\.(FlatConfig|LegacyConfig)\b/.test(source),
    'eslint.config.js still references removed legacy config types',
  );

  let config;

  try {
    config = context.requireFromRoot(path.join(context.root, 'eslint.config.js'));
  } catch (error) {
    fail(`eslint.config.js could not be loaded: ${error.message}`);
  }

  assert(
    Array.isArray(config),
    'eslint.config.js does not export a flat config array',
  );
}

function probeReadme(context) {
  const readme = readFile(context.root, 'README.md');

  assert(
    !/"plugins"\s*:\s*\[\s*"jest"\s*\]/.test(readme),
    'README still presents the legacy plugins: ["jest"] shape',
  );
  assert(
    !/"env"\s*:\s*\{/.test(readme) &&
      !/env:\s*\{\s*['"]jest\/globals['"]/.test(readme),
    'README still presents the legacy env: { "jest/globals": true } shape',
  );
  assert(
    /jest\.configs\.(recommended|all|style)|jest\.configs\[\s*['"]flat\//.test(
      readme,
    ),
    'README does not show a flat config usage example',
  );
  assert(
    /module\.exports\s*=\s*\[/.test(readme),
    'README does not show an eslint.config.js array example',
  );
}

async function fixFor(context, code, languageOptions) {
  const config = {
    files: ['**/*.js'],
    plugins: { jest: context.plugin },
    languageOptions,
    rules: {
      'jest/prefer-importing-jest-globals': 'error',
    },
  };
  let result;

  try {
    result = await lintText(context, config, code, 'sample.js', { fix: true });
  } catch (error) {
    fail(`prefer-importing-jest-globals could not run: ${error.message}`);
  }

  return result.output || code;
}

async function fixForSourceLegacy(context, code) {
  const linter = new context.Linter({ configType: 'eslintrc' });
  linter.defineRule(
    'jest/prefer-importing-jest-globals',
    context.plugin.rules['prefer-importing-jest-globals'],
  );

  const result = linter.verifyAndFix(
    code,
    {
      parserOptions: { sourceType: 'module', ecmaVersion: 2015 },
      rules: {
        'jest/prefer-importing-jest-globals': 'error',
      },
    },
    { filename: 'sample.js' },
  );

  return result.output;
}

async function probeSourceTypeFix(context) {
  const code = "test('x', () => {});";
  const moduleOutput = await fixFor(context, code, { sourceType: 'module' });
  const major = Number.parseInt(context.eslintVersion.split('.')[0], 10);
  const parserOptionsOutput =
    major < 10
      ? await fixForSourceLegacy(context, code)
      : await fixFor(context, code, {
          sourceType: 'script',
          parserOptions: { sourceType: 'module' },
        });

  assert(
    moduleOutput.includes("import { test } from '@jest/globals';"),
    `module sourceType did not produce an import fix: ${moduleOutput}`,
  );
  assert(
    !moduleOutput.includes('require('),
    `module sourceType unexpectedly produced a require fix: ${moduleOutput}`,
  );
  assert(
    parserOptionsOutput.includes(
      "const { test } = require('@jest/globals');",
    ),
    `parserOptions-only module did not produce a require fix in the target: ${parserOptionsOutput}`,
  );
  assert(
    !parserOptionsOutput.includes('import { test }'),
    `parserOptions-only module unexpectedly produced an import fix: ${parserOptionsOutput}`,
  );
}

async function probeStrictNullChecks(context) {
  assert(context.parser, 'TypeScript parser is unavailable');
  const root = path.join(fixtureRoot(context), 'unstrict');
  const config = {
    files: ['**/*.ts'],
    plugins: { jest: context.plugin },
    languageOptions: {
      globals: context.plugin.environments.globals.globals,
      parser: context.parser,
      parserOptions: {
        project: './tsconfig.json',
        tsconfigRootDir: root,
      },
    },
    rules: {
      'jest/no-unnecessary-assertion': 'error',
    },
  };
  const result = await lintText(
    context,
    config,
    'expect(1).toBeNull();',
    path.join(root, 'file.ts'),
  );

  assert(
    result.messages.some(message => message.messageId === 'noStrictNullCheck'),
    'no-unnecessary-assertion did not report noStrictNullCheck',
  );
}

const PROBES = {
  'F2P-001': probeLegacyNames,
  'F2P-002': probeSourceTypeFix,
  'F2P-003': probeLegacyNames,
  'F2P-004': probeAllConfigs,
  'F2P-005': probeAllAndFlatAll,
  'F2P-006': probeCiMatrix,
  'F2P-007': probeTypeDeclarations,
  'F2P-008': probeGlobals,
  'F2P-009': probeGlobals,
  'F2P-010': probeAllConfigs,
  'F2P-011': probeRecommendedDiagnostic,
  'F2P-012': probeDependencyPolicy,
  'F2P-013': probeGlobals,
  'F2P-014': probeInstalledDependency,
  'F2P-015': probeEslintConfig,
  'F2P-016': probeAllConfigs,
  'F2P-017': probeGlobals,
  'F2P-018': probeAllConfigs,
  'F2P-019': probeLegacyNames,
  'F2P-020': probeSourceTypeFix,
  'F2P-021': probeAllConfigs,
  'F2P-022': probeReadme,
  'F2P-023': probeAllConfigs,
  'F2P-024': async context => {
    await probeStrictNullChecks(context);
    await probeLegacyNames(context);
  },
};

async function runProbes(root, side) {
  const context = createContext(root);
  const results = [];

  for (const observationId of OBSERVATION_IDS) {
    const probe = PROBES[observationId];

    try {
      await probe(context);
      results.push({
        observation_id: observationId,
        status: 'PASS',
        detail: `${side} satisfied the target postcondition`,
      });
    } catch (error) {
      const status =
        error && error.code === 'INFRA' ? 'INFRA_ERROR' : 'FAIL';
      results.push({
        observation_id: observationId,
        status,
        detail: error && error.message ? error.message : String(error),
      });
    }
  }

  return results;
}

module.exports = {
  OBSERVATION_IDS,
  PROBES,
  ProbeFailure,
  runProbes,
};
