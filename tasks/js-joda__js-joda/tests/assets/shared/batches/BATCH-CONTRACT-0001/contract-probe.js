'use strict';

/*
 * BATCH-CONTRACT-0001 shared public-contract probe.
 *
 * Executes the CONTRACT observations against a js-joda monorepo checkout and
 * emits, per observation, the raw normalized value observed on that side. The
 * integration layer runs the same probe on the source and on the reference
 * target and compares the normalized values (they must be equal) while both
 * sides must reach status PASS.
 *
 * The probe never blocks: runtime observation is synchronous and every
 * potentially long-running external command is executed through `timeout`.
 */

const fs = require('fs');
const path = require('path');
const Module = require('module');
const { spawnSync } = require('child_process');

const SIDE = { SOURCE: 'source', TARGET: 'target' };

const OBSERVATION_IDS = Object.freeze([
  'CONTRACT-001', 'CONTRACT-002', 'CONTRACT-003', 'CONTRACT-004', 'CONTRACT-005',
  'CONTRACT-006', 'CONTRACT-007', 'CONTRACT-008', 'CONTRACT-009', 'CONTRACT-010',
  'CONTRACT-011', 'CONTRACT-012', 'CONTRACT-013', 'CONTRACT-014', 'CONTRACT-015',
  'CONTRACT-016', 'CONTRACT-017', 'CONTRACT-018', 'CONTRACT-019', 'CONTRACT-020',
  'CONTRACT-021',
]);

const CORE_BASELINE_EXPORTS = Object.freeze([
  'ArithmeticException', 'DateTimeException', 'DateTimeParseException',
  'IllegalArgumentException', 'IllegalStateException',
  'UnsupportedTemporalTypeException', 'NullPointerException',
  'Clock', 'DayOfWeek', 'Duration', 'Instant', 'LocalDate', 'LocalTime',
  'LocalDateTime', 'OffsetTime', 'OffsetDateTime', 'Month', 'MonthDay',
  'ParsePosition', 'Period', 'Year', 'YearConstants', 'YearMonth',
  'ZonedDateTime', 'ZoneOffset', 'ZoneId', 'ZoneRegion',
  'ZoneOffsetTransition', 'ZoneRules', 'ZoneRulesProvider',
  'ChronoField', 'ChronoUnit', 'IsoFields', 'Temporal', 'TemporalAccessor',
  'TemporalAdjuster', 'TemporalAdjusters', 'TemporalAmount', 'TemporalField',
  'TemporalQueries', 'TemporalQuery', 'TemporalUnit', 'ValueRange',
  'DateTimeFormatter', 'DateTimeFormatterBuilder', 'DecimalStyle',
  'ResolverStyle', 'SignStyle', 'TextStyle', 'convert', 'nativeJs', 'use',
]);

const EXPECTED_ENTRY_FIELDS = Object.freeze({
  core: { main: 'dist/js-joda.js', module: 'dist/js-joda.esm.js', typings: 'typings/js-joda.d.ts' },
  timezone: { main: 'dist/js-joda-timezone.js', module: 'dist/js-joda-timezone.esm.js', typings: 'typings/js-joda-timezone.d.ts' },
  locale: { main: 'dist/js-joda-locale.js', module: 'dist/js-joda-locale.esm.js', typings: 'typings/js-joda-locale.d.ts' },
  extra: { main: 'dist/js-joda-extra.js', module: 'dist/js-joda-extra.esm.js', typings: 'typings/js-joda-extra.d.ts' },
});

const UI_EXPORT_NAMES = Object.freeze(['render', 'mount', 'createRoot', 'Component', 'Widget', 'React']);

