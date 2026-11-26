'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { createRequire } = require('module');
const { spawnSync } = require('child_process');

const repoRequire = repo => createRequire(path.join(repo, 'package.json'));
const exists = file => {
  try {
    return fs.existsSync(file);
  } catch {
    return false;
  }
};
const readText = file => fs.readFileSync(file, 'utf8');
const readJSON = file => JSON.parse(readText(file));
const sha256 = value =>
  crypto.createHash('sha256').update(value).digest('hex');

const requireFrom = (repo, name) => repoRequire(repo)(name);

const walk = (dir, predicate = () => true) => {
  const files = [];
  if (!exists(dir)) {
    return files;
  }
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...walk(file, predicate));
    } else if (predicate(file)) {
      files.push(file);
    }
  }
  return files;
};

const buildPlugin = repo => {
  const babel = requireFrom(repo, '@babel/core');
  const buildRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'migration-build-'));
  const libRoot = path.join(buildRoot, 'lib');
  const srcRoot = path.join(repo, 'src');
  const repoModules = path.join(repo, 'node_modules');
  if (exists(repoModules)) {
    fs.symlinkSync(repoModules, path.join(buildRoot, 'node_modules'), 'dir');
  }
  const packageFile = path.join(repo, 'package.json');
  if (exists(packageFile)) {
    fs.copyFileSync(packageFile, path.join(buildRoot, 'package.json'));
  }
  const configFile = path.join(repo, 'babel.config.js');
  const transformOptions = {
    cwd: repo,
    babelrc: false,
    configFile: exists(configFile) ? configFile : false,
    sourceMaps: false,
  };
  for (const file of walk(srcRoot)) {
    const rel = path.relative(srcRoot, file);
    if (rel.split(path.sep).includes('__tests__')) {
      continue;
    }
    const out = path.join(libRoot, rel);
    fs.mkdirSync(path.dirname(out), { recursive: true });
    if (/\.(?:[cm]?ts|[cm]?js)$/u.test(file)) {
      const result = babel.transformFileSync(file, transformOptions);
      fs.writeFileSync(out.replace(/\.(?:[cm]?ts)$/u, '.js'), result.code);
    } else {
      fs.copyFileSync(file, out);
    }
  }
  return { root: libRoot, cleanup: () => fs.rmSync(buildRoot, { recursive: true, force: true }) };
};

const pluginCache = new Map();
const ensurePlugin = repo => {
  const key = path.resolve(repo);
  if (pluginCache.has(key)) {
    return pluginCache.get(key);
  }
  const libFile = path.join(key, 'lib/index.js');
  if (exists(libFile)) {
    const loaded = { plugin: require(libFile), root: path.join(key, 'lib'), cleanup: () => {} };
    pluginCache.set(key, loaded);
    return loaded;
  }
  const built = buildPlugin(key);
  const loaded = {
    plugin: require(path.join(built.root, 'index.js')),
    root: built.root,
    cleanup: built.cleanup,
  };
  pluginCache.set(key, loaded);
  return loaded;
};

const loadEslint = repo => requireFrom(repo, 'eslint');

const makeLinter = (Linter, repo) => {
  try {
    return new Linter({ configType: 'flat', ...(repo ? { cwd: repo } : {}) });
  } catch {
    try {
      return new Linter(repo ? { cwd: repo } : undefined);
    } catch {
      return new Linter();
    }
  }
};

const normalizeMessage = message => ({
  ruleId: message.ruleId || null,
  messageId: message.messageId || null,
  severity: message.severity,
  line: message.line || null,
  column: message.column || null,
  endLine: message.endLine || null,
  endColumn: message.endColumn || null,
  message: message.message,
  suggestions: (message.suggestions || []).map(s => ({
    messageId: s.messageId || null,
    desc: s.desc,
  })),
});

const normalizeMessages = messages =>
  messages.map(normalizeMessage).sort((a, b) =>
    JSON.stringify(a).localeCompare(JSON.stringify(b)),
  );

