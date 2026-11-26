'use strict';

const fs = require('fs');
const path = require('path');

const CONFIG_NAMES = [
  'all',
  'recommended',
  'style',
  'flat/all',
  'flat/recommended',
  'flat/style',
];

const OBSERVATIONS = [
  'F2P-036',
  'F2P-030',
  'F2P-025',
  'F2P-027',
  'F2P-028',
  'F2P-029',
  'F2P-031',
  'F2P-032',
  'F2P-034',
  'F2P-037',
  'F2P-038',
  'F2P-041',
  'F2P-042',
  'F2P-043',
  'F2P-045',
  'F2P-047',
  'F2P-048',
  'F2P-026',
  'F2P-033',
  'F2P-035',
  'F2P-039',
  'F2P-040',
  'F2P-044',
  'F2P-046',
];

function loadRuntime(repoRoot) {
  const eslintEntry = require.resolve('eslint', { paths: [repoRoot] });
  const eslintPackage = require(require.resolve('eslint/package.json', {
    paths: [repoRoot],
  }));
  const eslint = require(eslintEntry);
  let parser = null;
  try {
    parser = require(require.resolve('@typescript-eslint/parser', {
      paths: [repoRoot],
    }));
  } catch {
    parser = null;
  }

  return {
    repoRoot,
    eslint,
    ESLint: eslint.ESLint,
    Linter: eslint.Linter,
    eslintVersion: eslintPackage.version,
    eslintMajor: Number.parseInt(eslintPackage.version.split('.')[0], 10),
    parser,
    plugin: require(path.join(repoRoot, 'lib/index.js')),
  };
}

function readText(repoRoot, relativePath) {
  return fs.readFileSync(path.join(repoRoot, relativePath), 'utf8');
}

function flattenFiles(directory) {
  const entries = fs.readdirSync(directory, { withFileTypes: true });
  const files = [];

  for (const entry of entries) {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...flattenFiles(fullPath));
    } else if (entry.isFile()) {
      files.push(fullPath);
    }
  }

  return files;
}

function searchTree(repoRoot, relativeDirectories, needle) {
  const matches = [];

  for (const relativeDirectory of relativeDirectories) {
    const fullPath = path.join(repoRoot, relativeDirectory);
    if (!fs.existsSync(fullPath)) {
      continue;
    }

    const stat = fs.statSync(fullPath);
    const files = stat.isDirectory()
      ? flattenFiles(fullPath)
      : [fullPath];

    for (const file of files) {
      if (fs.readFileSync(file, 'utf8').includes(needle)) {
        matches.push(path.relative(repoRoot, file));
      }
    }
  }

  return matches;
}

function hasFlatConfigShape(config) {
  return Boolean(
    config &&
      config.plugins &&
      !Array.isArray(config.plugins) &&
      typeof config.plugins === 'object' &&
      config.plugins.jest &&
      config.languageOptions &&
      config.languageOptions.globals &&
      config.rules &&
      !Object.prototype.hasOwnProperty.call(config, 'env'),
  );
}

function allConfigsHaveFlatShape(plugin) {
  return CONFIG_NAMES.every(name => hasFlatConfigShape(plugin.configs[name]));
}

function withTypeScriptParser(context, config) {
  const fixturesRoot = path.join(
    context.repoRoot,
    'src/rules/__tests__/fixtures',
  );

  return {
    files: ['**/*.ts'],
    ...config,
    languageOptions: {
      ...(config.languageOptions || {}),
      ...(context.parser ? { parser: context.parser } : {}),
      parserOptions: {
        project: './tsconfig.json',
        tsconfigRootDir: fixturesRoot,
      },
    },
  };
}

async function lintWithConfig(context, configName, code) {
  const fixturesRoot = path.join(
    context.repoRoot,
    'src/rules/__tests__/fixtures',
  );
  const config = context.plugin.configs[configName];
  const eslint = new context.ESLint({
    overrideConfigFile: true,
    overrideConfig: [withTypeScriptParser(context, config)],
  });

  const [result] = await eslint.lintText(code, {
    filePath: path.join(fixturesRoot, 'file.ts'),
  });

  return result;
}