function corePkgPath(repoRoot) {
  return path.join(repoRoot, 'packages', 'core', 'package.json');
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function readTextIfExists(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (e) {
    return null;
  }
}

function addNodeModulesPath(repoRoot) {
  const candidates = [
    path.join(repoRoot, 'node_modules'),
    path.join(repoRoot, '..', 'node_modules'),
  ];
  const parts = (process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean);
  let changed = false;
  candidates.forEach((candidate) => {
    if (fs.existsSync(candidate) && !parts.includes(candidate)) {
      parts.push(candidate);
      changed = true;
    }
  });
  if (changed) {
    process.env.NODE_PATH = parts.join(path.delimiter);
    Module._initPaths();
  }
}

function resolveFromRepo(request, repoRoot) {
  const paths = [path.join(repoRoot, 'node_modules'), repoRoot, __dirname];
  return require.resolve(request, { paths });
}

function loadRuntime(repoRoot) {
  const srcMap = {
    '@js-joda/core': path.join(repoRoot, 'packages', 'core', 'src', 'js-joda.js'),
    '@js-joda/timezone': path.join(repoRoot, 'packages', 'timezone', 'src', 'js-joda-timezone.js'),
    '@js-joda/locale': path.join(repoRoot, 'packages', 'locale', 'src', 'js-joda-locale.js'),
    '@js-joda/extra': path.join(repoRoot, 'packages', 'extra', 'src', 'js-joda-extra.js'),
  };

  addNodeModulesPath(repoRoot);

  let babelRegistered = false;
  try {
    const registerModule = require(resolveFromRepo('@babel/register', repoRoot));
    const presetModule = require(resolveFromRepo('@babel/preset-env', repoRoot));
    const register = registerModule && registerModule.default ? registerModule.default : registerModule;
    const presetEnv = presetModule && presetModule.default ? presetModule.default : presetModule;
    register({
      babelrc: false,
      configFile: false,
      presets: [[presetEnv, { targets: { node: 'current' }, modules: 'commonjs' }]],
      ignore: [/node_modules/],
      extensions: ['.js'],
    });
    babelRegistered = true;
  } catch (e) {
    babelRegistered = false;
  }

  const originalResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, parent, isMain, options) {
    if (Object.prototype.hasOwnProperty.call(srcMap, request) && fs.existsSync(srcMap[request])) {
      return srcMap[request];
    }
    return originalResolve.call(this, request, parent, isMain, options);
  };

  const runtime = { babelRegistered, modules: {}, errors: {} };
  [['core', '@js-joda/core'], ['timezone', '@js-joda/timezone'], ['extra', '@js-joda/extra'], ['locale', '@js-joda/locale']]
    .forEach(([name, request]) => {
      try {
        runtime.modules[name] = require(request);
      } catch (e) {
        runtime.modules[name] = null;
        runtime.errors[name] = String((e && e.message) || e);
      }
    });
  return runtime;
}

function boundedCommand(command, args, options, timeoutSeconds) {
  return spawnSync('timeout', [String(timeoutSeconds), command].concat(args), Object.assign({
    encoding: 'utf8',
    timeout: (timeoutSeconds + 5) * 1000,
  }, options || {}));
}

function gitChangedPaths(repoRoot) {
  if (!fs.existsSync(path.join(repoRoot, '.git'))) {
    return { observed: false, paths: [] };
  }
  const result = boundedCommand('git', ['-C', repoRoot, 'status', '--porcelain'], {}, 60);
  if (result.status !== 0 || typeof result.stdout !== 'string') {
    return { observed: false, paths: [] };
  }
  const paths = result.stdout
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.replace(/^[A-Z?!]{1,2}\s+/, '').replace(/^"|"$/g, '').trim());
  return { observed: true, paths };
}

function isAllowedChangedPath(relativePath) {
  // Source modules are frozen, even when a file is a package entry or a typing.
  if (/^packages\/[^/]+\/src(?:\/|$)/.test(relativePath)) return false;
  return /^(package\.json|package-lock\.json|lerna\.json)$/.test(relativePath)
    || /^packages\/[^/]+\/(package\.json|package-lock\.json|.+\.d\.ts)$/.test(relativePath)
    || /^packages\/[^/]+\/(dist|typings)\//.test(relativePath)
    || /^packages\/locale\/packages\/[^/]+\/package\.json$/.test(relativePath);
}

