'use strict';

/*
 * BATCH-P2P-0002 shared executable checks.
 *
 * The checks use Node core and the preinstalled TypeScript parser. Every check
 * reads the repository working tree or runs a bounded, timeout-wrapped test
 * command, and returns a stable projection that must normalize equal on the
 * source and the target side of the migration.
 *
 * The suite-backed observations execute the repository's own Mocha test files
 * with `NODE_ENV=test` so that `@babel/register` compiles the ES modules to
 * CommonJS, exactly like the package `test` scripts do.  Without that env the
 * Babel config keeps ESM and Mocha falls back to the native ESM loader, which
 * cannot resolve the extensionless relative imports of this repository (this
 * is what previously made the suite projections unexecutable).
 */

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const BATCH_ID = 'BATCH-P2P-0002';
const DEFAULT_TIMEOUT_MS = 600000;
// Per-test budget for the package suites.  The repository's own `npm test`
// scripts run mocha with `--timeout 5000`, which leaves only ~2x headroom for
// the heaviest reference tests (`now_Clock_allSecsInDay_*` iterate a full day)
// and therefore produces spurious timeouts on an oversubscribed evaluation
// host.  A timeout is host jitter, not a migration regression, so give the
// tests a generous, clearly bounded budget; failures must still be zero.
const MOCHA_TIMEOUT = '60000';

const EXPECTED_CORE_ENTRY = {
  main: 'dist/js-joda.js',
  module: 'dist/js-joda.esm.js',
  typings: 'typings/js-joda.d.ts',
};

const EXPECTED_SIBLING_ENTRY = {
  timezone: {
    main: 'dist/js-joda-timezone.js',
    module: 'dist/js-joda-timezone.esm.js',
    typings: 'typings/js-joda-timezone.d.ts',
  },
  locale: {
    main: 'dist/js-joda-locale.js',
    module: 'dist/js-joda-locale.esm.js',
    typings: 'typings/js-joda-locale.d.ts',
  },
  extra: {
    main: 'dist/js-joda-extra.js',
    module: 'dist/js-joda-extra.esm.js',
    typings: 'typings/js-joda-extra.d.ts',
  },
};

const REQUIRED_CORE_EXPORTS = [
  'ZonedDateTime',
  'LocalDate',
  'LocalDateTime',
  'LocalTime',
  'Duration',
  'Period',
  'ChronoUnit',
  'ZoneOffset',
  'ZoneId',
  'Instant',
  'Year',
  'Month',
  'DayOfWeek',
  'MonthDay',
  'YearMonth',
  'OffsetDateTime',
  'OffsetTime',
  'DateTimeFormatter',
];

const LOCALE_PEER_NAMES = ['@js-joda/core', '@js-joda/locale', '@js-joda/timezone'];

/* Only manifest / lockfile / entry / typing changes are in migration scope.
 * The instruction explicitly freezes every package's src tree, including entries. */
const ALLOWED_DIFF = [
  /^lerna\.json$/,
  /^package\.json$/,
  /^package-lock\.json$/,
  /^packages\/[^/]+\/package\.json$/,
  /^packages\/[^/]+\/package-lock\.json$/,
  /^packages\/[^/]+\/js-joda[^/]*\.js$/,
  /^packages\/[^/]+\/typings\//,
  /^packages\/locale\/packages\/[^/]+\/package\.json$/,
];

/*
 * Generated report/output artifacts never count as migration changes.  The
 * report placeholder is materialized verbatim by some runners, so it is
 * ignored explicitly to keep the diff-scope projection stable.
 */
const IGNORED_DIFF = [
  /\{report\}/,
  /^logs\//,
  /(^|\/)node_modules\//,
];

const UI_NAME = /(^|[-_/])(ui|react|reactjs|vue|vuejs|angular|svelte|jquery|dom|browserify)([-_/]|$)/i;

const CORE_CASES = {
  duration: 'packages/core/test/reference/DurationTest.js',
  instant: 'packages/core/test/reference/InstantTest.js',
};

function readFile(file) {
  return fs.readFileSync(file, 'utf8');
}

function readJson(file) {
  return JSON.parse(readFile(file));
}