const makeFlatConfig = (plugin, rules, options = {}) => ({
  ...(options.files ? { files: options.files } : {}),
  plugins: { jest: plugin },
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: options.sourceType || 'script',
    ...(options.parser ? { parser: options.parser } : {}),
    ...(options.parserOptions ? { parserOptions: options.parserOptions } : {}),
    ...(options.globals
      ? { globals: options.globals }
      : { globals: plugin.environments.globals.globals }),
  },
  ...(options.settings ? { settings: options.settings } : {}),
  rules,
});

const lintText = (repo, plugin, code, ruleName, options = {}) => {
  const { Linter } = loadEslint(repo);
  const linter = makeLinter(Linter, repo);
  const config = makeFlatConfig(
    plugin,
    { [`jest/${ruleName}`]: options.ruleOptions || 'error' },
    options,
  );
  return normalizeMessages(
    linter.verify(code, [config], { filename: options.filename || 'sample.test.js' }),
  );
};

const fixText = (repo, plugin, code, ruleName, options = {}) => {
  const { Linter } = loadEslint(repo);
  const linter = makeLinter(Linter, repo);
  const config = makeFlatConfig(
    plugin,
    { [`jest/${ruleName}`]: options.ruleOptions || 'error' },
    options,
  );
  const result = linter.verifyAndFix(code, [config], {
    filename: options.filename || 'sample.test.js',
  });
  return { output: result.output, messages: normalizeMessages(result.messages) };
};

const targetVersionSatisfied = (spec, target) => {
  if (typeof spec !== 'string') {
    return false;
  }
  const value = spec.trim().replace(/^[v=\s]+/u, '');
  const targetMatch = /^(\d+)\.(\d+)\.(\d+)$/u.exec(target);
  if (!value || !targetMatch) {
    return false;
  }
  const parsed = targetMatch.slice(1).map(Number);
  const operator = /^(\^|~|>=|<=|>|<)/u.exec(value);
  const numbers = value.replace(/^(\^|~|>=|<=|>|<)/u, '').replace(/^[v=\s]+/u, '');
  const parts = numbers.split('.').map(part => (/^\d+$/u.test(part) ? Number(part) : null));
  const [major, minor, patch] = parts;
  if (major === null || major === undefined) {
    return false;
  }
  const cmp = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
  const base = [major, minor === null || minor === undefined ? 0 : minor, patch === null || patch === undefined ? 0 : patch];
  const atLeast = cmp(parsed, base) >= 0;
  const op = operator ? operator[1] : '';
  if (op === '^') {
    return parsed[0] === base[0] && (base[0] > 0 || parsed[1] === base[1]) && atLeast;
  }
  if (op === '~') {
    return parsed[0] === base[0] && parsed[1] === base[1] && atLeast;
  }
  if (op === '>=') return atLeast;
  if (op === '>') return cmp(parsed, base) > 0;
  if (op === '<=') return cmp(parsed, base) <= 0;
  if (op === '<') return cmp(parsed, base) < 0;
  // No operator: an exact version, or an X-range when components are omitted.
  if (minor === null || minor === undefined) return parsed[0] === base[0];
  if (patch === null || patch === undefined) {
    return parsed[0] === base[0] && parsed[1] === base[1];
  }
  return cmp(parsed, base) === 0;
};

const eslintPackage = repo => {
  try {
    return requireFrom(repo, 'eslint/package.json');
  } catch {
    return null;
  }
};

// Upgrading the repository to ESLint 10.5.0 also means the repository must no
// longer advertise Node runtimes its new ESLint dependency cannot run on.
// ``semver.subset`` certifies that every Node version admitted by the declared
// engines range is also admitted by the installed ESLint's engines range
// (instruction goal: upgrade the repository to ESLint 10.5.0; Constraint 13
// limits engine changes to that compatibility).  A range we cannot evaluate is
// not treated as a failure so a missing tool cannot turn into a false negative.
const nodeEnginesCompatible = (repo, pkg, installedPackage) => {
  const required =
    installedPackage && installedPackage.engines && installedPackage.engines.node;
  const declared = pkg.engines && pkg.engines.node;
  if (typeof required !== 'string' || !required) {
    return true;
  }
  if (typeof declared !== 'string' || !declared) {
    return false;
  }
  try {
    const semver = requireFrom(repo, 'semver');
    if (typeof semver.subset !== 'function') {
      return true;
    }
    return Boolean(semver.subset(declared, required));
  } catch {
    return true;
  }
};