function registerCldr(locale, localeModule, repoRoot) {
  if (!localeModule || typeof localeModule.registerLocaleData !== 'function') {
    return false;
  }
  try {
    const base = path.join(repoRoot, 'node_modules', 'cldr-data', 'main', locale);
    const gregorian = path.join(base, 'ca-gregorian.json');
    const timeZoneNames = path.join(base, 'timeZoneNames.json');
    if (fs.existsSync(gregorian)) {
      localeModule.registerLocaleData(`main/${locale}/ca-gregorian.json`, JSON.parse(fs.readFileSync(gregorian, 'utf8')));
    }
    if (fs.existsSync(timeZoneNames)) {
      localeModule.registerLocaleData(`main/${locale}/timeZoneNames.json`, JSON.parse(fs.readFileSync(timeZoneNames, 'utf8')));
    }
    return true;
  } catch (e) {
    return false;
  }
}

function entryFieldsFor(repoRoot, pkg) {
  try {
    const manifest = readJson(path.join(repoRoot, 'packages', pkg, 'package.json'));
    return { main: manifest.main, module: manifest.module, typings: manifest.typings };
  } catch (e) {
    return null;
  }
}

function allEqual(actual, expected) {
  return Object.keys(expected).every((key) => actual && actual[key] === expected[key]);
}

// --- observation computations -------------------------------------------------

function observeCoreEntryPoints(ctx) {
  const value = entryFieldsFor(ctx.repoRoot, 'core');
  return { status: value && allEqual(value, EXPECTED_ENTRY_FIELDS.core) ? 'PASS' : 'FAIL', value };
}

function observeCoreExports(ctx) {
  const core = ctx.runtime.modules.core;
  if (!core) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.core || 'core module unavailable' };
  }
  const names = Object.keys(core).sort();
  const missing = CORE_BASELINE_EXPORTS.filter((name) => !(name in core));
  return { status: missing.length === 0 ? 'PASS' : 'FAIL', value: { exportCount: names.length, missing } };
}

function observeAuxEntryPoints(ctx) {
  const value = {
    timezone: entryFieldsFor(ctx.repoRoot, 'timezone'),
    locale: entryFieldsFor(ctx.repoRoot, 'locale'),
    extra: entryFieldsFor(ctx.repoRoot, 'extra'),
  };
  const ok = allEqual(value.timezone, EXPECTED_ENTRY_FIELDS.timezone)
    && allEqual(value.locale, EXPECTED_ENTRY_FIELDS.locale)
    && allEqual(value.extra, EXPECTED_ENTRY_FIELDS.extra);
  return { status: ok ? 'PASS' : 'FAIL', value };
}

function observeLocaleExports(ctx) {
  const locale = ctx.runtime.modules.locale;
  if (!locale) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.locale || 'locale module unavailable' };
  }
  let callable = false;
  try {
    locale.registerLocaleData('main/contract-probe-probe/ca-gregorian.json', {});
    callable = true;
  } catch (e) {
    callable = false;
  }
  const value = {
    hasLocale: typeof locale.Locale === 'function',
    hasWeekFields: typeof locale.WeekFields === 'function',
    hasRegisterLocaleData: typeof locale.registerLocaleData === 'function',
    registerCallable: callable,
  };
  const ok = value.hasLocale && value.hasWeekFields && value.hasRegisterLocaleData && value.registerCallable;
  return { status: ok ? 'PASS' : 'FAIL', value };
}