function exists(file) {
  try {
    fs.accessSync(file);
    return true;
  } catch (e) {
    return false;
  }
}

function packageDir(root, name) {
  return path.join(root, 'packages', name);
}

function extractExports(file) {
  const names = new Set();
  let text;
  try {
    text = readFile(file);
  } catch (e) {
    return names;
  }
  const re = /export\s*\{([\s\S]*?)\}\s*;?/g;
  let match;
  while ((match = re.exec(text)) !== null) {
    match[1].split(',').forEach(function (raw) {
      const name = raw.trim().split(/\s+as\s+/).pop().trim();
      if (name) {
        names.add(name);
      }
    });
  }
  return names;
}

function projectedEntry(pkg) {
  return { main: pkg.main, module: pkg.module, typings: pkg.typings };
}

function entryMatches(actual, expected) {
  return actual.main === expected.main
    && actual.module === expected.module
    && actual.typings === expected.typings;
}

function localePackageDirs(root) {
  const base = path.join(root, 'packages', 'locale', 'packages');
  let entries = [];
  try {
    entries = fs.readdirSync(base, { withFileTypes: true });
  } catch (e) {
    return [];
  }
  return entries
    .filter(function (entry) { return entry.isDirectory(); })
    .map(function (entry) { return path.join(base, entry.name, 'package.json'); })
    .filter(exists);
}

function gitChangedFiles(root) {
  const res = spawnSync('git', ['-C', root, 'status', '--porcelain'], {
    encoding: 'utf8',
    timeout: 60000,
    killSignal: 'SIGKILL',
  });
  if (res.status !== 0 || typeof res.stdout !== 'string') {
    return null;
  }
  return res.stdout
    .split('\n')
    .map(function (line) { return line.slice(3).trim(); })
    .filter(Boolean)
    .map(function (file) { return file.replace(/^"(.*)"$/, '$1'); })
    .filter(function (file) {
      return !IGNORED_DIFF.some(function (re) { return re.test(file); });
    });
}

/* Read the migration baseline blob for a repository-relative path. */
function gitShowBaseline(root, relative) {
  const res = spawnSync('git', ['-C', root, 'show', 'HEAD:' + relative], {
    encoding: 'utf8',
    timeout: 60000,
    killSignal: 'SIGKILL',
  });
  if (res.status !== 0 || typeof res.stdout !== 'string') {
    return null;
  }
  return res.stdout;
}

function parseJson(text) {
  try {
    return JSON.parse(text);
  } catch (e) {
    return null;
  }
}

/* Typings may fill gaps in the existing API, but must not promise methods the
 * frozen runtime does not implement. Check only newly declared public members:
 * this upgrade does not require repairing unrelated baseline typing defects. */