async function canLoadConfigs(context, configNames) {
  const failures = [];

  for (const configName of configNames) {
    try {
      const result = await lintWithConfig(
        context,
        configName,
        "test.only('x', () => { expect(1).toBe(1); });",
      );
      if (!Array.isArray(result.messages)) {
        failures.push(`${configName}: messages is not an array`);
      }
    } catch (error) {
      failures.push(`${configName}: ${String(error.message).split('\n')[0]}`);
    }
  }

  return failures;
}

function configShapeFailures(plugin, configNames) {
  return configNames.filter(name => !hasFlatConfigShape(plugin.configs[name]));
}

function verifyPreferImporting(context, scenario) {
  const ruleName = 'jest/prefer-importing-jest-globals';
  const rule = context.plugin.rules['prefer-importing-jest-globals'];
  const code = "test('x', () => {});";

  if (context.eslintMajor >= 10) {
    const languageOptions =
      scenario === 'module'
        ? { sourceType: 'module' }
        : scenario === 'script'
          ? { sourceType: 'script' }
          : { sourceType: 'script', parserOptions: { sourceType: 'module' } };
    const linter = new context.Linter({ configType: 'flat' });
    const result = linter.verifyAndFix(code, [
      {
        plugins: {
          jest: {
            rules: { 'prefer-importing-jest-globals': rule },
          },
        },
        languageOptions,
        rules: { [ruleName]: 'error' },
      },
    ]);
    return result.output;
  }

  const parserOptions =
    scenario === 'script' ? { sourceType: 'script' } : { sourceType: 'module' };
  const linter = new context.Linter({ configType: 'eslintrc' });
  linter.defineRule(ruleName, rule);
  const result = linter.verifyAndFix(
    code,
    {
      env: { es2021: true },
      parserOptions,
      rules: { [ruleName]: 'error' },
    },
    { filename: 'x.js' },
  );
  return result.output;
}

function hasImportFix(output) {
  return output.includes("import { test } from '@jest/globals';");
}

function hasRequireFix(output) {
  return output.includes("const { test } = require('@jest/globals');");
}

function preferImportingPredicate(context) {
  const moduleOutput = verifyPreferImporting(context, 'module');
  const scriptOutput = verifyPreferImporting(context, 'script');
  const parserOnlyOutput = verifyPreferImporting(context, 'parserOptions');

  return {
    ok:
      hasImportFix(moduleOutput) &&
      hasRequireFix(scriptOutput) &&
      hasRequireFix(parserOnlyOutput) &&
      !hasImportFix(parserOnlyOutput),
    detail: { moduleOutput, scriptOutput, parserOnlyOutput },
  };
}

function flatGlobalsPredicate(context) {
  const config = context.plugin.configs['flat/recommended'];
  const linter = new context.Linter({ configType: 'flat' });
  const messages = linter.verify(
    "describe('d', () => { it('i', () => { expect(1).toBe(1); }); });",
    [
      {
        ...config,
        rules: { ...config.rules, 'no-undef': 'error' },
      },
    ],
    { filename: 'sample.test.js' },
  );
  const undefinedNames = messages
    .filter(message => message.ruleId === 'no-undef')
    .map(message => message.message);
  const expectedNames = ['describe', 'it', 'expect'];
  const failure = expectedNames.find(name =>
    undefinedNames.some(message => message.includes(`'${name}'`)),
  );

  return {
    ok: !failure,
    detail: { undefinedNames },
  };
}