function observePrebuiltLocalePeers(ctx) {
  const base = path.join(ctx.repoRoot, 'packages', 'locale', 'packages');
  let dirs = [];
  try {
    dirs = fs.readdirSync(base).filter((name) => {
      try {
        return fs.statSync(path.join(base, name)).isDirectory();
      } catch (e) {
        return false;
      }
    }).sort();
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
  const ok = [];
  const bad = [];
  dirs.forEach((name) => {
    try {
      const manifest = readJson(path.join(base, name, 'package.json'));
      const peers = manifest.peerDependencies || {};
      const peerMeta = manifest.peerDependenciesMeta || {};
      const timezoneMeta = peerMeta['@js-joda/timezone'] || {};
      if (peers['@js-joda/core'] && peers['@js-joda/locale'] && peers['@js-joda/timezone']
        && timezoneMeta.optional === true) {
        ok.push(name);
      } else {
        bad.push(name);
      }
    } catch (e) {
      bad.push(name);
    }
  });
  const value = { localeCount: dirs.length, compliant: ok.length, nonCompliant: bad };
  return { status: bad.length === 0 && dirs.length > 0 ? 'PASS' : 'FAIL', value };
}

function observeTimezoneRegistration(ctx) {
  const core = ctx.runtime.modules.core;
  const timezone = ctx.runtime.modules.timezone;
  if (!core || !timezone) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.timezone || 'timezone module unavailable' };
  }
  try {
    const zoneId = core.ZoneId.of('Europe/Berlin');
    const value = {
      id: zoneId.id(),
      summerOffset: zoneId.rules().offset(core.Instant.parse('2016-06-21T12:00:00Z')).toString(),
      winterOffset: zoneId.rules().offset(core.Instant.parse('2016-12-21T12:00:00Z')).toString(),
    };
    return { status: 'PASS', value };
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
}

function observeImmutability(ctx) {
  const core = ctx.runtime.modules.core;
  if (!core) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.core || 'core module unavailable' };
  }
  try {
    const localDate = core.LocalDate.of(2020, 1, 1);
    const localDateBefore = localDate.toString();
    const localDateNext = localDate.plusDays(1);
    const instant = core.Instant.parse('2020-01-01T00:00:00Z');
    const instantBefore = instant.toString();
    const instantNext = instant.plusSeconds(1);
    const zoned = core.ZonedDateTime.parse('2020-01-01T00:00:00Z');
    const zonedBefore = zoned.toString();
    const zonedNext = zoned.plusDays(1);
    const value = {
      localDateNewInstance: localDateNext !== localDate,
      localDateUnchanged: localDate.toString() === localDateBefore,
      instantNewInstance: instantNext !== instant,
      instantUnchanged: instant.toString() === instantBefore,
      zonedNewInstance: zonedNext !== zoned,
      zonedUnchanged: zoned.toString() === zonedBefore,
    };
    const ok = Object.keys(value).every((key) => value[key] === true);
    return { status: ok ? 'PASS' : 'FAIL', value };
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
}

function observeZonedNow(ctx) {
  const core = ctx.runtime.modules.core;
  const timezone = ctx.runtime.modules.timezone;
  if (!core || !timezone) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.timezone || 'timezone module unavailable' };
  }
  try {
    const rendered = core.ZonedDateTime.now().toString();
    const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})(?:\[[^\]]+\])?$/.test(rendered);
    let roundTrip = false;
    try {
      core.ZonedDateTime.parse(rendered);
      roundTrip = true;
    } catch (e) {
      roundTrip = false;
    }
    const value = { hasOffset, roundTrip };
    return { status: hasOffset && roundTrip ? 'PASS' : 'FAIL', value };
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
}