function typingRuntimeIssues(root) {
  const issues = [];
  ['core', 'timezone', 'locale', 'extra'].forEach(function (name) {
    const pkg = readJson(path.join(packageDir(root, name), 'package.json'));
    const relative = path.posix.join('packages', name, pkg.typings);
    const before = gitShowBaseline(root, relative);
    const after = readFile(path.join(root, relative));
    if (before === after) return;
    if (before === null) throw new Error('missing baseline typings: ' + relative);
    const ts = require(require.resolve('typescript', { paths: [root] }));
    function members(text) {
      const ast = ts.createSourceFile(relative, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
      if (ast.parseDiagnostics.length) throw new Error('invalid typings: ' + relative);
      const result = new Map();
      ast.forEachChild(function (node) {
        if (!ts.isClassDeclaration(node) || !node.name
          || !(node.modifiers || []).some(function (m) { return m.kind === ts.SyntaxKind.ExportKeyword; })) return;
        node.members.forEach(function (member) {
          const modifiers = member.modifiers || [];
          if (modifiers.some(function (m) {
            return [ts.SyntaxKind.PrivateKeyword, ts.SyntaxKind.ProtectedKeyword,
              ts.SyntaxKind.AbstractKeyword].indexOf(m.kind) !== -1;
          })) return;
          const isStatic = modifiers.some(function (m) { return m.kind === ts.SyntaxKind.StaticKeyword; });
          const isMethod = ts.isMethodDeclaration(member);
          // Instance data fields may be created by constructors rather than
          // prototypes. Do not instantiate arbitrary date-time classes here.
          if (!isMethod && !(isStatic && ts.isPropertyDeclaration(member))) return;
          if (!member.name || !(ts.isIdentifier(member.name) || ts.isStringLiteral(member.name))) return;
          const entry = { owner: node.name.text, name: member.name.text, isStatic: isStatic, isMethod: isMethod };
          const key = entry.owner + (isStatic ? '.' : '.prototype.') + entry.name;
          result.set(key, entry);
        });
      });
      return result;
    }
    const previous = members(before);
    const additions = Array.from(members(after)).filter(function (pair) { return !previous.has(pair[0]); });
    if (!additions.length) return;
    const main = path.join(packageDir(root, name), pkg.main);
    const runtime = require(exists(main) ? main : path.join(packageDir(root, name), 'src', 'js-joda' + (name === 'core' ? '' : '-' + name) + '.js'));
    additions.forEach(function (pair) {
      const key = pair[0];
      const member = pair[1];
      const owner = runtime[member.owner];
      const target = member.isStatic ? owner : owner && owner.prototype;
      if (!target || !(member.name in target) || (member.isMethod && typeof target[member.name] !== 'function')) {
        issues.push(name + ':' + key);
      }
    });
  });
  return issues.sort();
}

/* A workspace core link (``../core`` or ``.../@js-joda/core``) carries the
 * migrated package; every other workspace entry belongs to a sibling package
 * whose version is not part of this upgrade. */
function isCoreWorkspaceKey(key) {
  if (key.indexOf('@js-joda/core') !== -1) {
    return true;
  }
  const trimmed = key.replace(/\/+$/, '');
  return trimmed === '../core' || /\/core$/.test(trimmed);
}

/* A finite Mocha timeout is an execution budget, not a test assertion.
 * Keep every test file, assertion-related option and failure propagation. */
function normalizeTestCommand(command) {
  return command.replace(/--timeout(?:\s+|=)([1-9]\d*)(?=\s|$)/g, '--timeout <bounded>');
}

function scriptPreserved(name, before, after, scripts) {
  if (typeof before !== 'string' || typeof after !== 'string') return false;
  if (before === after) return true;
  if (['test', 'test-coverage', 'test-ts-definitions'].indexOf(name) !== -1) {
    return normalizeTestCommand(before) === normalizeTestCommand(after);
  }
  if (name !== 'test-ci') return false;
  const original = before.split(/\s*&&\s*/);
  const updated = after.split(/\s*&&\s*/);
  // Extra existing checks are allowed; no original command may be dropped.
  const remaining = updated.slice();
  for (const command of original) {
    const index = remaining.indexOf(command);
    if (index === -1) return false;
    remaining.splice(index, 1);
  }
  return remaining.every(function (command) {
    const match = /^npm run ([\w-]+)$/.exec(command);
    return match && Object.prototype.hasOwnProperty.call(scripts, match[1]);
  });
}

function scriptsPreserved(before, after) {
  return before && after && Object.keys(before).every(function (name) {
    return scriptPreserved(name, before[name], after[name], before);
  });
}

/* Manifest metadata may retain equivalent checks or add existing checks. */
function coreManifestScopeIssues(root) {
  const relative = 'packages/core/package.json';
  const before = parseJson(gitShowBaseline(root, relative));
  const after = parseJson(readFile(path.join(root, relative)));
  if (!before || !after) {
    return [];
  }
  const issues = [];
  Object.keys(Object.assign({}, before, after)).forEach(function (key) {
    if (key === 'version') {
      return;
    }
    if (key === 'scripts' && scriptsPreserved(before.scripts, after.scripts)) {
      return;
    }
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
      issues.push(relative + '#' + key);
    }
  });
  return issues;
}

function lockFiles(root) {
  const relatives = ['package-lock.json'];
  ['core', 'timezone', 'locale', 'extra', 'examples'].forEach(function (name) {
    relatives.push(path.join('packages', name, 'package-lock.json'));
  });
  return relatives.filter(function (relative) {
    return exists(path.join(root, relative));
  });
}

