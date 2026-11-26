'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');
const util = require('util');

const DEPS =
  process.env.MIGRATION_DEPENDENCIES_NODE_MODULES ||
  '/opt/migration/dependencies/node_modules';

if (!process.env.NODE_PATH || !process.env.NODE_PATH.split(path.delimiter).includes(DEPS)) {
  process.env.NODE_PATH = process.env.NODE_PATH
    ? `${DEPS}${path.delimiter}${process.env.NODE_PATH}`
    : DEPS;
}
require('module').Module._initPaths();

process.env.TS_NODE_TRANSPILE_ONLY = 'true';
process.env.TS_NODE_COMPILER_OPTIONS = JSON.stringify({
  module: 'commonjs',
  moduleResolution: 'node',
  resolveJsonModule: true,
  esModuleInterop: true,
});

require(path.join(DEPS, 'ts-node/register/transpile-only'));

const { ESLint, Linter } = require(path.join(DEPS, 'eslint'));
const parser = require('@typescript-eslint/parser');
const { isDeepStrictEqual } = util;

const ASSET_ROOT = path.resolve(__dirname, '../../..');
const EXPECTED = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'expected.json'), 'utf8'),
);
const CONFIG_NAMES = EXPECTED.configNames;
const RULE_NAMES = EXPECTED.ruleNames;
const CONFIG_RULE_NAMES = EXPECTED.configRuleNames;
const FIXTURE_CONSUMER = path.join(
  ASSET_ROOT,
  'fixtures/batches/BATCH-F2P-0003/legacy-config-consumer.ts',
);
const TSC = path.join(DEPS, 'typescript/bin/tsc');

function read(repoRoot, relativePath) {
  return fs.readFileSync(path.join(repoRoot, relativePath), 'utf8');
}

function loadPlugin(repoRoot) {
  const loaded = require(path.join(repoRoot, 'src/index.ts'));
  return loaded.default || loaded;
}

function hasOwn(object, key) {
  return Object.prototype.hasOwnProperty.call(object, key);
}

function tempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `migration-${prefix}-`));
}

function runTsc(tsconfigPath) {
  const result = cp.spawnSync(process.execPath, [TSC, '-p', tsconfigPath], {
    encoding: 'utf8',
    env: { ...process.env, NODE_PATH: DEPS },
  });
  return {
    status: result.status,
    output: `${result.stdout || ''}${result.stderr || ''}`.trim(),
  };
}