function observeInterval(ctx) {
  const core = ctx.runtime.modules.core;
  const extra = ctx.runtime.modules.extra;
  if (!core || !extra || typeof extra.Interval !== 'function') {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.extra || 'extra module unavailable' };
  }
  try {
    const startDate = core.LocalDate.of(2020, 1, 1);
    const endDate = core.LocalDate.of(2020, 2, 1);
    const start = startDate.atStartOfDay(core.ZoneOffset.UTC).toInstant();
    const end = endDate.atStartOfDay(core.ZoneOffset.UTC).toInstant();
    const mid = core.LocalDate.of(2020, 1, 15).atStartOfDay(core.ZoneOffset.UTC).toInstant();
    const interval = extra.Interval.of(start, end);
    const value = {
      toString: interval.toString(),
      containsMid: interval.contains(mid),
      containsEnd: interval.contains(end),
    };
    const ok = value.toString === '2020-01-01T00:00:00Z/2020-02-01T00:00:00Z'
      && value.containsMid === true && value.containsEnd === false;
    return { status: ok ? 'PASS' : 'FAIL', value };
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
}

function observeLocaleFormatting(ctx) {
  const core = ctx.runtime.modules.core;
  const locale = ctx.runtime.modules.locale;
  if (!core || !locale || typeof locale.Locale !== 'function') {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.locale || 'locale module unavailable' };
  }
  try {
    registerCldr('en', locale, ctx.repoRoot);
    registerCldr('de', locale, ctx.repoRoot);
    const format = (targetLocale) => core.DateTimeFormatter.ofPattern('MMMM')
      .withLocale(targetLocale)
      .format(core.LocalDate.of(2020, 1, 15));
    const value = {
      'en-US': format(locale.Locale.US),
      'de-DE': format(locale.Locale.GERMANY),
    };
    const ok = value['en-US'] === 'January' && value['de-DE'] === 'Januar';
    return { status: ok ? 'PASS' : 'FAIL', value };
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
}

function observeCoreCommands(ctx) {
  let manifest = {};
  try {
    manifest = readJson(corePkgPath(ctx.repoRoot));
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
  const scripts = manifest.scripts || {};
  const required = ['test', 'test-ts-definitions', 'test-browser', 'lint'];
  const baselineResult = boundedCommand('git', ['-C', ctx.repoRoot, 'show', 'HEAD:packages/core/package.json'], {}, 60);
  let baseline = {};
  try {
    baseline = JSON.parse(baselineResult.stdout).scripts || {};
  } catch (e) {
    return { status: 'FAIL', value: null, error: 'baseline core commands unavailable' };
  }
  const { scriptPreserved } = require('../BATCH-P2P-0002/p2p-checks');
  const value = {};
  let ok = true;
  required.forEach((name) => {
    const preserved = scriptPreserved(name, baseline[name], scripts[name], baseline);
    ok = ok && preserved;
    // Project equivalent commands to the same baseline capability, while
    // retaining FAIL and the actual command for weakened or removed checks.
    value[name] = preserved ? baseline[name] : (scripts[name] || null);
  });
  return { status: ok ? 'PASS' : 'FAIL', value };
}

function observeBuildScripts(ctx) {
  const readScripts = (pkg) => {
    try {
      return readJson(path.join(ctx.repoRoot, 'packages', pkg, 'package.json')).scripts || {};
    } catch (e) {
      return {};
    }
  };
  const coreScripts = readScripts('core');
  const timezoneScripts = readScripts('timezone');
  const localeScripts = readScripts('locale');
  const extraScripts = readScripts('extra');
  const value = {
    coreBuildDist: typeof coreScripts['build-dist'] === 'string',
    timezoneBuildDist: typeof timezoneScripts['build-dist'] === 'string',
    localeBuildDist: typeof localeScripts['build-dist'] === 'string',
    localeBuildLocaleDist: typeof localeScripts['build-locale-dist'] === 'string',
    extraBuildDist: typeof extraScripts['build-dist'] === 'string',
  };
  const ok = Object.keys(value).every((key) => value[key] === true);
  return { status: ok ? 'PASS' : 'FAIL', value };
}

function observeCapabilities(ctx) {
  const core = ctx.runtime.modules.core;
  if (!core) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.core || 'core module unavailable' };
  }
  let firstDay = null;
  try {
    firstDay = typeof core.TemporalAdjusters.firstDayOfMonth === 'function'
      ? core.TemporalAdjusters.firstDayOfMonth() : null;
  } catch (e) {
    firstDay = null;
  }
  const value = {
    chronoUnitSeconds: core.ChronoUnit && core.ChronoUnit.SECONDS != null,
    isoLocalDate: core.DateTimeFormatter && core.DateTimeFormatter.ISO_LOCAL_DATE != null,
    periodZero: core.Period && core.Period.ZERO != null,
    firstDayOfMonth: firstDay != null,
  };
  const ok = Object.keys(value).every((key) => value[key] === true);
  return { status: ok ? 'PASS' : 'FAIL', value };
}