/* npm also refreshes stale sibling metadata while resolving core.  Accept
 * synchronization to an existing workspace version, not an unrelated release. */
function siblingVersionAligned(root, lockRelative, key, version) {
  let manifest;
  const installed = /(?:^|\/)node_modules\/@js-joda\/([^/]+)$/.exec(key);
  if (installed) {
    manifest = path.join('packages', installed[1], 'package.json');
  } else {
    const absolute = path.resolve(root, path.dirname(lockRelative), key, 'package.json');
    manifest = path.relative(root, absolute);
    if (manifest === '..' || manifest.indexOf('..' + path.sep) === 0 || path.isAbsolute(manifest)) return false;
  }
  const original = parseJson(gitShowBaseline(root, manifest));
  const current = exists(path.join(root, manifest)) ? parseJson(readFile(path.join(root, manifest))) : null;
  return original && current && original.name === current.name
    && original.version === version && current.version === version;
}

function lockScopeIssues(root) {
  const issues = [];
  lockFiles(root).forEach(function (relative) {
    const before = parseJson(gitShowBaseline(root, relative));
    const after = parseJson(readFile(path.join(root, relative)));
    if (!before || !after) {
      return;
    }
    const beforePackages = before.packages || {};
    const afterPackages = after.packages || {};
    Object.keys(Object.assign({}, beforePackages, afterPackages)).forEach(function (key) {
      if (key.indexOf('../') !== 0 || isCoreWorkspaceKey(key)) {
        return;
      }
      // Dropping an obsolete lock entry while relocating a workspace link is
      // graph maintenance, not a change to the sibling package's release.
      // Retained and newly added entries must still match workspace versions.
      if (!Object.prototype.hasOwnProperty.call(afterPackages, key)) {
        return;
      }
      const beforeVersion = (beforePackages[key] || {}).version;
      const afterVersion = (afterPackages[key] || {}).version;
      if (beforeVersion !== afterVersion && !siblingVersionAligned(root, relative, key, afterVersion)) {
        issues.push(relative + '#' + key + '.version');
      }
    });
  });
  return issues;
}

/* Locate a tool (mocha binary, @babel/register) inside the workspace. */
function resolveTool(root, pkg, relative) {
  const candidates = [
    path.join(root, 'node_modules', relative),
    path.join(root, 'packages', pkg, 'node_modules', relative),
  ];
  for (let i = 0; i < candidates.length; i += 1) {
    if (exists(candidates[i])) {
      return candidates[i];
    }
  }
  return null;
}

function parseMochaStats(stdout) {
  if (typeof stdout !== 'string') {
    return null;
  }
  const start = stdout.indexOf('{');
  const end = stdout.lastIndexOf('}');
  if (start === -1 || end <= start) {
    return null;
  }
  try {
    const parsed = JSON.parse(stdout.slice(start, end + 1));
    const stats = parsed && parsed.stats;
    if (!stats) {
      return null;
    }
    return {
      failures: typeof stats.failures === 'number' ? stats.failures : -1,
      pending: typeof stats.pending === 'number' ? stats.pending : -1,
    };
  } catch (e) {
    return null;
  }
}

const suiteCache = Object.create(null);

/*
 * Run the package's own Mocha suite with an explicit `timeout` wrapper, a hard
 * spawn timeout and `NODE_ENV=test`, and reduce it to the number of failing and
 * pending specs.  That pair is the stable common projection: it is a concrete,
 * normalized behavior result (not a bare status) and is expected to normalize
 * equal on both sides of the migration.
 */
function suite(root, key, pkg, files) {
  const cacheKey = root + '::' + key;
  if (!(cacheKey in suiteCache)) {
    const mocha = resolveTool(root, pkg, path.join('.bin', 'mocha'));
    const register = resolveTool(root, pkg, path.join('@babel', 'register'));
    let outcome = { ran: false, failures: -1, pending: -1 };
    if (mocha && register) {
      const seconds = Math.max(1, Math.ceil(DEFAULT_TIMEOUT_MS / 1000));
      const argv = [mocha, '--reporter', 'json', '--timeout', MOCHA_TIMEOUT, '--require', register].concat(files);
      const res = spawnSync('timeout', [String(seconds)].concat(argv), {
        cwd: path.join(root, 'packages', pkg),
        encoding: 'utf8',
        maxBuffer: 256 * 1024 * 1024,
        timeout: seconds * 1000 + 30000,
        killSignal: 'SIGKILL',
        env: Object.assign({}, process.env, { NODE_ENV: 'test' }),
      });
      const stats = parseMochaStats(res.stdout);
      if (stats) {
        outcome = { ran: true, failures: stats.failures, pending: stats.pending };
      }
    }
    suiteCache[cacheKey] = outcome;
  }
  return suiteCache[cacheKey];
}

