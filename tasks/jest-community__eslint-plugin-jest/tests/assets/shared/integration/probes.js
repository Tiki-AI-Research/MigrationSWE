'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const {
  exists,
  fixText,
  hashFiles,
  lintText,
  loadEslint,
  normalizeMessages,
  readJSON,
  readText,
  requireFrom,
  ruleMetadata,
  sha256,
  spawnSync,
  targetTechnology,
  targetTechnologyOk,
  typeAwareLint,
  walk,
} = require('./probe-lib');

const CONFIG_NAMES = [
  'all',
  'recommended',
  'style',
  'flat/all',
  'flat/recommended',
  'flat/style',
];

const SKIP_RE = /\b(?:it|describe|test)\.skip\s*\(/u;

// The rule-test file and its snapshot legitimately embed `it.skip(...)` /
// `describe.skip(...)` / `test.skip(...)` as *fixture inputs* to the rules
// under test (e.g. a case that trips `jest/no-disabled-tests`).  Matching the
// raw text therefore reports a skipped test the candidate never wrote, which
// false-fails `behavioralAssertions` / `rulesTestFlat`.  Blank out comment,
// string, template-literal and regex bodies first so `hasSkip` only observes
// executable code: a real skipped test in code is still detected, while a skip
// that only appears inside a quoted fixture (or a comment) is ignored.
const codeWithoutLiterals = text => {
  let out = '';
  const n = text.length;
  let i = 0;
  let mode = 'code';
  let prev = '';
  const REGEX_OK = new Set([
    '', '(', ',', '=', ':', '[', '!', '&', '|', '?', '{', '}', ';',
    '+', '-', '*', '%', '~', '^', '<', '>', '/',
  ]);
  while (i < n) {
    const ch = text[i];
    const next = text[i + 1];
    if (mode === 'code') {
      if (ch === '/' && next === '/') {
        mode = 'line';
        out += '  ';
        i += 2;
        continue;
      }
      if (ch === '/' && next === '*') {
        mode = 'block';
        out += '  ';
        i += 2;
        continue;
      }
      if (ch === '/' && REGEX_OK.has(prev)) {
        mode = 'regex';
        out += ' ';
        i += 1;
        prev = '';
        continue;
      }
      if (ch === "'") {
        mode = 'single';
        out += ' ';
        i += 1;
        continue;
      }
      if (ch === '"') {
        mode = 'double';
        out += ' ';
        i += 1;
        continue;
      }
      if (ch === '`') {
        mode = 'template';
        out += ' ';
        i += 1;
        continue;
      }
      out += ch;
      i += 1;
      if (!/\s/u.test(ch)) {
        prev = ch;
      }
      continue;
    }
    if (mode === 'line') {
      if (ch === '\n') {
        mode = 'code';
        out += '\n';
        i += 1;
        prev = '';
        continue;
      }
      out += ' ';
      i += 1;
      continue;
    }
    if (mode === 'block') {
      if (ch === '*' && next === '/') {
        mode = 'code';
        out += '  ';
        i += 2;
        prev = '';
        continue;
      }
      out += ch === '\n' ? '\n' : ' ';
      i += 1;
      continue;
    }
    if (mode === 'regex') {
      if (ch === '\\') {
        out += '  ';
        i += 2;
        continue;
      }
      if (ch === '[') {
        out += ' ';
        i += 1;
        while (i < n && text[i] !== ']') {
          out += text[i] === '\n' ? '\n' : ' ';
          i += text[i] === '\\' ? 2 : 1;
        }
        if (i < n) {
          out += ' ';
          i += 1;
        }
        continue;
      }
      if (ch === '/') {
        out += ' ';
        i += 1;
        while (i < n && /[a-z]/iu.test(text[i])) {
          out += ' ';
          i += 1;
        }
        mode = 'code';
        prev = '';
        continue;
      }
      out += ch === '\n' ? '\n' : ' ';
      i += 1;
      continue;
    }
    const quote = mode === 'single' ? "'" : mode === 'double' ? '"' : '`';
    if (ch === '\\') {
      out += '  ';
      i += 2;
      continue;
    }
    if (ch === quote) {
      mode = 'code';
      out += ' ';
      i += 1;
      prev = '';
      continue;
    }
    out += ch === '\n' ? '\n' : ' ';
    i += 1;
    continue;
  }
  return out;
};

// The `rules` table must keep its string index signature as part of the public
// contract, but an equivalent declaration may wrap or intersect that literal,
// e.g. `rules: NonNullable<ESLint.Plugin['rules']> & { [key: string]: ... }`.
// Requiring `{` to immediately follow `rules:` (the former
// `/rules\s*:\s*\{[\s\S]*\[key:\s*string\]/u`) rejects such an equivalent type
// and reports a false regression.  Instead, scan the type annotation that
// belongs to the `rules` member and look for a string index signature there,
// stopping at the member boundary so an index signature owned by a sibling
// property (e.g. `environments.globals`) is never attributed to `rules`.
const STRING_INDEX_SIGNATURE = /\[\s*[A-Za-z_$][\w$]*\s*:\s*string\s*\]\s*:/u;

const rulesMemberType = (text, from) => {
  let depth = 0;
  let quote = '';
  let i = from;
  while (i < text.length) {
    const char = text[i];
    if (quote) {
      if (char === '\\') {
        i += 2;
      } else {
        if (char === quote) {
          quote = '';
        }
        i += 1;
      }
      continue;
    }
    if (char === '"' || char === "'" || char === '`') {
      quote = char;
      i += 1;
      continue;
    }
    if (char === '/' && text[i + 1] === '/') {
      const end = text.indexOf('\n', i);
      i = end === -1 ? text.length : end;
      continue;
    }
    if (char === '/' && text[i + 1] === '*') {
      const end = text.indexOf('*/', i + 2);
      i = end === -1 ? text.length : end + 2;
      continue;
    }
    if (char === '{' || char === '[' || char === '(') {
      depth += 1;
      i += 1;
      continue;
    }
    if (char === '}' || char === ']' || char === ')') {
      if (depth === 0) {
        break;
      }
      depth -= 1;
      i += 1;
      continue;
    }
    if (depth === 0 && (char === ';' || char === ',')) {
      break;
    }
    if (depth === 0 && char === '\n') {
      // TypeScript separates type members with a newline (ASI).  Only treat it
      // as the end of the `rules` member when the following token cannot
      // continue the current type expression (e.g. `&`/`|`/`<`/`.`).
      const next = text.slice(i + 1).match(/\S/u);
      if (!next || !/[&|<>.<({]/u.test(next[0])) {
        break;
      }
    }
    i += 1;
  }
  return text.slice(from, i);
};

const hasRulesIndexSignature = text => {
  const marker = /\brules\s*:/gu;
  let match;
  while ((match = marker.exec(text)) !== null) {
    if (STRING_INDEX_SIGNATURE.test(rulesMemberType(text, marker.lastIndex))) {
      return true;
    }
  }
  return false;
};

const EXPECTED_RECOMMENDED = {
  'jest/expect-expect': 'warn',
  'jest/no-alias-methods': 'error',
  'jest/no-commented-out-tests': 'warn',
  'jest/no-conditional-expect': 'error',
  'jest/no-deprecated-functions': 'error',
  'jest/no-disabled-tests': 'warn',
  'jest/no-done-callback': 'error',
  'jest/no-export': 'error',
  'jest/no-focused-tests': 'error',
  'jest/no-identical-title': 'error',
  'jest/no-interpolation-in-snapshots': 'error',
  'jest/no-jasmine-globals': 'error',
  'jest/no-mocks-import': 'error',
  'jest/no-standalone-expect': 'error',
  'jest/no-test-prefixes': 'error',
  'jest/valid-describe-callback': 'error',
  'jest/valid-expect': 'error',
  'jest/valid-expect-in-promise': 'error',
  'jest/valid-title': 'error',
};

const EXPECTED_STYLE = {
  'jest/prefer-to-be': 'error',
  'jest/prefer-to-contain': 'error',
  'jest/prefer-to-have-length': 'error',
};

const EXPECTED_TYPE_AWARE = [
  'no-error-equal',
  'no-unnecessary-assertion',
  'unbound-method',
  'valid-expect-with-promise',
];

const sortedKeys = value => Object.keys(value).sort();
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const result = (ok, value) => ({ ok: Boolean(ok), value });
const ruleName = name => `jest/${name}`;
const flattened = config => Object.fromEntries(Object.entries(config.rules).sort());

const nonDeprecatedRuleNames = plugin =>
  Object.keys(plugin.rules)
    .filter(name => !plugin.rules[name].meta || !plugin.rules[name].meta.deprecated)
    .sort()
    .map(ruleName);

const configShape = (plugin, name) => {
  const config = plugin.configs[name] || {};
  return {
    name,
    hasEnv: Object.prototype.hasOwnProperty.call(config, 'env'),
    pluginsIsArray: Array.isArray(config.plugins),
    pluginsJest: Boolean(config.plugins && config.plugins.jest === plugin),
    hasLanguageOptionsGlobals: Boolean(
      config.languageOptions && config.languageOptions.globals,
    ),
    hasRules: Boolean(config.rules),
    ruleCount: config.rules ? Object.keys(config.rules).length : 0,
  };
};

const flatDiagnostics = (repo, plugin, config, code, filename = 'sample.test.js') => {
  const { Linter } = loadEslint(repo);
  const linter = (() => {
    try {
      return new Linter({ configType: 'flat', cwd: repo });
    } catch {
      try {
        return new Linter({ cwd: repo });
      } catch {
        return new Linter();
      }
    }
  })();
  const flat = {
    plugins: { jest: plugin },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'script',
      globals:
        (config.languageOptions && config.languageOptions.globals) ||
        plugin.environments.globals.globals,
    },
    rules: config.rules,
  };
  return normalizeMessages(linter.verify(code, [flat], { filename }));
};

const withTargetTechnology = async (repo, probe) => {
  const technology = targetTechnology(repo);
  if (!targetTechnologyOk(repo)) {
    let detail;
    try {
      detail = await probe();
    } catch (error) {
      detail = result(false, { error: error.message });
    }
    return result(false, { technology, detail: detail.value });
  }
  const detail = await probe();
  return result(detail.ok, { technology, detail: detail.value });
};

const pluginExport = (repo, plugin) => {
  const globals = plugin.environments.globals.globals;
  const value = {
    name: plugin.meta.name,
    version: plugin.meta.version,
    configKeys: Object.keys(plugin.configs),
    ruleCount: Object.keys(plugin.rules).length,
    globals: sortedKeys(globals),
  };
  return result(
    value.name === 'eslint-plugin-jest' &&
      /^\d+\.\d+\.\d+/u.test(value.version) &&
      value.configKeys.length === 6 &&
      value.ruleCount === 71 &&
      ['describe', 'it', 'expect'].every(key => key in globals),
    value,
  );
};

const configKeys = (repo, plugin) => {
  const keys = Object.keys(plugin.configs);
  return result(same(keys, CONFIG_NAMES), keys);
};

const allExactRuleSet = (repo, plugin) => {
  const all = sortedKeys(plugin.configs.all.rules);
  const flatAll = sortedKeys(plugin.configs['flat/all'].rules);
  const expected = nonDeprecatedRuleNames(plugin);
  return result(
    same(all, flatAll) && same(all, expected) && all.length === 71,
    {
      count: all.length,
      allEqualsFlatAll: same(all, flatAll),
      allEqualsNonDeprecated: same(all, expected),
      hash: sha256(JSON.stringify(all)),
    },
  );
};

const inventory = (repo, plugin) => {
  const ruleNames = sortedKeys(plugin.rules);
  const value = {
    ruleCount: ruleNames.length,
    ruleHash: sha256(JSON.stringify(ruleNames)),
    configKeys: Object.keys(plugin.configs),
    recommendedHash: sha256(JSON.stringify(flattened(plugin.configs.recommended))),
    styleHash: sha256(JSON.stringify(flattened(plugin.configs.style))),
    allHash: sha256(JSON.stringify(flattened(plugin.configs.all))),
  };
  return result(value.ruleCount === 71 && value.configKeys.length === 6, value);
};

const sameNamedTables = (repo, plugin) => {
  const value = {
    all: same(flattened(plugin.configs.all), flattened(plugin.configs['flat/all'])),
    recommended: same(
      flattened(plugin.configs.recommended),
      flattened(plugin.configs['flat/recommended']),
    ),
    style: same(flattened(plugin.configs.style), flattened(plugin.configs['flat/style'])),
  };
  return result(value.all && value.recommended && value.style, value);
};

const recommendedStyleTables = (repo, plugin) => {
  const value = {
    recommended: same(flattened(plugin.configs.recommended), EXPECTED_RECOMMENDED),
    style: same(flattened(plugin.configs.style), EXPECTED_STYLE),
    recommendedCount: Object.keys(plugin.configs.recommended.rules).length,
    styleCount: Object.keys(plugin.configs.style.rules).length,
  };
  return result(
    value.recommended && value.style && value.recommendedCount === 19 && value.styleCount === 3,
    value,
  );
};

const deprecatedOmission = (repo, plugin) => {
  const configured = sortedKeys(plugin.configs.all.rules).map(name => name.replace(/^jest\//u, ''));
  const deprecated = sortedKeys(plugin.rules).filter(
    name => plugin.rules[name].meta && plugin.rules[name].meta.deprecated,
  );
  const unexpected = configured.filter(name => deprecated.includes(name));
  const missing = nonDeprecatedRuleNames(plugin)
    .map(name => name.replace(/^jest\//u, ''))
    .filter(name => !configured.includes(name));
  return result(unexpected.length === 0 && missing.length === 0, {
    configuredCount: configured.length,
    deprecatedInConfig: unexpected,
    missing,
  });
};

const recommendedDiagnostics = (repo, plugin) => {
  const code = "test.only('focused', () => { expect(true).toBe(true); });";
  const recommended = flatDiagnostics(repo, plugin, plugin.configs.recommended, code);
  const flatRecommended = flatDiagnostics(
    repo,
    plugin,
    plugin.configs['flat/recommended'],
    code,
  );
  return result(same(recommended, flatRecommended) && recommended.length > 0, {
    recommended,
    flatRecommended,
  });
};

const settingsSurface = (repo, plugin) => {
  const alias = lintText(repo, plugin, "context.only('x', () => {});", 'no-focused-tests', {
    settings: { jest: { globalAliases: { describe: ['context'] } } },
  });
  const withoutAlias = lintText(repo, plugin, "context.only('x', () => {});", 'no-focused-tests');
  const globalPackage = lintText(
    repo,
    plugin,
    "import { test } from 'bun:test';\ntest.only('x', () => {});",
    'no-focused-tests',
    {
      sourceType: 'module',
      settings: { jest: { globalPackage: 'bun:test' } },
    },
  );
  const numberVersion = lintText(repo, plugin, "jest.genMockFromModule('x');", 'no-deprecated-functions', {
    settings: { jest: { version: 27 } },
  });
  const stringVersion = lintText(repo, plugin, "jest.genMockFromModule('x');", 'no-deprecated-functions', {
    settings: { jest: { version: '27.4.0' } },
  });
  const value = {
    aliasReports: alias.some(message => message.messageId === 'focusedTest'),
    aliasWithoutSettingReports: withoutAlias.length > 0,
    globalPackageReports: globalPackage.some(message => message.messageId === 'focusedTest'),
    numberVersionMessageIds: numberVersion.map(message => message.messageId),
    stringVersionMessageIds: stringVersion.map(message => message.messageId),
  };
  return result(
    value.aliasReports &&
      !value.aliasWithoutSettingReports &&
      value.globalPackageReports &&
      same(value.numberVersionMessageIds, ['deprecatedFunction']) &&
      same(value.stringVersionMessageIds, ['deprecatedFunction']),
    value,
  );
};

const deprecatedFunctions = (repo, plugin) => {
  const v25 = lintText(repo, plugin, "jest.genMockFromModule('x');", 'no-deprecated-functions', {
    settings: { jest: { version: 25 } },
  });
  const v26Report = lintText(repo, plugin, "jest.genMockFromModule('x');", 'no-deprecated-functions', {
    settings: { jest: { version: 26 } },
  });
  const v26 = fixText(repo, plugin, "jest.genMockFromModule('x');", 'no-deprecated-functions', {
    settings: { jest: { version: 26 } },
  });
  const automaticReport = lintText(repo, plugin, "jest.genMockFromModule('x');", 'no-deprecated-functions');
  const automatic = fixText(repo, plugin, "jest.genMockFromModule('x');", 'no-deprecated-functions');
  const value = {
    v25Count: v25.length,
    v26MessageId: v26Report[0] && v26Report[0].messageId,
    v26Output: v26.output,
    automaticMessageId: automaticReport[0] && automaticReport[0].messageId,
    automaticOutput: automatic.output,
  };
  return result(
    value.v25Count === 0 &&
      value.v26MessageId === 'deprecatedFunction' &&
      value.v26Output === "jest.createMockFromModule('x');" &&
      value.automaticMessageId === 'deprecatedFunction' &&
      value.automaticOutput === "jest.createMockFromModule('x');",
    value,
  );
};

const detectModulePath = libRoot => path.join(libRoot, 'rules/utils/detectJestVersion.js');

const autoDetection = (repo, plugin, libRoot) => {
  const detect = require(detectModulePath(libRoot)).detectJestVersion;
  const first = detect();
  const second = detect();
  const value = {
    firstType: typeof first,
    sameCall: first === second,
  };
  return result(value.firstType === 'number' && value.sameCall, value);
};

const detectThrows = (repo, plugin, libRoot) => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'migration-no-jest-'));
  const moduleFile = path.join(
    temp,
    'node_modules',
    'eslint-plugin-jest',
    'lib',
    'rules',
    'detectJestVersion.js',
  );
  fs.mkdirSync(path.dirname(moduleFile), { recursive: true });
  fs.copyFileSync(detectModulePath(libRoot), moduleFile);
  const script = `try { require(${JSON.stringify(moduleFile)}).detectJestVersion(); process.exit(2); } catch (error) { console.error(error.message); process.exit(0); }`;
  const child = spawnSync(process.execPath, ['-e', script], {
    cwd: temp,
    encoding: 'utf8',
  });
  const output = `${child.stdout || ''}${child.stderr || ''}`;
  fs.rmSync(temp, { recursive: true, force: true });
  return result(child.status === 0 && /Unable to detect Jest version/u.test(output), {
    threw: child.status === 0,
    message: output.trim().split('\n').pop() || '',
  });
};

const pluginSemantics = (repo, plugin) => {
  const metadata = ruleMetadata(plugin);
  const messages = lintText(repo, plugin, "test.only('x', () => {});", 'no-focused-tests');
  const focused = messages[0] || {};
  const value = {
    metadataHash: sha256(JSON.stringify(metadata)),
    ruleCount: metadata.length,
    messageId: focused.messageId || null,
    suggestionId:
      (focused.suggestions && focused.suggestions[0] && focused.suggestions[0].messageId) ||
      null,
  };
  return result(
    value.ruleCount === 71 &&
      value.messageId === 'focusedTest' &&
      value.suggestionId === 'suggestRemoveFocus',
    value,
  );
};

const typeAwareMetadata = (repo, plugin) => {
  const actual = sortedKeys(plugin.rules).filter(
    name => plugin.rules[name].meta && plugin.rules[name].meta.docs && plugin.rules[name].meta.docs.requiresTypeChecking,
  );
  return result(same(actual, EXPECTED_TYPE_AWARE), actual);
};

const requireTypeRule = (repo, plugin) => {
  const names = ['valid-expect-with-promise', 'no-unnecessary-assertion'];
  const value = names.map(name => ({
    name,
    rule: Boolean(plugin.rules[name]),
    requiresTypeChecking: Boolean(
      plugin.rules[name] && plugin.rules[name].meta && plugin.rules[name].meta.docs && plugin.rules[name].meta.docs.requiresTypeChecking,
    ),
  }));
  return result(value.every(item => item.rule && item.requiresTypeChecking), value);
};

const dtsEntry = repo => {
  const text = readText(path.join(repo, 'index.d.ts'));
  const value = {
    hasMeta: /meta\s*:/u.test(text),
    hasGlobals: /environments\s*:\s*\{[\s\S]*globals/u.test(text),
    configKeys: CONFIG_NAMES.filter(name => {
      const escaped = name.replace('/', '\\/');
      return new RegExp(`(?:['"]${escaped}['"]|${escaped})\\s*:`, 'u').test(text);
    }),
    hasRulesIndex: hasRulesIndexSignature(text),
  };
  return result(
    value.hasMeta && value.hasGlobals && value.hasRulesIndex && value.configKeys.length === 6,
    value,
  );
};

const docsEachRule = repo => {
  const rulesDir = path.join(repo, 'src/rules');
  const ruleNames = (exists(rulesDir) ? fs.readdirSync(rulesDir, { withFileTypes: true }) : [])
    .filter(entry => entry.isFile() && /\.ts$/u.test(entry.name))
    .map(entry => entry.name.slice(0, -3))
    .sort();
  const docs = exists(path.join(repo, 'docs/rules'))
    ? fs.readdirSync(path.join(repo, 'docs/rules')).filter(file => file.endsWith('.md')).map(file => file.slice(0, -3)).sort()
    : [];
  const tests = exists(path.join(repo, 'src/rules/__tests__'))
    ? fs.readdirSync(path.join(repo, 'src/rules/__tests__')).filter(file => file.endsWith('.test.ts')).map(file => file.replace(/\.test\.ts$/u, '')).sort()
    : [];
  const missingDocs = ruleNames.filter(name => !docs.includes(name));
  const missingTests = ruleNames.filter(name => !tests.includes(name));
  return result(
    ruleNames.length === 71 && missingDocs.length === 0 && missingTests.length === 0,
    {
      ruleCount: ruleNames.length,
      docCount: docs.length,
      testCount: tests.length,
      missingDocs,
      missingTests,
    },
  );
};

const protectedFiles = repo => {
  const rulesDir = path.join(repo, 'src/rules');
  const ruleFiles = (exists(rulesDir) ? fs.readdirSync(rulesDir, { withFileTypes: true }) : [])
    .filter(
      entry =>
        entry.isFile() &&
        /\.(?:ts|js)$/u.test(entry.name) &&
        entry.name !== 'prefer-importing-jest-globals.ts',
    )
    .map(entry => path.join(rulesDir, entry.name));
  const testFiles = walk(
    path.join(repo, 'src/rules/__tests__'),
    file =>
      /\.test\.ts$/u.test(file) &&
      path.basename(file) !== 'prefer-importing-jest-globals.test.ts',
  ).filter(file => path.basename(file) !== 'test-utils.ts');
  const docFiles = walk(path.join(repo, 'docs/rules'), file => file.endsWith('.md'));
  const globalsFile = path.join(repo, 'src/globals.json');
  const files = [...ruleFiles, ...testFiles, ...docFiles, ...(exists(globalsFile) ? [globalsFile] : [])];
  return result(
    ruleFiles.length === 70 && testFiles.length === 70 && docFiles.length === 71,
    {
      hash: hashFiles(repo, files),
      ruleCount: ruleFiles.length,
      docCount: docFiles.length,
      testCount: testFiles.length,
    },
  );
};

const behavioralAssertions = repo => {
  const rulesTest = path.join(repo, 'src/__tests__/rules.test.ts');
  const snapshot = path.join(repo, 'src/__tests__/__snapshots__/rules.test.ts.snap');
  const text = `${exists(rulesTest) ? readText(rulesTest) : ''}\n${exists(snapshot) ? readText(snapshot) : ''}`;
  const value = {
    hasExpectations: /expect\s*\(/u.test(text),
    hasRuleTableCheck: /rules/u.test(text) && /config/u.test(text),
    hasSkip: SKIP_RE.test(codeWithoutLiterals(text)),
  };
  return result(value.hasExpectations && value.hasRuleTableCheck && !value.hasSkip, value);
};

const publicCommands = repo => {
  const pkg = readJSON(path.join(repo, 'package.json'));
  const scripts = pkg.scripts || {};
  const required = ['test', 'typecheck', 'lint', 'prettier:check'];
  const value = {
    scripts: sortedKeys(scripts),
    missing: required.filter(name => !(name in scripts)),
  };
  return result(value.missing.length === 0, value);
};

const duplicateValid = (repo, plugin) => {
  const messages = lintText(
    repo,
    plugin,
    "describe('outside', () => {\n  beforeEach(() => {});\n  test('bar', () => {});\n});",
    'no-duplicate-hooks',
  );
  return result(messages.length === 0, messages);
};

const duplicateInner = (repo, plugin) => {
  const messages = lintText(
    repo,
    plugin,
    "describe('outside', () => {\n  beforeAll(() => {});\n  test('bar', () => {});\n  describe('inner', () => {\n    beforeEach(() => {});\n    beforeEach(() => {});\n    test('inner bar', () => {});\n  });\n});",
    'no-duplicate-hooks',
  );
  return result(messages.length > 0, normalizeMessages(messages));
};

const preferExpectResolves = (repo, plugin, code, expectedCount) => {
  const messages = lintText(repo, plugin, code, 'prefer-expect-resolves');
  return result(messages.length === expectedCount, messages);
};

const lowercaseBoolean = (repo, plugin) => {
  const messages = lintText(repo, plugin, 'it(true, function () {});', 'prefer-lowercase-title');
  return result(messages.length === 0, messages);
};

const conditionalInvalid = (repo, plugin) => {
  const messages = lintText(
    repo,
    plugin,
    "it('foo', () => {\n  if (bar) {\n    expect(1).toBe(1);\n  }\n});",
    'no-conditional-in-test',
  );
  return result(messages.length > 0, normalizeMessages(messages));
};

const topLevelValid = (repo, plugin) => {
  const messages = lintText(
    repo,
    plugin,
    "describe('suite', () => {\n  it('my test', () => {});\n  describe('another', () => {});\n  test('my other test', () => {});\n});",
    'require-top-level-describe',
  );
  return result(messages.length === 0, messages);
};

const validExpectPromise = (repo, plugin) => {
  const code = "const x: Array<number> = [1, 2, 3];\n\nit('is true', async () => {\n  expect(x).toEqual(null);\n});";
  const root = path.join(repo, 'src/rules/__tests__/fixtures');
  const messages = typeAwareLint(repo, plugin, code, 'valid-expect-with-promise', {
    tsconfigRootDir: root,
    filename: path.join(root, 'file.ts'),
  });
  return result(messages.length === 0, messages);
};

const paddingValid = (repo, plugin) => {
  const code = [
    'foo();',
    'bar();',
    '',
    "const someText = 'abc';",
    'const someObject = {',
    '  one: 1,',
    '  two: 2,',
    '};',
    '',
    "// A comment before describe",
    "describe('someText', () => {",
    "  describe('some condition', () => {",
    '  });',
    '',
    "  describe('some other condition', () => {",
    '  });',
    '});',
  ].join('\n');
  const messages = lintText(repo, plugin, code, 'padding-around-describe-blocks');
  return result(messages.length === 0, messages);
};

const requireHookValid = (repo, plugin) => {
  const messages = lintText(
    repo,
    plugin,
    "describe('some tests', () => {\n  it('is true', () => {\n    expect(true).toBe(true);\n  });\n});",
    'require-hook',
  );
  return result(messages.length === 0, messages);
};

const doneCallbackInvalid = (repo, plugin) => {
  const messages = lintText(
    repo,
    plugin,
    "test('my test', async (done) => {\n  done();\n});",
    'no-done-callback',
  );
  return result(messages.length > 0, normalizeMessages(messages));
};

const validTitleValid = (repo, plugin) => {
  const messages = lintText(repo, plugin, "it('correctly sets the value', () => {});", 'valid-title');
  return result(messages.length === 0, messages);
};

const maxNestedValid = (repo, plugin) => {
  const messages = lintText(
    repo,
    plugin,
    "describe('foo', () => {\n  describe.only('bar', () => {\n    describe.skip('baz', () => {\n      it('something', async () => {\n        expect('something').toBe('something');\n      });\n    });\n  });\n});",
    'max-nested-describe',
    { ruleOptions: ['error', { max: 3 }] },
  );
  return result(messages.length === 0, messages);
};

const requireThrowMessage = (repo, plugin) => {
  const code = "test('empty rejects.toThrow', async () => {\n  const throwErrorAsync = async () => { throw new Error('a'); };\n  await expect(throwErrorAsync()).rejects.toThrow();\n  await expect(throwErrorAsync()).rejects.toThrowError();\n});";
  const messages = lintText(repo, plugin, code, 'require-to-throw-message');
  return result(
    messages.length === 2 && messages.every(message => message.messageId === 'addErrorMessage'),
    normalizeMessages(messages),
  );
};

const endingExpectValid = (repo, plugin) => {
  const code = "describe('GET /user', function () {\n  it('responds with json', function (done) {\n    doSomething();\n    request(app).get('/user').expect('Content-Type', /json/).expect(200, done);\n  });\n});";
  const messages = lintText(repo, plugin, code, 'prefer-ending-with-an-expect', {
    ruleOptions: ['error', { assertFunctionNames: ['expect', 'request.**.expect'] }],
  });
  return result(messages.length === 0, messages);
};

const importModule = (repo, plugin) => {
  const fixed = fixText(repo, plugin, "test('x', () => {});", 'prefer-importing-jest-globals', {
    sourceType: 'module',
  });
  return result(
    fixed.output.startsWith("import { test } from '@jest/globals';") &&
      fixed.messages.every(message => message.messageId === 'preferImportingJestGlobal'),
    fixed,
  );
};

const importScript = (repo, plugin) => {
  const fixed = fixText(repo, plugin, "test('x', () => {});", 'prefer-importing-jest-globals', {
    sourceType: 'script',
  });
  return result(
    fixed.output.startsWith("const { test } = require('@jest/globals');"),
    fixed,
  );
};

const useStrictMerge = (repo, plugin) => {
  const fixed = fixText(
    repo,
    plugin,
    "'use strict';\ntest('x', () => { expect(1).toBe(1); });",
    'prefer-importing-jest-globals',
    { sourceType: 'script' },
  );
  return result(
    fixed.output.startsWith(
      "'use strict';\nconst { expect, test } = require('@jest/globals');",
    ),
    fixed,
  );
};

const unnecessaryStrictNull = (repo, plugin) => {
  const fixtures = path.join(repo, 'src/rules/__tests__/fixtures');
  const code = 'expect(x).toBe(y);';
  const unstrict = typeAwareLint(repo, plugin, code, 'no-unnecessary-assertion', {
    tsconfigRootDir: path.join(fixtures, 'unstrict'),
    filename: path.join(fixtures, 'unstrict/file.ts'),
  });
  const strict = typeAwareLint(repo, plugin, code, 'no-unnecessary-assertion', {
    tsconfigRootDir: fixtures,
    filename: path.join(fixtures, 'file.ts'),
  });
  const value = {
    unstrict: unstrict.map(message => message.messageId),
    strict: strict.map(message => message.messageId),
  };
  return result(
    value.unstrict.includes('noStrictNullCheck') && !value.strict.includes('noStrictNullCheck'),
    value,
  );
};

const configsAllFlat = (repo, plugin) => {
  const shape = CONFIG_NAMES.map(name => configShape(plugin, name));
  const all = sortedKeys(plugin.configs.all.rules);
  const flatAll = sortedKeys(plugin.configs['flat/all'].rules);
  const expected = nonDeprecatedRuleNames(plugin);
  return result(
    shape.every(
      item =>
        item.pluginsJest &&
        item.hasLanguageOptionsGlobals &&
        item.hasRules &&
        !item.hasEnv &&
        !item.pluginsIsArray,
    ) &&
      same(all, flatAll) &&
      same(all, expected),
    { shape, allCount: all.length },
  );
};

const configsLegacyAbsent = (repo, plugin) => {
  const shape = ['all', 'recommended', 'style'].map(name => configShape(plugin, name));
  const value = {
    shape,
    recommended: same(flattened(plugin.configs.recommended), EXPECTED_RECOMMENDED),
    style: same(flattened(plugin.configs.style), EXPECTED_STYLE),
  };
  return result(
    value.shape.every(
      item =>
        item.pluginsJest &&
        item.hasLanguageOptionsGlobals &&
        item.hasRules &&
        !item.hasEnv &&
        !item.pluginsIsArray,
    ) &&
      value.recommended &&
      value.style,
    value,
  );
};

const flatLoadAll = async (repo, plugin) => {
  const { ESLint } = loadEslint(repo);
  const parser = requireFrom(repo, '@typescript-eslint/parser');
  const root = path.join(repo, 'src/rules/__tests__/fixtures');
  const eslint = new ESLint({
    cwd: repo,
    overrideConfigFile: true,
    overrideConfig: [
      {
        files: ['**/*.ts'],
        ...plugin.configs['flat/all'],
        languageOptions: {
          parser,
          parserOptions: {
            project: './tsconfig.json',
            tsconfigRootDir: root,
            disallowAutomaticSingleRunInference: true,
          },
        },
      },
    ],
  });
  const [lintResult] = await eslint.lintText(
    "test('x', () => { expect(1).toBe(1); });",
    { filePath: path.join(root, 'file.ts') },
  );
  const messages = normalizeMessages(lintResult.messages);
  return result(Array.isArray(lintResult.messages), {
    messageCount: messages.length,
    ruleIds: [...new Set(messages.map(message => message.ruleId))].sort(),
  });
};

const globalsInjection = async (repo, plugin) => {
  const { ESLint } = loadEslint(repo);
  const config = plugin.configs.recommended;
  const eslint = new ESLint({
    cwd: repo,
    overrideConfigFile: true,
    overrideConfig: [
      {
        files: ['**/*.test.js'],
        ...config,
        rules: {
          ...config.rules,
          'no-undef': 'error',
        },
      },
    ],
  });
  const [lintResult] = await eslint.lintText(
    "describe('d', () => { it('i', () => { expect(1).toBe(1); }); });",
    { filePath: 'sample.test.js' },
  );
  const messages = normalizeMessages(lintResult.messages);
  const undefinedMessages = messages.filter(message => message.ruleId === 'no-undef');
  return result(undefinedMessages.length === 0, {
    noUndefCount: undefinedMessages.length,
    hasConfigGlobals: Boolean(config.languageOptions && config.languageOptions.globals),
    configHasEnv: Object.prototype.hasOwnProperty.call(config, 'env'),
    configPluginsIsArray: Array.isArray(config.plugins),
  });
};

const dependency = repo => {
  const value = targetTechnology(repo);
  return result(
    value.declared &&
      value.installed &&
      value.lock &&
      value.enginesCompatible &&
      value.eslint9TypesRemoved,
    value,
  );
};

const typesAmbient = repo => {
  const text = readText(path.join(repo, 'types.d.ts'));
  const value = {
    hasLegacyConfig: /LegacyConfig/u.test(text),
    hasFlatConfig: /FlatConfig/u.test(text),
    usesLinterConfig: /(?:ESLint\.)?Linter\.(?:Config|FlatConfig)\b/u.test(text),
  };
  return result(!value.hasLegacyConfig && value.usesLinterConfig, value);
};

const typesIndex = repo => {
  const text = readText(path.join(repo, 'index.d.ts'));
  const configAliases = new Set(
    [
      ...text.matchAll(
        /type\s+([A-Za-z_$][\w$]*)\s*=\s*[^;]*Linter\.(?:Config|FlatConfig)[^;]*;/gsu,
      ),
    ].map(match => match[1]),
  );
  const configEntries = CONFIG_NAMES.map(name => ({
    name,
    config: (() => {
      const match = new RegExp(
        `['"]?${name.replace('/', '\\/')}['"]?\\s*:\\s*([A-Za-z_$][\\w$]*(?:\\.(?:Config|FlatConfig))?)\\b`,
        'u',
      ).exec(text);
      return Boolean(
        match &&
          (match[1] === 'Linter.Config' ||
            match[1] === 'Linter.FlatConfig' ||
            configAliases.has(match[1])),
      );
    })(),
  }));
  const value = {
    hasLegacyConfig: /LegacyConfig/u.test(text),
    hasFlatConfig: /FlatConfig/u.test(text),
    configEntries,
    hasRulesIndex: hasRulesIndexSignature(text),
  };
  return result(
    !value.hasLegacyConfig &&
      value.hasRulesIndex &&
      value.configEntries.every(item => item.config),
    value,
  );
};

const productionNoLegacy = (repo, plugin) => {
  const files = ['src/index.ts', 'index.d.ts', 'types.d.ts'].map(file => path.join(repo, file));
  const text = files.map(file => (exists(file) ? readText(file) : '')).join('\n');
  const configs = configsAllFlat(repo, plugin);
  const value = {
    hasLegacyConfig: /LegacyConfig/u.test(text),
    hasCreateRCConfig: /createRCConfig/u.test(text),
    hasLegacyEnv: /env\s*:\s*\{\s*['"]jest\/globals['"]/u.test(text),
    configsFlat: configs.ok,
  };
  return result(
    !value.hasLegacyConfig &&
      !value.hasCreateRCConfig &&
      !value.hasLegacyEnv &&
      value.configsFlat,
    value,
  );
};

const eslintConfigType = repo => {
  const text = readText(path.join(repo, 'eslint.config.js'));
  const value = {
    usesLinterConfigArray: /Linter\.Config\[\]/u.test(text),
    usesLinterFlatConfig: /Linter\.FlatConfig/u.test(text),
    usesLegacyConfig: /LegacyConfig/u.test(text),
  };
  return result(
    (value.usesLinterConfigArray || value.usesLinterFlatConfig) &&
      !value.usesLegacyConfig,
    value,
  );
};

const readmeFlatOnly = repo => {
  const text = readText(path.join(repo, 'README.md'));
  const value = {
    hasLegacyPluginArray: /"plugins"\s*:\s*\[\s*"jest"\s*\]/u.test(text),
    hasLegacyEnv: /["']jest\/globals["']\s*:\s*true/u.test(text),
    mentionsEslintrc: /\.eslintrc/u.test(text),
    mentionsRecommendedFlat: /configs\.(?:recommended|\[['"]flat\/recommended['"]\])/u.test(text),
  };
  return result(
    !value.hasLegacyPluginArray &&
      !value.hasLegacyEnv &&
      value.mentionsRecommendedFlat,
    value,
  );
};

const ciMatrix = repo => {
  const file = path.join(repo, '.github/workflows/nodejs.yml');
  const text = exists(file) ? readText(file) : '';
  const value = {
    hasEslintVersionMatrix: /eslint-version\s*:/u.test(text),
    pins1050: /10\.5\.0/u.test(text),
  };
  return result(value.hasEslintVersionMatrix && value.pins1050, value);
};

const snapshotFlat = repo => {
  const file = path.join(repo, 'src/__tests__/__snapshots__/rules.test.ts.snap');
  const text = exists(file) ? readText(file) : '';
  const value = {
    hasLanguageOptions: /"languageOptions"\s*:/u.test(text),
    hasPluginsObject: /"plugins"\s*:\s*\{/u.test(text),
    hasEnv: /"env"\s*:\s*\{/u.test(text),
    hasPluginsArray: /"plugins"\s*:\s*\[/u.test(text),
  };
  return result(
    value.hasLanguageOptions && value.hasPluginsObject && !value.hasEnv && !value.hasPluginsArray,
    value,
  );
};

const rulesTestFlat = (repo, plugin) => {
  // Verify the contract behaviour instead of requiring the reference's exact
  // test prose: every exported config must be a flat config and the aliased
  // `recommended` / `flat/recommended` tables must produce identical
  // diagnostics.  The repository's own test surface only has to reference the
  // flat shape in some equivalent form (direct property access, object
  // matching, `toHaveProperty`, or the committed snapshot).
  const shape = CONFIG_NAMES.map(name => configShape(plugin, name));
  const checksFlatConfig = shape.every(
    item =>
      item.pluginsJest &&
      item.hasLanguageOptionsGlobals &&
      item.hasRules &&
      !item.hasEnv &&
      !item.pluginsIsArray,
  );
  const diagnostics = recommendedDiagnostics(repo, plugin);
  const testFile = path.join(repo, 'src/__tests__/rules.test.ts');
  const snapshot = path.join(repo, 'src/__tests__/__snapshots__/rules.test.ts.snap');
  const text = `${exists(testFile) ? readText(testFile) : ''}\n${
    exists(snapshot) ? readText(snapshot) : ''
  }`;
  const referencesFlatConfig =
    /plugins\s*\??\.\s*jest|plugins\s*:\s*\{[\s\S]{0,80}?jest|["']plugins["']\s*:\s*\{[\s\S]{0,80}?jest|toHaveProperty\(\s*['"]plugins\.jest/u.test(text) &&
    /languageOptions|toHaveProperty\(\s*['"]languageOptions\.globals/u.test(text) &&
    /globals/u.test(text);
  const hasSkip = SKIP_RE.test(codeWithoutLiterals(text));
  const value = {
    checksFlatConfig,
    referencesFlatConfig,
    checksDiagnostics: diagnostics.ok,
    hasSkip,
    diagnostics: diagnostics.value,
  };
  return result(
    checksFlatConfig && diagnostics.ok && referencesFlatConfig && !hasSkip,
    value,
  );
};

const configsDiagnostics = (repo, plugin) => {
  const value = recommendedDiagnostics(repo, plugin);
  return result(value.ok, value.value);
};

const sourceTypeSelection = (repo, plugin) => {
  const script = fixText(repo, plugin, "test('x', () => {});", 'prefer-importing-jest-globals', {
    sourceType: 'script',
  });
  const module = fixText(repo, plugin, "test('x', () => {});", 'prefer-importing-jest-globals', {
    sourceType: 'module',
  });
  const parserFallback = fixText(
    repo,
    plugin,
    "test('x', () => {});",
    'prefer-importing-jest-globals',
    {
      sourceType: 'script',
      parserOptions: { sourceType: 'module' },
    },
  );
  const value = {
    scriptOutput: script.output,
    moduleOutput: module.output,
    parserFallbackOutput: parserFallback.output,
  };
  return result(
    value.scriptOutput.startsWith("const { test } = require('@jest/globals');") &&
      value.moduleOutput.startsWith("import { test } from '@jest/globals';") &&
      value.parserFallbackOutput.startsWith("const { test } = require('@jest/globals');"),
    value,
  );
};

const sourceTypeMerge = (repo, plugin) => {
  const selection = sourceTypeSelection(repo, plugin);
  const merge = useStrictMerge(repo, plugin);
  return result(selection.ok && merge.ok, {
    selection: selection.value,
    merge: merge.value,
  });
};

const settingsLegacy = (repo, plugin) => {
  const settings = settingsSurface(repo, plugin);
  const legacy = configsLegacyAbsent(repo, plugin);
  return result(settings.ok && legacy.ok, {
    settings: settings.value,
    configs: legacy.value,
  });
};

const PROBES = {
  pluginExport,
  configKeys,
  allExactRuleSet,
  inventory,
  sameNamedTables,
  recommendedStyleTables,
  deprecatedOmission,
  recommendedDiagnostics,
  settingsSurface,
  deprecatedFunctions,
  autoDetection,
  detectThrows,
  pluginSemantics,
  typeAwareMetadata,
  requireTypeRule,
  dtsEntry,
  docsEachRule,
  protectedFiles,
  behavioralAssertions,
  publicCommands,
  duplicateValid,
  duplicateInner,
  conditionalInvalid,
  topLevelValid,
  validExpectPromise,
  paddingValid,
  requireHookValid,
  doneCallbackInvalid,
  validTitleValid,
  maxNestedValid,
  requireThrowMessage,
  endingExpectValid,
  importModule,
  importScript,
  useStrictMerge,
  unnecessaryStrictNull,
  lowercaseBoolean,
  configsAllFlat,
  configsLegacyAbsent,
  flatLoadAll,
  globalsInjection,
  dependency,
  typesAmbient,
  typesIndex,
  productionNoLegacy,
  eslintConfigType,
  readmeFlatOnly,
  ciMatrix,
  snapshotFlat,
  rulesTestFlat,
  configsDiagnostics,
  settingsLegacy,
  sourceTypeSelection,
  sourceTypeMerge,
};

const P2P = {
  'P2P-001': 'duplicateValid',
  'P2P-002': 'preferExpectResolvesValid',
  'P2P-003': 'lowercaseBoolean',
  'P2P-004': 'duplicateInner',
  'P2P-005': 'detectThrows',
  'P2P-006': 'conditionalInvalid',
  'P2P-007': 'topLevelValid',
  'P2P-008': 'validExpectPromise',
  'P2P-009': 'paddingValid',
  'P2P-010': 'requireHookValid',
  'P2P-011': 'doneCallbackInvalid',
  'P2P-012': 'preferExpectResolvesValid',
  'P2P-013': 'validTitleValid',
  'P2P-014': 'maxNestedValid',
  'P2P-015': 'allExactRuleSet',
  'P2P-016': 'requireThrowMessage',
  'P2P-017': 'endingExpectValid',
  'P2P-018': 'inventory',
  'P2P-019': 'importModule',
  'P2P-020': 'docsEachRule',
  'P2P-021': 'importScript',
  'P2P-022': 'sameNamedTables',
  'P2P-023': 'useStrictMerge',
  'P2P-024': 'deprecatedFunctions',
  'P2P-025': 'settingsSurface',
  'P2P-026': 'behavioralAssertions',
  'P2P-027': 'publicCommands',
  'P2P-028': 'pluginExport',
  'P2P-029': 'autoDetection',
  'P2P-030': 'unnecessaryStrictNull',
  'P2P-031': 'settingsSurface',
  'P2P-032': 'configKeys',
  'P2P-033': 'deprecatedOmission',
  'P2P-034': 'requireTypeRule',
  'P2P-035': 'typeAwareMetadata',
  'P2P-036': 'dtsEntry',
  'P2P-037': 'pluginSemantics',
  'P2P-038': 'protectedFiles',
  'P2P-039': 'recommendedStyleTables',
  'P2P-040': 'recommendedDiagnostics',
};

const DIFF = {
  'DIFF-001': 'pluginExport',
  'DIFF-002': 'configKeys',
  'DIFF-003': 'sameNamedTables',
  'DIFF-004': 'deprecatedOmission',
  'DIFF-005': 'recommendedStyleTables',
  'DIFF-006': 'recommendedDiagnostics',
  'DIFF-007': 'settingsSurface',
  'DIFF-008': 'dtsEntry',
  'DIFF-009': 'importModule',
  'DIFF-010': 'allExactRuleSet',
  'DIFF-011': 'importScript',
  'DIFF-012': 'useStrictMerge',
  'DIFF-013': 'deprecatedFunctions',
  'DIFF-014': 'autoDetection',
  'DIFF-015': 'typeAwareMetadata',
  'DIFF-016': 'unnecessaryStrictNull',
  'DIFF-017': 'pluginSemantics',
  'DIFF-018': 'inventory',
  'DIFF-019': 'settingsSurface',
  'DIFF-020': 'protectedFiles',
  'DIFF-021': 'behavioralAssertions',
  'DIFF-022': 'publicCommands',
};

const F2P = {
  'F2P-001': 'configsAllFlat',
  'F2P-002': 'sourceTypeMerge',
  'F2P-003': 'configsLegacyAbsent',
  'F2P-004': 'configsAllFlat',
  'F2P-005': 'flatLoadAll',
  'F2P-006': 'dependency',
  'F2P-007': 'typesAmbient',
  'F2P-008': 'globalsInjection',
  'F2P-009': 'globalsInjection',
  'F2P-010': 'configsAllFlat',
  'F2P-011': 'dependency',
  'F2P-012': 'dependency',
  'F2P-013': 'globalsInjection',
  'F2P-014': 'dependency',
  'F2P-015': 'eslintConfigType',
  'F2P-016': 'configsAllFlat',
  'F2P-017': 'globalsInjection',
  'F2P-018': 'configsAllFlat',
  'F2P-019': 'settingsLegacy',
  'F2P-020': 'sourceTypeSelection',
  'F2P-021': 'snapshotFlat',
  'F2P-022': 'readmeFlatOnly',
  'F2P-023': 'configsAllFlat',
  'F2P-024': 'configsLegacyAbsent',
  'F2P-025': 'configsLegacyAbsent',
  'F2P-026': 'flatLoadAll',
  'F2P-027': 'readmeFlatOnly',
  'F2P-028': 'configsDiagnostics',
  'F2P-029': 'flatLoadAll',
  'F2P-030': 'sourceTypeSelection',
  'F2P-031': 'settingsLegacy',
  'F2P-032': 'eslintConfigType',
  'F2P-033': 'configsAllFlat',
  'F2P-034': 'rulesTestFlat',
  'F2P-035': 'configsDiagnostics',
  'F2P-036': 'productionNoLegacy',
  'F2P-037': 'configsAllFlat',
  'F2P-038': 'snapshotFlat',
  'F2P-039': 'globalsInjection',
  'F2P-040': 'configsAllFlat',
  'F2P-041': 'sourceTypeSelection',
  'F2P-042': 'sourceTypeSelection',
  'F2P-043': 'flatLoadAll',
  'F2P-044': 'configsDiagnostics',
  'F2P-045': 'configsAllFlat',
  'F2P-046': 'configsLegacyAbsent',
  'F2P-047': 'configsAllFlat',
  'F2P-048': 'globalsInjection',
  'F2P-049': 'globalsInjection',
  'F2P-050': 'sourceTypeSelection',
  'F2P-051': 'flatLoadAll',
  'F2P-052': 'configsAllFlat',
  'F2P-053': 'configsAllFlat',
  'F2P-054': 'flatLoadAll',
  'F2P-055': 'sourceTypeSelection',
  'F2P-056': 'typesIndex',
  'F2P-057': 'flatLoadAll',
  'F2P-058': 'configsAllFlat',
  'F2P-059': 'flatLoadAll',
  'F2P-060': 'configsAllFlat',
  'F2P-061': 'flatLoadAll',
  'F2P-062': 'rulesTestFlat',
  'F2P-063': 'sourceTypeSelection',
  'F2P-064': 'ciMatrix',
  'F2P-065': 'snapshotFlat',
  'F2P-066': 'sourceTypeSelection',
  'F2P-067': 'configsLegacyAbsent',
  'F2P-068': 'snapshotFlat',
  'F2P-069': 'typesIndex',
  'F2P-070': 'sourceTypeMerge',
  'F2P-071': 'rulesTestFlat',
  'F2P-072': 'sourceTypeSelection',
};

const preferExpectResolvesCodes = {
  'P2P-002': ["it('errors', async () => {\n  await expect(Promise.reject(new Error('x'))).rejects.toThrow('x');\n});", 0],
  'P2P-012': ["it('is true', async () => {\n  await expect(Promise.resolve(true)).resolves.toBe(true);\n});", 0],
};

const getProbe = id => {
  let name = P2P[id] || DIFF[id] || F2P[id];
  if (!name) {
    return null;
  }
  if (id === 'P2P-002' || id === 'P2P-012') {
    return async (repo, plugin) => {
      const [code] = preferExpectResolvesCodes[id];
      return preferExpectResolves(repo, plugin, code, 0);
    };
  }
  const probe = PROBES[name];
  if (!probe) {
    return null;
  }
  if (id.startsWith('F2P-')) {
    return async (repo, plugin, libRoot) =>
      withTargetTechnology(repo, () => probe(repo, plugin, libRoot));
  }
  return probe;
};

module.exports = {
  CONFIG_NAMES,
  F2P,
  PROBES,
  getProbe,
};