// ESLint 10 ships its own type declarations, so the ESLint 9 ``@types/eslint``
// package is stale after the upgrade and its presence means the repository has
// not actually moved off the removed ESLint 9 types (instruction Constraint 4;
// the ``yarn typecheck`` flow is documented against the installed ESLint 10
// types).
const hasEslint9Types = pkg => {
  for (const field of ['devDependencies', 'dependencies']) {
    const section = pkg[field];
    if (section && Object.prototype.hasOwnProperty.call(section, '@types/eslint')) {
      return true;
    }
  }
  return false;
};

const targetTechnology = repo => {
  const pkg = readJSON(path.join(repo, 'package.json'));
  const installedPackage = eslintPackage(repo);
  const installed = installedPackage ? installedPackage.version : null;
  const lockFile = path.join(repo, 'yarn.lock');
  const lock = exists(lockFile) ? readText(lockFile) : '';
  const dev = pkg.devDependencies && pkg.devDependencies.eslint;
  return {
    // The declaration must admit ESLint 10.5.0; the resolved version is pinned
    // by the installed dependency and yarn.lock checks below (instruction: the
    // installed dependency and yarn.lock MUST resolve to 10.5.0).  The upgrade
    // is only complete when the repository also drops the removed ESLint 9
    // types and keeps its declared Node support inside the installed ESLint's
    // supported range.
    declared: targetVersionSatisfied(dev, '10.5.0'),
    installed: installed === '10.5.0',
    lock: lock.includes('eslint@npm:10.5.0'),
    // Keep this as diagnostic information only. An unreferenced historical
    // lock entry is not an installed runtime and does not mean production code
    // delegates to ESLint 9; the active declaration, installed package and
    // ESLint 10.5.0 lock resolution are checked independently below.
    noEslint9: !/"eslint@npm:9\./u.test(lock),
    enginesCompatible: nodeEnginesCompatible(repo, pkg, installedPackage),
    eslint9TypesRemoved: !hasEslint9Types(pkg),
    installedVersion: installed,
    declaredVersion: dev || null,
  };
};

const targetTechnologyOk = repo => {
  const tech = targetTechnology(repo);
  return tech.declared && tech.installed && tech.lock;
};

const hashFiles = (repo, files) =>
  sha256(
    files
      .map(file => path.relative(repo, file))
      .sort()
      .map(rel => `${rel}\0${readText(path.join(repo, rel))}`)
      .join('\0'),
  );

const ruleMetadata = plugin =>
  Object.keys(plugin.rules)
    .sort()
    .map(name => {
      const meta = plugin.rules[name].meta || {};
      return {
        name,
        type: meta.type || null,
        docs: meta.docs || null,
        schema: meta.schema || null,
        messages: meta.messages || null,
        fixable: meta.fixable || null,
        hasSuggestions: Boolean(meta.hasSuggestions),
        deprecated: Boolean(meta.deprecated),
      };
    });

const typeAwareLint = (repo, plugin, code, ruleName, options = {}) => {
  const parser = requireFrom(repo, '@typescript-eslint/parser');
  const { Linter } = loadEslint(repo);
  const linter = makeLinter(Linter, repo);
  const config = makeFlatConfig(plugin, { [`jest/${ruleName}`]: 'error' }, {
    files: options.files || ['**/*.ts'],
    parser,
    parserOptions: {
      sourceType: 'module',
      project: './tsconfig.json',
      tsconfigRootDir: options.tsconfigRootDir,
      disallowAutomaticSingleRunInference: true,
    },
    filename: options.filename,
  });
  return normalizeMessages(
    linter.verify(code, [config], { filename: options.filename }),
  );
};

module.exports = {
  buildPlugin,
  ensurePlugin,
  exists,
  fixText,
  hashFiles,
  lintText,
  loadEslint,
  makeFlatConfig,
  makeLinter,
  normalizeMessage,
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
};