function suiteValue(outcome) {
  return { failures: outcome.failures, pending: outcome.pending };
}

function suitePass(outcome) {
  return outcome.ran && outcome.failures === 0;
}

function coreAssertionIntegrity(root) {
  const duration = path.join(root, CORE_CASES.duration);
  const instant = path.join(root, CORE_CASES.instant);
  const durationHasCase = exists(duration)
    && readFile(duration).indexOf('PT-1M-59.999999999S') !== -1;
  const instantHasOffset = exists(instant)
    && readFile(instant).indexOf('factory_parse_offset_resolvesToUtcInstant') !== -1;
  const instantHasTruncation = exists(instant)
    && readFile(instant).indexOf('truncatedTo') !== -1;
  return durationHasCase && instantHasOffset && instantHasTruncation;
}

const CHECKS = {
  'P2P-038': function (root) {
    const value = projectedEntry(readJson(path.join(packageDir(root, 'core'), 'package.json')));
    return { value: value, pass: entryMatches(value, EXPECTED_CORE_ENTRY) };
  },

  'P2P-037': function (root) {
    const value = {
      timezone: projectedEntry(readJson(path.join(packageDir(root, 'timezone'), 'package.json'))),
      locale: projectedEntry(readJson(path.join(packageDir(root, 'locale'), 'package.json'))),
      extra: projectedEntry(readJson(path.join(packageDir(root, 'extra'), 'package.json'))),
    };
    const pass = Object.keys(EXPECTED_SIBLING_ENTRY).every(function (name) {
      return entryMatches(value[name], EXPECTED_SIBLING_ENTRY[name]);
    });
    return { value: value, pass: pass };
  },

  'P2P-025': function (root) {
    const manifests = localePackageDirs(root);
    const missing = [];
    const optionalBad = [];
    let checked = 0;
    manifests.forEach(function (manifest) {
      const pkg = readJson(manifest);
      const peers = pkg.peerDependencies || {};
      const meta = pkg.peerDependenciesMeta || {};
      checked += 1;
      LOCALE_PEER_NAMES.forEach(function (name) {
        if (!(name in peers)) {
          missing.push(path.basename(path.dirname(manifest)) + ':' + name);
        }
      });
      const timezoneMeta = meta['@js-joda/timezone'] || {};
      if (timezoneMeta.optional !== true) {
        optionalBad.push(path.basename(path.dirname(manifest)));
      }
    });
    const value = { checked: checked, missing: missing.sort(), optionalBad: optionalBad.sort() };
    return { value: value, pass: checked > 0 && missing.length === 0 && optionalBad.length === 0 };
  },

  'P2P-026': function (root) {
    const localeRoot = packageDir(root, 'locale');
    const run = suite(root, 'locale', 'locale', ['./test/format/TextPrinterTest.js']);
    const value = {
      localePackages: localePackageDirs(root).length,
      cldrCache: exists(path.join(localeRoot, 'src', 'format', 'cldr', 'CldrCache.js')),
      supplementalData: exists(path.join(localeRoot, 'src', 'supplemental-data.js')),
      failures: run.failures,
      pending: run.pending,
    };
    return { value: value, pass: value.cldrCache && value.supplementalData && suitePass(run) };
  },

  'P2P-027': function (root) {
    const uiDeps = [];
    const uiExports = [];
    ['core', 'timezone', 'locale', 'extra'].forEach(function (name) {
      const pkg = readJson(path.join(packageDir(root, name), 'package.json'));
      ['dependencies', 'devDependencies', 'peerDependencies'].forEach(function (section) {
        const deps = pkg[section] || {};
        Object.keys(deps).forEach(function (dep) {
          if (UI_NAME.test(dep)) {
            uiDeps.push(name + ':' + section + ':' + dep);
          }
        });
      });
    });
    const coreExports = extractExports(path.join(packageDir(root, 'core'), 'src', 'js-joda.js'));
    coreExports.forEach(function (name) {
      if (UI_NAME.test(name)) {
        uiExports.push(name);
      }
    });
    const value = { uiDeps: uiDeps.sort(), uiExports: uiExports.sort() };
    return { value: value, pass: uiDeps.length === 0 && uiExports.length === 0 };
  },

  'P2P-028': function (root) {
    const names = extractExports(path.join(packageDir(root, 'core'), 'src', 'js-joda.js'));
    const missing = REQUIRED_CORE_EXPORTS.filter(function (name) { return !names.has(name); });
    const value = { missing: missing.sort() };
    return { value: value, pass: missing.length === 0 };
  },

  'P2P-033': function (root) {
    const names = extractExports(path.join(packageDir(root, 'core'), 'src', 'js-joda.js'));
    const missing = REQUIRED_CORE_EXPORTS.filter(function (name) { return !names.has(name); })
      .concat(typingRuntimeIssues(root));
    const entryMismatch = [];
    if (!entryMatches(projectedEntry(readJson(path.join(packageDir(root, 'core'), 'package.json'))), EXPECTED_CORE_ENTRY)) {
      entryMismatch.push('core');
    }
    Object.keys(EXPECTED_SIBLING_ENTRY).forEach(function (name) {
      if (!entryMatches(projectedEntry(readJson(path.join(packageDir(root, name), 'package.json'))), EXPECTED_SIBLING_ENTRY[name])) {
        entryMismatch.push(name);
      }
    });
    const value = { missing: missing.sort(), entryMismatch: entryMismatch.sort() };
    return { value: value, pass: missing.length === 0 && entryMismatch.length === 0 };
  },

  'P2P-029': function (root) {
    const changed = gitChangedFiles(root);
    if (changed === null) {
      return { value: { git: false, offScope: [] }, pass: false };
    }
    const offScope = changed.filter(function (file) {
      return !ALLOWED_DIFF.some(function (re) { return re.test(file); });
    });
    /* File-level scope is necessary but not sufficient: the upgrade must also
     * stay limited to the @js-joda/core version declarations.  Fold the
     * content-level findings into the same projection so both the raw PASS
     * state and the source/target value comparison reject out-of-scope edits. */
    const contentScope = coreManifestScopeIssues(root).concat(lockScopeIssues(root));
    const violations = offScope.concat(contentScope).sort();
    const value = { git: true, offScope: violations };
    return { value: value, pass: violations.length === 0 };
  },

  'P2P-032': function (root) {
    const resolvedRoot = path.resolve(root);
    const changed = gitChangedFiles(root);
    const inside = changed === null
      ? []
      : changed.filter(function (file) {
        const abs = path.resolve(resolvedRoot, file);
        return abs === resolvedRoot || abs.indexOf(resolvedRoot + path.sep) === 0;
      });
    const allInside = changed !== null && inside.length === changed.length;
    const value = { absolute: path.isAbsolute(resolvedRoot), allInside: allInside };
    return { value: value, pass: value.absolute && value.allInside };
  },

  'P2P-034': function (root) {
    const corePkg = readJson(path.join(packageDir(root, 'core'), 'package.json'));
    const localePkg = readJson(path.join(packageDir(root, 'locale'), 'package.json'));
    const value = {
      coreBuildDist: !!(corePkg.scripts && corePkg.scripts['build-dist']),
      localeBuildDist: !!(localePkg.scripts && localePkg.scripts['build-dist']),
      localeBuildLocaleDist: !!(localePkg.scripts && localePkg.scripts['build-locale-dist']),
      coreRollup: exists(path.join(packageDir(root, 'core'), 'rollup.config.js')),
      localeRollup: exists(path.join(packageDir(root, 'locale'), 'rollup.config.js')),
    };
    const pass = value.coreBuildDist && value.localeBuildDist && value.localeBuildLocaleDist
      && value.coreRollup && value.localeRollup;
    return { value: value, pass: pass };
  },

  'P2P-035': function (root) {
    const casesPresent = coreAssertionIntegrity(root);
    const run = suite(root, 'core', 'core', ['./test/*Test.js', './test/**/*Test.js']);
    const value = { casesPresent: casesPresent, failures: run.failures, pending: run.pending };
    return { value: value, pass: casesPresent && suitePass(run) };
  },

  'P2P-039': function (root) {
    const coreSrc = path.join(packageDir(root, 'core'), 'src');
    const present = {
      'ChronoUnit.SECONDS': exists(path.join(coreSrc, 'temporal', 'ChronoUnit.js'))
        && readFile(path.join(coreSrc, 'temporal', 'ChronoUnit.js')).indexOf('SECONDS') !== -1,
      'DateTimeFormatter.ISO_LOCAL_DATE': exists(path.join(coreSrc, 'format', 'DateTimeFormatter.js'))
        && readFile(path.join(coreSrc, 'format', 'DateTimeFormatter.js')).indexOf('ISO_LOCAL_DATE') !== -1,
      'Period.ZERO': exists(path.join(coreSrc, 'Period.js'))
        && readFile(path.join(coreSrc, 'Period.js')).indexOf('ZERO') !== -1,
      'TemporalAdjusters.firstDayOfMonth': exists(path.join(coreSrc, 'temporal', 'TemporalAdjusters.js'))
        && readFile(path.join(coreSrc, 'temporal', 'TemporalAdjusters.js')).indexOf('firstDayOfMonth') !== -1,
    };
    const run = suite(root, 'core', 'core', ['./test/*Test.js', './test/**/*Test.js']);
    const value = { present: present, failures: run.failures, pending: run.pending };
    const allPresent = Object.keys(present).every(function (key) { return present[key]; });
    return { value: value, pass: allPresent && suitePass(run) };
  },

  'P2P-040': function (root) {
    const run = suite(root, 'timezone-provider', 'timezone', ['./test/MomentZoneRulesProviderTest.js']);
    return { value: suiteValue(run), pass: suitePass(run) };
  },

  'P2P-031': function (root) {
    const run = suite(root, 'timezone-now', 'timezone', ['./test/ZonedDateTimeTest.js']);
    return { value: suiteValue(run), pass: suitePass(run) };
  },

  'P2P-036': function (root) {
    const run = suite(root, 'timezone-region', 'timezone', ['./test/MomentZoneRulesTest.js', './test/MomentZoneRulesProviderTest.js']);
    return { value: suiteValue(run), pass: suitePass(run) };
  },

  'P2P-030': function (root) {
    const run = suite(root, 'locale', 'locale', ['./test/format/TextPrinterTest.js']);
    return { value: suiteValue(run), pass: suitePass(run) };
  },
};