function observeExportSuperset(ctx) {
  const core = ctx.runtime.modules.core;
  if (!core) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.core || 'core module unavailable' };
  }
  const missing = CORE_BASELINE_EXPORTS.filter((name) => !(name in core));
  const entryFields = entryFieldsFor(ctx.repoRoot, 'core');
  const entryFieldsUnchanged = allEqual(entryFields, EXPECTED_ENTRY_FIELDS.core);
  const value = { missing, entryFieldsUnchanged };
  return { status: missing.length === 0 && entryFieldsUnchanged ? 'PASS' : 'FAIL', value };
}

function observeParseFormat(ctx) {
  const core = ctx.runtime.modules.core;
  if (!core) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.core || 'core module unavailable' };
  }
  try {
    const value = {
      localTime: core.LocalTime.parse('12:30').toString(),
      yearMonth: core.YearMonth.parse('2020-06').toString(),
      plusDaysAcrossLeap: core.LocalDate.parse('2020-02-29').plusDays(1).toString(),
      positiveDuration: core.Duration.ofSeconds(90).toString(),
    };
    const ok = value.localTime === '12:30' && value.yearMonth === '2020-06'
      && value.plusDaysAcrossLeap === '2020-03-01' && value.positiveDuration === 'PT1M30S';
    return { status: ok ? 'PASS' : 'FAIL', value };
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
}

function observeChangeScope(ctx) {
  const changed = gitChangedPaths(ctx.repoRoot);
  const disallowed = changed.paths.filter((relativePath) => !isAllowedChangedPath(relativePath)).sort();
  const value = { disallowed };
  return { status: disallowed.length === 0 ? 'PASS' : 'FAIL', value };
}

function observeTimezoneDataIntegrity(ctx) {
  const changed = gitChangedPaths(ctx.repoRoot);
  const packedChanged = changed.paths.filter((relativePath) => relativePath.startsWith('packages/timezone/data/packed/')).sort();
  const value = { changedPackedFiles: packedChanged };
  return { status: packedChanged.length === 0 ? 'PASS' : 'FAIL', value };
}

function observeLocaleDataSemantics(ctx) {
  const core = ctx.runtime.modules.core;
  const locale = ctx.runtime.modules.locale;
  if (!core || !locale || typeof locale.Locale !== 'function') {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.locale || 'locale module unavailable' };
  }
  try {
    registerCldr('en', locale, ctx.repoRoot);
    registerCldr('de', locale, ctx.repoRoot);
    const format = (targetLocale) => core.DateTimeFormatter.ofPattern('MMMM')
      .withLocale(targetLocale)
      .format(core.LocalDate.of(2020, 1, 15));
    const value = { 'en-US': format(locale.Locale.US), 'de-DE': format(locale.Locale.GERMANY) };
    const ok = value['en-US'] === 'January' && value['de-DE'] === 'Januar';
    return { status: ok ? 'PASS' : 'FAIL', value };
  } catch (e) {
    return { status: 'FAIL', value: null, error: String(e.message || e) };
  }
}

function observeNoUiSurface(ctx) {
  const core = ctx.runtime.modules.core;
  if (!core) {
    return { status: 'FAIL', value: null, error: ctx.runtime.errors.core || 'core module unavailable' };
  }
  const uiExports = Object.keys(core).filter((name) => UI_EXPORT_NAMES.indexOf(name) !== -1);
  const value = { uiExports };
  return { status: uiExports.length === 0 ? 'PASS' : 'FAIL', value };
}