function writeJson(filePath, value) {
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

function declarationTsconfig(repoRoot) {
  const dir = tempDir('declarations');
  const configPath = path.join(dir, 'tsconfig.json');
  writeJson(configPath, {
    compilerOptions: {
      noEmit: true,
      strict: true,
      skipLibCheck: false,
      target: 'ES2022',
      module: 'commonjs',
      moduleResolution: 'node',
      esModuleInterop: true,
      resolveJsonModule: true,
      baseUrl: '/',
      paths: { '*': [`${DEPS}/*`] },
      typeRoots: [`${DEPS}/@types`],
      types: [],
    },
    files: [
      path.join(repoRoot, 'index.d.ts'),
      path.join(repoRoot, 'types.d.ts'),
    ],
  });
  return configPath;
}

function consumerTsconfig(repoRoot) {
  const dir = tempDir('consumer');
  const configPath = path.join(dir, 'tsconfig.json');
  writeJson(configPath, {
    compilerOptions: {
      noEmit: true,
      strict: true,
      skipLibCheck: false,
      target: 'ES2022',
      module: 'commonjs',
      moduleResolution: 'node',
      esModuleInterop: true,
      baseUrl: '/',
      paths: {
        '*': [`${DEPS}/*`],
        '@plugin': [path.join(repoRoot, 'index.d.ts')],
      },
      typeRoots: [`${DEPS}/@types`],
      types: [],
    },
    files: [FIXTURE_CONSUMER],
  });
  return configPath;
}

async function calculate(plugin, name) {
  const eslint = new ESLint({
    overrideConfigFile: true,
    overrideConfig: [plugin.configs[name]],
  });
  return eslint.calculateConfigForFile('x.test.js');
}

async function lint(plugin, names, code, extraConfig, filePath = 'x.test.js') {
  const overrideConfig = names.map(name => plugin.configs[name]);
  if (extraConfig) {
    overrideConfig.push(extraConfig);
  }
  const eslint = new ESLint({ overrideConfigFile: true, overrideConfig });
  return eslint.lintText(code, { filePath });
}

function linterFix(plugin, code, languageOptions) {
  const linter = new Linter({ configType: 'flat' });
  const config = {
    plugins: { jest: plugin },
    languageOptions: { parser, ...languageOptions },
    rules: { 'jest/prefer-importing-jest-globals': 'error' },
  };
  return linter.verifyAndFix(code, config, { filename: 'x.test.js' });
}

function linterMessages(plugin, code, languageOptions) {
  const linter = new Linter({ configType: 'flat' });
  const config = {
    plugins: { jest: plugin },
    languageOptions: { parser, ...languageOptions },
    rules: { 'jest/prefer-importing-jest-globals': 'error' },
  };
  return linter.verify(code, config, { filename: 'x.test.js' });
}

function normalizeMessages(messages) {
  return messages.map(message => ({
    ruleId: message.ruleId,
    severity: message.severity,
    message: message.message,
    line: message.line,
    column: message.column,
    endLine: message.endLine,
    endColumn: message.endColumn,
    messageId: message.messageId,
  }));
}

const checks = {
  async declarations({ plugin, repoRoot }) {
    const indexText = read(repoRoot, 'index.d.ts');
    const typesText = read(repoRoot, 'types.d.ts');
    const configAliases = new Set(
      [
        ...indexText.matchAll(
          /type\s+([A-Za-z_$][\w$]*)\s*=\s*[^;]*Linter\.(?:Config|FlatConfig)[^;]*;/gs,
        ),
      ].map(match => match[1]),
    );
    const indexConfigEntries = CONFIG_NAMES.map(name => {
      const match = new RegExp(
        `['"]?${name.replace('/', '\\/')}['"]?\\s*:\\s*([A-Za-z_$][\\w$]*(?:\\.(?:Config|FlatConfig))?)\\b`,
      ).exec(indexText);
      return (
        match &&
        (match[1] === 'Linter.Config' ||
          match[1] === 'Linter.FlatConfig' ||
          configAliases.has(match[1]))
      );
    });
    const indexUsesConfig = indexConfigEntries.every(Boolean);
    const indexHasLegacy = /LegacyConfig/.test(indexText);
    const typesHasLegacy = /LegacyConfig/.test(typesText);
    const typesConfigCount =
      (typesText.match(/(?:ESLint\.)?Linter\.(?:Config|FlatConfig);/g) || [])
        .length;
    const declarationCompile = runTsc(declarationTsconfig(repoRoot));
    const consumerCompile = runTsc(consumerTsconfig(repoRoot));
    const evidence = [
      `index.d.ts valid ESLint 10 config entries=${indexConfigEntries.filter(Boolean).length}, legacy tokens=${indexHasLegacy}`,
      `types.d.ts Linter.Config entries=${typesConfigCount}, legacy tokens=${typesHasLegacy}`,
      `declaration tsc exit=${declarationCompile.status}`,
      `legacy consumer tsc exit=${consumerCompile.status}`,
    ];
    return {
      pass:
        indexUsesConfig &&
        !indexHasLegacy &&
        !typesHasLegacy &&
        typesConfigCount >= 14 &&
        declarationCompile.status === 0 &&
        consumerCompile.status !== 0,
      evidence,
    };
  },

  async noLegacy({ repoRoot }) {
    const indexText = read(repoRoot, 'index.d.ts');
    const typesText = read(repoRoot, 'types.d.ts');
    const declarationCompile = runTsc(declarationTsconfig(repoRoot));
    const consumerCompile = runTsc(consumerTsconfig(repoRoot));
    const hasLegacy = /Linter\.LegacyConfig/.test(indexText + typesText);
    return {
      pass:
        !hasLegacy &&
        declarationCompile.status === 0 &&
        consumerCompile.status !== 0,
      evidence: [
        `LegacyConfig present=${hasLegacy}`,
        `declaration tsc exit=${declarationCompile.status}`,
        `legacy consumer tsc exit=${consumerCompile.status}`,
      ],
    };
  },

  async flatShape({ plugin }) {
    const evidence = [];
    let pass = true;
    const keys = Object.keys(plugin.configs);
    if (!isDeepStrictEqual(keys, CONFIG_NAMES)) {
      pass = false;
      evidence.push(`config keys=${keys.join(',')}`);
    }
    for (const name of CONFIG_NAMES) {
      const config = plugin.configs[name];
      const hasPlugins =
        hasOwn(config, 'plugins') &&
        !Array.isArray(config.plugins) &&
        Boolean(config.plugins.jest);
      const hasGlobals =
        hasOwn(config, 'languageOptions') &&
        Boolean(config.languageOptions.globals);
      const hasRules = hasOwn(config, 'rules');
      const hasEnv = hasOwn(config, 'env');
      try {
        await calculate(plugin, name);
        evidence.push(`${name}: plugins=${hasPlugins}, globals=${hasGlobals}, rules=${hasRules}, env=${hasEnv}, loads=true`);
        if (!hasPlugins || !hasGlobals || !hasRules || hasEnv) {
          pass = false;
        }
      } catch (error) {
        pass = false;
        evidence.push(`${name}: load error=${error.message}`);
      }
    }
    return { pass, evidence };
  },

  async legacyShape({ plugin }) {
    const evidence = [];
    let pass = true;
    for (const name of ['all', 'recommended', 'style']) {
      const config = plugin.configs[name];
      const hasEnv = hasOwn(config, 'env');
      const pluginsObject =
        hasOwn(config, 'plugins') &&
        !Array.isArray(config.plugins) &&
        config.plugins.jest === plugin;
      try {
        await calculate(plugin, name);
        evidence.push(`${name}: env=${hasEnv}, pluginsObject=${pluginsObject}, loads=true`);
        if (hasEnv || !pluginsObject) {
          pass = false;
        }
      } catch (error) {
        pass = false;
        evidence.push(`${name}: load error=${error.message}`);
      }
    }
    return { pass, evidence };
  },

  async loadAll({ plugin }) {
    const evidence = [];
    let pass = true;
    for (const name of CONFIG_NAMES) {
      try {
        await calculate(plugin, name);
        evidence.push(`${name}: loads=true`);
      } catch (error) {
        pass = false;
        evidence.push(`${name}: load error=${error.message}`);
      }
    }
    try {
      const [result] = await lint(
        plugin,
        ['recommended'],
        "test.only('focused', () => { expect(true).toBe(true); });",
      );
      evidence.push(`recommended lint messages array=${Array.isArray(result.messages)}`);
      if (!Array.isArray(result.messages)) {
        pass = false;
      }
    } catch (error) {
      pass = false;
      evidence.push(`recommended lint error=${error.message}`);
    }
    return { pass, evidence };
  },

  async globals({ plugin }) {
    const evidence = [];
    let pass = true;
    for (const name of CONFIG_NAMES) {
      try {
        const config = await calculate(plugin, name);
        const globals = config.languageOptions && config.languageOptions.globals;
        const ok =
          globals &&
          ['describe', 'it', 'expect'].every(key => hasOwn(globals, key));
        evidence.push(`${name}: jest globals=${Boolean(ok)}`);
        if (!ok) {
          pass = false;
        }
      } catch (error) {
        pass = false;
        evidence.push(`${name}: load error=${error.message}`);
      }
    }
    const code =
      "describe('d', () => { it('i', () => { expect(1).toBe(1); }); });";
    try {
      const [withGlobals] = await lint(plugin, ['recommended'], code, {
        rules: { 'no-undef': 'error' },
      });
      const undefinedNames = withGlobals.messages.filter(message =>
        ['describe', 'it', 'expect'].some(name =>
          message.message.includes(`'${name}' is not defined`),
        ),
      );
      evidence.push(`flat config no-undef diagnostics=${undefinedNames.length}`);
      if (undefinedNames.length !== 0) {
        pass = false;
      }
    } catch (error) {
      pass = false;
      evidence.push(`flat config lint error=${error.message}`);
    }
    try {
      const [withoutGlobals] = await lint(plugin, [], code, {
        rules: { 'no-undef': 'error' },
      });
      const undefinedNames = withoutGlobals.messages.filter(message =>
        ['describe', 'it', 'expect'].some(name =>
          message.message.includes(`'${name}' is not defined`),
        ),
      );
      evidence.push(`without globals no-undef diagnostics=${undefinedNames.length}`);
      if (undefinedNames.length !== 3) {
        pass = false;
      }
    } catch (error) {
      pass = false;
      evidence.push(`baseline lint error=${error.message}`);
    }
    return { pass, evidence };
  },

  async recommendedDiagnostics({ plugin }) {
    const evidence = [];
    let pass = true;
    if (
      !isDeepStrictEqual(
        plugin.configs.recommended.rules,
        plugin.configs['flat/recommended'].rules,
      )
    ) {
      pass = false;
      evidence.push('recommended and flat/recommended rule tables differ');
    } else {
      evidence.push('recommended and flat/recommended rule tables equal');
    }
    const code =
      "test.only('focused', () => { expect(true).toBe(true); });";
    try {
      const [recommended] = await lint(plugin, ['recommended'], code);
      const [flatRecommended] = await lint(plugin, ['flat/recommended'], code);
      const left = normalizeMessages(recommended.messages);
      const right = normalizeMessages(flatRecommended.messages);
      if (!isDeepStrictEqual(left, right)) {
        pass = false;
        evidence.push('diagnostic arrays differ');
      } else {
        evidence.push(`diagnostic arrays equal, count=${left.length}`);
      }
    } catch (error) {
      pass = false;
      evidence.push(`diagnostic lint error=${error.message}`);
    }
    return { pass, evidence };
  },

  async pluginSurface({ plugin }) {
    const evidence = [];
    const globals = plugin.environments && plugin.environments.globals.globals;
    const meta = plugin.meta || {};
    const versionOk =
      typeof meta.version === 'string' &&
      /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(meta.version);
    const keys = Object.keys(plugin.configs);
    const ruleKeys = Object.keys(plugin.rules);
    const pass =
      meta.name === 'eslint-plugin-jest' &&
      versionOk &&
      isDeepStrictEqual(keys, CONFIG_NAMES) &&
      Boolean(globals) &&
      ['describe', 'it', 'expect'].every(key => hasOwn(globals, key)) &&
      ruleKeys.length === 71;
    evidence.push(`meta.name=${meta.name}`);
    evidence.push(`meta.version=${meta.version}, valid=${versionOk}`);
    evidence.push(`config keys=${keys.join(',')}`);
    evidence.push(`rule count=${ruleKeys.length}`);
    evidence.push(
      `environment globals=${['describe', 'it', 'expect']
        .map(key => `${key}:${Boolean(globals && hasOwn(globals, key))}`)
        .join(',')}`,
    );
    return { pass, evidence };
  },

  async configKeys({ plugin }) {
    const keys = Object.keys(plugin.configs);
    return {
      pass: isDeepStrictEqual(keys, CONFIG_NAMES),
      evidence: [`config keys=${keys.join(',')}`],
    };
  },

  async ruleInventory({ plugin }) {
    const keys = Object.keys(plugin.rules).sort();
    return {
      pass:
        keys.length === EXPECTED.ruleCount &&
        isDeepStrictEqual(keys, RULE_NAMES),
      evidence: [
        `rule count=${keys.length}, expected=${EXPECTED.ruleCount}`,
        `rule names equal=${isDeepStrictEqual(keys, RULE_NAMES)}`,
      ],
    };
  },

  async allRuleSet({ plugin }) {
    const allKeys = Object.keys(plugin.configs.all.rules).sort();
    const flatAllKeys = Object.keys(plugin.configs['flat/all'].rules).sort();
    const pass =
      isDeepStrictEqual(allKeys, CONFIG_RULE_NAMES) &&
      isDeepStrictEqual(flatAllKeys, CONFIG_RULE_NAMES);
    return {
      pass,
      evidence: [
        `all rule count=${allKeys.length}, flat/all rule count=${flatAllKeys.length}`,
        `expected non-deprecated count=${CONFIG_RULE_NAMES.length}`,
        `all equals expected=${isDeepStrictEqual(allKeys, CONFIG_RULE_NAMES)}`,
        `flat/all equals expected=${isDeepStrictEqual(flatAllKeys, CONFIG_RULE_NAMES)}`,
      ],
    };
  },

  async ci({ repoRoot }) {
    const workflow = read(repoRoot, '.github/workflows/nodejs.yml');
    const matrix = /eslint-version:\s*\[([^\]]*)\]/m.exec(workflow);
    const matrixValues = matrix
      ? matrix[1].split(',').map(value => value.trim())
      : [];
    const pinsTarget = matrixValues.includes('10.5.0');
    const installsMatrix = /eslint@\$\{\{\s*matrix\.eslint-version\s*\}\}/m.test(
      workflow,
    );
    return {
      pass: pinsTarget && installsMatrix,
      evidence: [
        `eslint-version matrix=${matrixValues.join(',')}`,
        `matrix pins 10.5.0=${pinsTarget}`,
        `workflow installs eslint@matrix=${installsMatrix}`,
      ],
    };
  },

  async preferImporting({ plugin, repoRoot, includeMerge = false }) {
    const ruleText = read(
      repoRoot,
      'src/rules/prefer-importing-jest-globals.ts',
    );
    const noParserOptionsFallback = !/context\.parserOptions/.test(ruleText);
    const evidence = [`rule uses context.parserOptions=${!noParserOptionsFallback}`];
    let pass = noParserOptionsFallback;

    const moduleFix = linterFix(plugin, "test('x', () => {});", {
      sourceType: 'module',
    });
    const scriptFix = linterFix(plugin, "test('x', () => {});", {
      sourceType: 'script',
    });
    const parserOptionsFix = linterFix(plugin, "test('x', () => {});", {
      sourceType: 'script',
      parserOptions: { sourceType: 'module' },
    });
    const messages = linterMessages(plugin, "test('x', () => {});", {
      sourceType: 'module',
    });

    const moduleOutput = moduleFix.output;
    const scriptOutput = scriptFix.output;
    const parserOptionsOutput = parserOptionsFix.output;
    const message = messages.find(
      item => item.messageId === 'preferImportingJestGlobal',
    );

    if (!moduleOutput.includes("import { test } from '@jest/globals';")) {
      pass = false;
    }
    evidence.push(`module fix=${JSON.stringify(moduleOutput.split('\n')[0])}`);

    if (!scriptOutput.includes("const { test } = require('@jest/globals');")) {
      pass = false;
    }
    evidence.push(`script fix=${JSON.stringify(scriptOutput.split('\n')[0])}`);

    if (!parserOptionsOutput.includes("const { test } = require('@jest/globals');")) {
      pass = false;
    }
    evidence.push(
      `parserOptions-only fix=${JSON.stringify(parserOptionsOutput.split('\n')[0])}`,
    );

    if (!message || !String(message.message).includes('test')) {
      pass = false;
    }
    evidence.push(
      `messageId=${message && message.messageId}, message=${message && message.message}`,
    );

    if (includeMerge) {
      const importMerge = linterFix(
        plugin,
        "import { expect } from '@jest/globals';\ntest('x', () => { expect(1).toBe(1); });",
        { sourceType: 'module' },
      );
      const requireMerge = linterFix(
        plugin,
        "const { expect } = require('@jest/globals');\ntest('x', () => { expect(1).toBe(1); });",
        { sourceType: 'script' },
      );
      if (!importMerge.output.includes("import { expect, test } from '@jest/globals';")) {
        pass = false;
      }
      if (!requireMerge.output.includes("const { expect, test } = require('@jest/globals');")) {
        pass = false;
      }
      evidence.push(
        `import merge=${JSON.stringify(importMerge.output.split('\n')[0])}`,
      );
      evidence.push(
        `require merge=${JSON.stringify(requireMerge.output.split('\n')[0])}`,
      );
    }

    return { pass, evidence };
  },

  async mergeImportRequire({ plugin, repoRoot }) {
    const base = await checks.preferImporting({
      plugin,
      repoRoot,
      includeMerge: true,
    });
    return base;
  },
};