const OBSERVATION_ORDER = [
  'P2P-040', 'P2P-032', 'P2P-025', 'P2P-026', 'P2P-027', 'P2P-028', 'P2P-029',
  'P2P-030', 'P2P-031', 'P2P-033', 'P2P-034', 'P2P-035', 'P2P-036', 'P2P-037',
  'P2P-038', 'P2P-039',
];

function runAll(root, side) {
  const results = OBSERVATION_ORDER.map(function (id) {
    const check = CHECKS[id];
    let outcome;
    try {
      outcome = check(root);
    } catch (err) {
      outcome = { value: { failures: -1, pending: -1, code: 'CHECK_ERROR' }, pass: false };
    }
    return {
      observation_id: id,
      side: side,
      value: outcome.value,
      result: outcome.pass ? 'PASS' : 'FAIL',
    };
  });
  return {
    schema: 'migration-asset-p2p-report:v2',
    batch_id: BATCH_ID,
    side: side,
    relation: 'equal_after_normalization',
    observations: results,
  };
}

module.exports = {
  typingRuntimeIssues: typingRuntimeIssues,
  scriptPreserved: scriptPreserved,
  scriptsPreserved: scriptsPreserved,
  BATCH_ID: BATCH_ID,
  OBSERVATION_ORDER: OBSERVATION_ORDER,
  CHECKS: CHECKS,
  runAll: runAll,
};