function observeTestIntegrity(ctx) {
  const durationTest = readTextIfExists(path.join(ctx.repoRoot, 'packages/core/test/reference/DurationTest.js'));
  const instantTest = readTextIfExists(path.join(ctx.repoRoot, 'packages/core/test/reference/InstantTest.js'));
  const value = {
    durationNegativeBoundary: durationTest !== null && durationTest.indexOf('PT-1M-59.999999999S') !== -1,
    instantOffsetParse: instantTest !== null && instantTest.indexOf('2024-03-07T15:30:00+08:00') !== -1,
  };
  const ok = value.durationNegativeBoundary && value.instantOffsetParse;
  return { status: ok ? 'PASS' : 'FAIL', value };
}

function observeChangeLocation(ctx) {
  const changed = gitChangedPaths(ctx.repoRoot);
  const outside = changed.paths.filter((relativePath) => relativePath.startsWith('/') || relativePath.indexOf('..') !== -1).sort();
  const value = { outsideRepo: outside };
  return { status: outside.length === 0 ? 'PASS' : 'FAIL', value };
}

const OBSERVERS = {
  'CONTRACT-001': observeCoreEntryPoints,
  'CONTRACT-002': observeCoreExports,
  'CONTRACT-003': observeAuxEntryPoints,
  'CONTRACT-004': observeLocaleExports,
  'CONTRACT-005': observePrebuiltLocalePeers,
  'CONTRACT-006': observeTimezoneRegistration,
  'CONTRACT-007': observeImmutability,
  'CONTRACT-008': observeZonedNow,
  'CONTRACT-009': observeInterval,
  'CONTRACT-010': observeLocaleFormatting,
  'CONTRACT-011': observeCoreCommands,
  'CONTRACT-012': observeBuildScripts,
  'CONTRACT-013': observeCapabilities,
  'CONTRACT-014': observeExportSuperset,
  'CONTRACT-015': observeParseFormat,
  'CONTRACT-016': observeChangeScope,
  'CONTRACT-017': observeTimezoneDataIntegrity,
  'CONTRACT-018': observeLocaleDataSemantics,
  'CONTRACT-019': observeNoUiSurface,
  'CONTRACT-020': observeTestIntegrity,
  'CONTRACT-021': observeChangeLocation,
};

function runProbe(side, repoRoot) {
  const ctx = { side, repoRoot, runtime: loadRuntime(repoRoot) };
  const results = {};
  OBSERVATION_IDS.forEach((observationId) => {
    try {
      results[observationId] = OBSERVERS[observationId](ctx);
    } catch (e) {
      results[observationId] = { status: 'FAIL', value: null, error: String((e && e.message) || e) };
    }
  });
  const status = OBSERVATION_IDS.every((observationId) => results[observationId].status === 'PASS') ? 'PASS' : 'FAIL';
  return {
    schema_version: 'migration-contract-probe:v1',
    batch_id: 'BATCH-CONTRACT-0001',
    side,
    repo_root: repoRoot,
    status,
    results,
  };
}

function main(argv) {
  const side = argv[2];
  const repoRoot = path.resolve(argv[3] || process.cwd());
  const outFile = argv[4];
  if (side !== SIDE.SOURCE && side !== SIDE.TARGET) {
    process.stderr.write('usage: contract-probe.js <source|target> <repoRoot> [outFile]\n');
    return 2;
  }
  const report = runProbe(side, repoRoot);
  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  if (outFile) {
    fs.writeFileSync(outFile, serialized);
  } else {
    process.stdout.write(serialized);
  }
  return report.status === 'PASS' ? 0 : 1;
}

module.exports = { OBSERVATION_IDS, runProbe, main };

if (require.main === module) {
  process.exitCode = main(process.argv);
}