const PLAN = {
  'F2P-056': [['declarations']],
  'F2P-049': [['globals']],
  'F2P-050': [['preferImporting']],
  'F2P-055': [['preferImporting']],
  'F2P-063': [['preferImporting']],
  'F2P-064': [['ci']],
  'F2P-065': [['flatShape'], ['configTables', ['all', 'recommended', 'style']], ['loadAll']],
  'F2P-066': [['preferImporting']],
  'F2P-068': [['flatShape'], ['configTables', ['recommended', 'style']]],
  'F2P-069': [['noLegacy'], ['declarations']],
  'F2P-070': [['mergeImportRequire']],
  'F2P-071': [['recommendedDiagnostics']],
  'F2P-072': [['preferImporting']],
  'F2P-051': [['pluginSurface'], ['loadAll']],
  'F2P-052': [['globals']],
  'F2P-053': [['flatShape'], ['loadAll']],
  'F2P-054': [['configKeys'], ['loadAll']],
  'F2P-057': [['configTables', ['recommended', 'style']], ['loadAll']],
  'F2P-058': [['flatShape']],
  'F2P-059': [['ruleInventory'], ['loadAll']],
  'F2P-060': [['configTables', ['recommended', 'style']], ['flatShape']],
  'F2P-061': [['allRuleSet'], ['loadAll']],
  'F2P-062': [['flatShape']],
  'F2P-067': [['legacyShape']],
};