function readmePredicate(context) {
  const readme = readText(context.repoRoot, 'README.md');
  const hasFlatUsage =
    readme.includes('jest.configs.recommended') ||
    readme.includes("jest.configs['flat/recommended']") ||
    readme.includes('jest.configs["flat/recommended"]');
  const hasLegacyPlugins = /"plugins"\s*:\s*\[\s*"jest"\s*\]/.test(readme);
  const hasLegacyEnv = /env\s*:\s*\{\s*['"]jest\/globals['"]\s*:\s*true/.test(
    readme,
  );

  return {
    ok: hasFlatUsage && !hasLegacyPlugins && !hasLegacyEnv,
    detail: { hasFlatUsage, hasLegacyPlugins, hasLegacyEnv },
  };
}

function legacyConfigRemovedPredicate(context) {
  const matches = searchTree(
    context.repoRoot,
    ['src', 'index.d.ts', 'types.d.ts'],
    'LegacyConfig',
  );

  return { ok: matches.length === 0, detail: { matches } };
}

function eslintConfigFlatPredicate(context) {
  const source = readText(context.repoRoot, 'eslint.config.js');
  const usesConfigArray =
    source.includes('Linter.Config[]') ||
    source.includes('Linter.FlatConfig[]');
  const usesRemovedTypes = source.includes('LegacyConfig');

  return {
    ok: usesConfigArray && !usesRemovedTypes,
    detail: { usesConfigArray, usesRemovedTypes },
  };
}

function configRuleTablesPredicate(context) {
  const { plugin } = context;
  const allRules = Object.keys(plugin.configs.all.rules);
  const flatAllRules = Object.keys(plugin.configs['flat/all'].rules);
  const recommendedRules = Object.keys(plugin.configs.recommended.rules);
  const styleRules = Object.keys(plugin.configs.style.rules);
  const deprecatedRules = allRules.filter(ruleName => {
    const shortName = ruleName.replace(/^jest\//, '');
    return Boolean(plugin.rules[shortName]?.meta?.deprecated);
  });

  return {
    ok:
      allRules.length === 71 &&
      flatAllRules.length === 71 &&
      recommendedRules.length === 19 &&
      styleRules.length === 3 &&
      deprecatedRules.length === 0,
    detail: {
      allRules: allRules.length,
      flatAllRules: flatAllRules.length,
      recommendedRules: recommendedRules.length,
      styleRules: styleRules.length,
      deprecatedRules,
    },
  };
}

function pluginExportPredicate(context) {
  const { plugin } = context;
  const globals = plugin.environments?.globals?.globals || {};
  const versionIsSemver =
    typeof plugin.meta?.version === 'string' &&
    /^\d+\.\d+\.\d+/.test(plugin.meta.version);

  return {
    ok:
      plugin.meta?.name === 'eslint-plugin-jest' &&
      versionIsSemver &&
      Object.keys(plugin.configs).length === 6 &&
      ['describe', 'it', 'expect'].every(name =>
        Object.prototype.hasOwnProperty.call(globals, name),
      ) &&
      Object.keys(plugin.rules).length === 71,
    detail: {
      name: plugin.meta?.name,
      version: plugin.meta?.version,
      configs: Object.keys(plugin.configs),
      globals: ['describe', 'it', 'expect'].filter(name =>
        Object.prototype.hasOwnProperty.call(globals, name),
      ),
      rules: Object.keys(plugin.rules).length,
    },
  };
}

function testAssertionsIntactPredicate(context) {
  const source = readText(context.repoRoot, 'src/__tests__/rules.test.ts');
  const mentionsSixConfigs = CONFIG_NAMES.every(name =>
    source.includes(`'${name}'`),
  );
  const checksShape =
    source.includes("toHaveProperty('plugins.jest')") &&
    source.includes("toHaveProperty('languageOptions.globals')") &&
    source.includes("not.toHaveProperty('env')");
  const hasSkip = /(it|describe|test)\.skip\s*\(/.test(source);

  return {
    ok: mentionsSixConfigs && checksShape && !hasSkip,
    detail: { mentionsSixConfigs, checksShape, hasSkip },
  };
}

async function evaluate(repoRoot) {
  const context = loadRuntime(repoRoot);
  const results = {};

  const set = (id, result) => {
    results[id] = result;
  };

  const shapeCheck = names => {
    const failures = configShapeFailures(context.plugin, names);
    return {
      ok: failures.length === 0,
      detail: { failures },
    };
  };

  const loadCheck = async (names, extraCheck) => {
    const failures = await canLoadConfigs(context, names);
    const extra = extraCheck ? extraCheck() : { ok: true, detail: {} };
    return {
      ok: failures.length === 0 && extra.ok,
      detail: { failures, extra: extra.detail },
    };
  };

  const combined = (...checks) => ({
    ok: checks.every(check => check.ok),
    detail: { checks: checks.map(check => check.detail) },
  });

  const baseShape = () => shapeCheck(['all', 'recommended', 'style']);
  const allShape = () => shapeCheck(CONFIG_NAMES);

  set(
    'F2P-025',
    await loadCheck(['all', 'recommended', 'style'], baseShape),
  );

  set(
    'F2P-026',
    combined(
      allShape(),
      await loadCheck(['all', 'flat/all'], () => configRuleTablesPredicate(context)),
    ),
  );

  set('F2P-027', readmePredicate(context));

  set(
    'F2P-028',
    combined(
      allShape(),
      await loadCheck(CONFIG_NAMES, () =>
        configRuleTablesPredicate(context),
      ),
    ),
  );

  set(
    'F2P-029',
    combined(
      allShape(),
      await loadCheck(['all', 'flat/all'], () => configRuleTablesPredicate(context)),
    ),
  );

  set(
    'F2P-030',
    combined(baseShape(), preferImportingPredicate(context)),
  );

  set(
    'F2P-031',
    await loadCheck(['all', 'recommended', 'style'], baseShape),
  );

  set('F2P-032', eslintConfigFlatPredicate(context));

  set(
    'F2P-033',
    combined(
      allShape(),
      await loadCheck(CONFIG_NAMES, () =>
        configRuleTablesPredicate(context),
      ),
    ),
  );

  set(
    'F2P-034',
    combined(
      baseShape(),
      testAssertionsIntactPredicate(context),
      await loadCheck(['all', 'recommended', 'style']),
    ),
  );

  set(
    'F2P-035',
    await loadCheck(['all', 'recommended', 'style'], baseShape),
  );

  set('F2P-036', legacyConfigRemovedPredicate(context));

  set(
    'F2P-037',
    combined(
      allShape(),
      await loadCheck(CONFIG_NAMES),
      testAssertionsIntactPredicate(context),
    ),
  );

  set(
    'F2P-038',
    combined(
      allShape(),
      await loadCheck(CONFIG_NAMES),
      testAssertionsIntactPredicate(context),
    ),
  );

  set(
    'F2P-039',
    combined(baseShape(), flatGlobalsPredicate(context), await loadCheck(['all', 'recommended', 'style'])),
  );

  set(
    'F2P-040',
    combined(
      allShape(),
      await loadCheck(['all', 'recommended', 'style']),
      configRuleTablesPredicate(context),
    ),
  );

  set(
    'F2P-041',
    combined(baseShape(), preferImportingPredicate(context)),
  );

  set(
    'F2P-042',
    combined(baseShape(), preferImportingPredicate(context)),
  );

  set(
    'F2P-043',
    combined(
      allShape(),
      await loadCheck(['all', 'flat/all'], () => configRuleTablesPredicate(context)),
    ),
  );

  set(
    'F2P-044',
    combined(
      allShape(),
      await loadCheck(['all', 'flat/all'], () =>
        configRuleTablesPredicate(context),
      ),
    ),
  );

  set(
    'F2P-045',
    combined(baseShape(), preferImportingPredicate(context)),
  );

  set(
    'F2P-046',
    await loadCheck(['all', 'recommended', 'style'], baseShape),
  );

  set(
    'F2P-047',
    combined(allShape(), pluginExportPredicate(context)),
  );

  set(
    'F2P-048',
    combined(baseShape(), flatGlobalsPredicate(context), await loadCheck(['all', 'recommended', 'style'])),
  );

  return {
    eslint_version: context.eslintVersion,
    observations: OBSERVATIONS.map(id => ({
      observation_id: id,
      status: results[id]?.ok ? 'PASS' : 'FAIL',
      detail: results[id]?.detail ?? null,
    })),
  };
}

module.exports = { evaluate, OBSERVATIONS };