checks.configTables = async ({ plugin }, names) => {
  const evidence = [];
  let pass = true;
  for (const name of names || CONFIG_NAMES) {
    const actual = plugin.configs[name] && plugin.configs[name].rules;
    const expected = EXPECTED.configRules[name];
    if (!isDeepStrictEqual(actual, expected)) {
      pass = false;
      evidence.push(`${name}: rule table mismatch`);
    } else {
      evidence.push(
        `${name}: rule table equal (${Object.keys(actual || {}).length} rules)`,
      );
    }
  }
  return { pass, evidence };
};

async function runProbe(repoRoot) {
  const plugin = loadPlugin(repoRoot);
  const context = { plugin, repoRoot };
  const results = [];
  for (const observationId of Object.keys(PLAN)) {
    const parts = [];
    let pass = true;
    for (const step of PLAN[observationId]) {
      const [checkName, argument] = step;
      try {
        const outcome = await checks[checkName](context, argument);
        parts.push(...outcome.evidence.map(item => `${checkName}: ${item}`));
        if (!outcome.pass) {
          pass = false;
        }
      } catch (error) {
        pass = false;
        parts.push(`${checkName}: error=${error.stack || error.message}`);
      }
    }
    results.push({
      observation_id: observationId,
      status: pass ? 'PASS' : 'FAIL',
      evidence: parts,
    });
  }
  return results;
}

function reportPathFromArgv(argv) {
  return argv[2] || process.env.PROBE_REPORT_PATH || null;
}

async function runCli(repoRoot) {
  const results = await runProbe(repoRoot);
  const report = {
    batch_id: 'BATCH-F2P-0003',
    repo_root: repoRoot,
    results,
  };
  const reportPath = reportPathFromArgv(process.argv);
  if (reportPath) {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  }
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  return results.every(result => result.status === 'PASS') ? 0 : 1;
}

module.exports = {
  runProbe,
  runCli,
};
