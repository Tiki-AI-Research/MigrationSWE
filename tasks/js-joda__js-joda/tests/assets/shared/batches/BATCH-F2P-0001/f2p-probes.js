'use strict';

const fs = require('fs');
const path = require('path');

const TARGET_VERSION = '6.3.2';
const SOURCE_VERSION = '6.1.0';

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

const OBSERVATION_REQUIREMENTS = {
  'F2P-001': ['R038'],
  'F2P-002': ['R022'],
  'F2P-003': ['R034'],
  'F2P-004': ['R036'],
  'F2P-005': ['R014'],
  'F2P-006': ['R026'],
  'F2P-007': ['R002'],
  'F2P-008': ['R027'],
  'F2P-009': ['R007'],
  'F2P-010': ['R003'],
  'F2P-011': ['R002'],
  'F2P-012': ['R039'],
  'F2P-013': ['R002'],
  'F2P-014': ['R035'],
  'F2P-015': ['R035'],
  'F2P-016': ['R002'],
  'F2P-017': ['R027'],
  'F2P-018': ['R006'],
  'F2P-019': ['R035'],
  'F2P-020': ['R037'],
  'F2P-021': ['R037'],
  'F2P-022': ['R027'],
  'F2P-023': ['R001'],
  'F2P-024': ['R041'],
};

const OBSERVATION_ORDER = [
  'F2P-001',
  'F2P-002',
  'F2P-003',
  'F2P-004',
  'F2P-005',
  'F2P-006',
  'F2P-007',
  'F2P-008',
  'F2P-009',
  'F2P-010',
  'F2P-012',
  'F2P-014',
  'F2P-018',
  'F2P-020',
  'F2P-023',
  'F2P-024',
  'F2P-011',
  'F2P-013',
  'F2P-015',
  'F2P-016',
  'F2P-019',
  'F2P-021',
  'F2P-022',
  'F2P-017',
];

const PLACEHOLDER_REPO = /^\{.*\}$/;

function readJson(absPath) {
  return JSON.parse(fs.readFileSync(absPath, 'utf8'));
}

function attempt(fn) {
  try {
    return { ok: true, value: fn() };
  } catch (error) {
    return { ok: false, error };
  }
}

function isModuleNotFound(error) {
  return (
    error &&
    (error.code === 'MODULE_NOT_FOUND' || error.code === 'ERR_MODULE_NOT_FOUND')
  );
}

function loadFirst(candidates) {
  let lastError = null;
  for (const candidate of candidates) {
    const loaded = attempt(candidate);
    if (loaded.ok) {
      return loaded.value;
    }
    lastError = loaded.error;
    if (!isModuleNotFound(loaded.error)) {
      throw loaded.error;
    }
  }
  throw lastError || new Error('no candidate module could be loaded');
}

function createContext(repoRoot) {
  const root = path.resolve(repoRoot);
  const context = {
    repoRoot: root,
    readJson: (relative) => readJson(path.join(root, relative)),
    core: null,
    coreVersion: null,
    coreSource: null,
    modules: {},
    loadErrors: {},
  };

  const versionProbe = attempt(() => context.readJson('packages/core/package.json').version);
  if (versionProbe.ok) {
    context.coreVersion = versionProbe.value;
  }

  const coreProbe = attempt(() =>
    loadFirst([
      () => require('@js-joda/core'),
      () => require(path.join(root, 'packages/core/dist/js-joda.js')),
      () => require(path.join(root, 'packages/core/src/js-joda.js')),
    ])
  );
  if (coreProbe.ok) {
    context.core = coreProbe.value;
  } else {
    context.loadErrors.core = coreProbe.error;
  }

  return context;
}

function loadSibling(context, name, entry) {
  if (context.modules[name]) {
    return context.modules[name];
  }
  const probe = attempt(() =>
    loadFirst([
      () => require('@js-joda/' + name),
      () => require(path.join(context.repoRoot, 'packages', name, 'dist', entry)),
      () => require(path.join(context.repoRoot, 'packages', name, 'src', entry)),
    ])
  );
  if (probe.ok) {
    context.modules[name] = probe.value;
    return probe.value;
  }
  context.loadErrors[name] = probe.error;
  throw probe.error;
}

function loadTimezone(context) {
  return loadSibling(context, 'timezone', 'js-joda-timezone.js');
}

function loadExtra(context) {
  return loadSibling(context, 'extra', 'js-joda-extra.js');
}

function loadLocale(context) {
  const locale = loadSibling(context, 'locale', 'js-joda-locale.js');
  if (!context.localeDataRegistered) {
    const register = locale.registerLocaleData;
    for (const localeId of ['en', 'de']) {
      const gregorian = require('cldr-data/main/' + localeId + '/ca-gregorian.json');
      const timeZoneNames = require('cldr-data/main/' + localeId + '/timeZoneNames.json');
      register('main/' + localeId + '/ca-gregorian.json', gregorian);
      register('main/' + localeId + '/timeZoneNames.json', timeZoneNames);
    }
    context.localeDataRegistered = true;
  }
  return locale;
}

function targetVersionInEffect(context) {
  return context.coreVersion === TARGET_VERSION;
}

function manifestVersionInEffect(context, relative, expected) {
  const probe = attempt(() => context.readJson(relative).version);
  return probe.ok && probe.value === expected;
}

function dependencyPinInEffect(context, relative, section, dependency, expected) {
  const probe = attempt(() => context.readJson(relative)[section][dependency]);
  return probe.ok && probe.value === expected;
}

function coreFixes(context) {
  const core = context.core;
  const offsetParse =
    core.Instant.parse('2024-03-07T15:30:00+08:00').toString() ===
    '2024-03-07T07:30:00Z';
  const offsetParseZero =
    core.Instant.parse('2024-03-07T07:30:00-00:00').toString() ===
    '2024-03-07T07:30:00Z';
  const negativeDuration =
    core.Duration.ofSeconds(-120, 1).toString() === 'PT-1M-59.999999999S' &&
    core.Duration.ofSeconds(-60, 500000000).toString() === 'PT-59.5S' &&
    core.Duration.ofSeconds(-3601, 500000000).toString() === 'PT-1H-0.5S';
  const preEpochTruncation =
    core.Instant.ofEpochSecond(-1, -1000000)
      .truncatedTo(core.ChronoUnit.SECONDS)
      .equals(core.Instant.ofEpochSecond(-2)) &&
    core.Instant.ofEpochSecond(0, -1000000)
      .truncatedTo(core.ChronoUnit.SECONDS)
      .equals(core.Instant.ofEpochSecond(-1));
  const prefixedFixedZone =
    core.ZoneId.of('UTC+01:00').id() === 'UTC+01:00' &&
    core.ZoneId.of('UTC+01:00').rules().equals(core.ZoneOffset.ofHours(1).rules()) &&
    core.ZoneId.of('GMT+01:00').rules().equals(core.ZoneOffset.ofHours(1).rules());
  return { offsetParse, offsetParseZero, negativeDuration, preEpochTruncation, prefixedFixedZone };
}

function assertThrows(fn) {
  return attempt(fn).ok === false;
}

function excessDaysZero(context) {
  const core = context.core;
  const formatter = core.DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH").withResolverStyle(
    core.ResolverStyle.SMART
  );
  const text = '2016-06-03T24';
  const date = formatter.parse(text, core.LocalDate.FROM).toString();
  const excess = formatter.parse(text, core.DateTimeFormatter.PARSED_EXCESS_DAYS);
  return date === '2016-06-04' && excess != null && excess.equals(core.Period.ZERO);
}

const PROBES = {
  'F2P-001': (context) => {
    const manifest = 'packages/locale/packages/de/package.json';
    const probe = attempt(() => context.readJson(manifest));
    if (!probe.ok) {
      return false;
    }
    const manifestData = probe.value;
    const peers = manifestData.peerDependencies || {};
    const peersMeta = manifestData.peerDependenciesMeta || {};
    // Compatibility is a semver relation, not a required spelling of a range.
    const semver = require(require.resolve('semver', { paths: [context.repoRoot] }));
    const localeVersion = context.readJson('packages/locale/package.json').version;
    const keepsCorePeer = typeof peers['@js-joda/core'] === 'string'
      && semver.satisfies(TARGET_VERSION, peers['@js-joda/core']);
    const alignedLocalePeer = typeof peers['@js-joda/locale'] === 'string'
      && semver.satisfies(localeVersion, peers['@js-joda/locale']);
    const keepsOptionalTimezone =
      typeof peers['@js-joda/timezone'] === 'string' &&
      peersMeta['@js-joda/timezone'] &&
      peersMeta['@js-joda/timezone'].optional === true;
    return (
      targetVersionInEffect(context) &&
      keepsCorePeer &&
      alignedLocalePeer &&
      keepsOptionalTimezone
    );
  },
  'F2P-002': (context) => {
    const fixes = coreFixes(context);
    return (
      targetVersionInEffect(context) &&
      fixes.offsetParse &&
      fixes.negativeDuration &&
      fixes.preEpochTruncation &&
      fixes.prefixedFixedZone &&
      excessDaysZero(context)
    );
  },
  'F2P-003': (context) => {
    const lock = attempt(() => context.readJson('packages/examples/package-lock.json'));
    if (!lock.ok) {
      return false;
    }
    const peerRange = attempt(
      () => context.readJson('packages/examples/package.json').peerDependencies['@js-joda/core']
    );
    return (
      targetVersionInEffect(context) &&
      peerRange.ok &&
      peerRange.value === '>=5.7.0' &&
      JSON.stringify(lock.value).indexOf('@js-joda/core') !== -1
    );
  },
  'F2P-004': (context) => {
    loadTimezone(context);
    const { ZoneId, Instant, ZoneOffset } = context.core;
    const summer = ZoneId.of('America/New_York')
      .rules()
      .offset(Instant.parse('2016-06-21T00:00:00Z'))
      .equals(ZoneOffset.ofHours(-4));
    const winter = ZoneId.of('America/New_York')
      .rules()
      .offset(Instant.parse('2016-12-21T00:00:00Z'))
      .equals(ZoneOffset.ofHours(-5));
    return targetVersionInEffect(context) && summer && winter;
  },
  'F2P-005': (context) => {
    const manifest = attempt(() => context.readJson('packages/core/package.json'));
    if (!manifest.ok) {
      return false;
    }
    return (
      manifestVersionInEffect(context, 'packages/core/package.json', TARGET_VERSION) &&
      manifest.value.main === 'dist/js-joda.js' &&
      manifest.value.module === 'dist/js-joda.esm.js' &&
      manifest.value.typings === 'typings/js-joda.d.ts'
    );
  },
  'F2P-006': (context) => {
    const fixes = coreFixes(context);
    const timezoneManifest = attempt(() => context.readJson('packages/timezone/package.json'));
    return (
      targetVersionInEffect(context) &&
      fixes.offsetParse &&
      timezoneManifest.ok &&
      timezoneManifest.value.main === 'dist/js-joda-timezone.js'
    );
  },
  'F2P-007': (context) => {
    const manifest = attempt(() => context.readJson('packages/extra/package.json'));
    if (!manifest.ok) {
      return false;
    }
    const peerRange = (manifest.value.peerDependencies || {})['@js-joda/core'];
    return targetVersionInEffect(context) && peerRange === '>=5.7.0';
  },
  'F2P-008': (context) => {
    const lock = attempt(() => context.readJson('packages/core/package-lock.json'));
    const fixes = coreFixes(context);
    return (
      targetVersionInEffect(context) &&
      lock.ok &&
      fixes.offsetParse
    );
  },
  'F2P-009': (context) => {
    const { ZoneId, ZoneOffset } = context.core;
    const prefixed = ZoneId.of('UTC+01:00');
    return (
      targetVersionInEffect(context) &&
      prefixed.id() === 'UTC+01:00' &&
      prefixed.rules().equals(ZoneOffset.ofHours(1).rules())
    );
  },
  'F2P-010': (context) => {
    const fixes = coreFixes(context);
    return (
      targetVersionInEffect(context) &&
      fixes.offsetParse &&
      fixes.negativeDuration &&
      fixes.preEpochTruncation &&
      fixes.prefixedFixedZone &&
      excessDaysZero(context)
    );
  },
  'F2P-011': (context) => {
    const locale = loadLocale(context);
    const aligned = dependencyPinInEffect(
      context,
      'packages/locale/package.json',
      'devDependencies',
      '@js-joda/core',
      '^6.3.2'
    );
    return targetVersionInEffect(context) && aligned && typeof locale.registerLocaleData === 'function';
  },
  'F2P-012': (context) => {
    const duration = context.core.Duration.ofSeconds(-120, 1);
    return (
      targetVersionInEffect(context) &&
      typeof duration.toJSON === 'function' &&
      duration.toJSON() === 'PT-1M-59.999999999S' &&
      duration.toJSON() === duration.toString()
    );
  },
  'F2P-013': (context) => {
    const coreManifest = attempt(() => context.readJson('packages/core/package.json'));
    const timezoneManifest = attempt(() => context.readJson('packages/timezone/package.json'));
    const localeManifest = attempt(() => context.readJson('packages/locale/package.json'));
    const extraManifest = attempt(() => context.readJson('packages/extra/package.json'));
    if (!coreManifest.ok || !timezoneManifest.ok || !localeManifest.ok || !extraManifest.ok) {
      return false;
    }
    return (
      targetVersionInEffect(context) &&
      timezoneManifest.value.main === 'dist/js-joda-timezone.js' &&
      timezoneManifest.value.module === 'dist/js-joda-timezone.esm.js' &&
      localeManifest.value.main === 'dist/js-joda-locale.js' &&
      localeManifest.value.module === 'dist/js-joda-locale.esm.js' &&
      extraManifest.value.main === 'dist/js-joda-extra.js' &&
      extraManifest.value.module === 'dist/js-joda-extra.esm.js'
    );
  },
  'F2P-014': (context) => {
    const core = context.core;
    const missing = REQUIRED_CORE_EXPORTS.filter((name) => typeof core[name] === 'undefined');
    const cjs = core.LocalDate;
    const parseOk = cjs.parse('2020-01-02').toString() === '2020-01-02';
    const manifest = attempt(() => context.readJson('packages/core/package.json'));
    return (
      targetVersionInEffect(context) &&
      missing.length === 0 &&
      parseOk &&
      manifest.ok &&
      manifest.value.main === 'dist/js-joda.js' &&
      manifest.value.module === 'dist/js-joda.esm.js'
    );
  },
  'F2P-015': (context) => {
    const core = context.core;
    const manifest = attempt(() => context.readJson('packages/core/package.json'));
    const pairingDeclared =
      manifest.ok &&
      manifest.value.main === 'dist/js-joda.js' &&
      manifest.value.module === 'dist/js-joda.esm.js';
    const parseOk = core.LocalDate.parse('2020-01-02').toString() === '2020-01-02';
    return targetVersionInEffect(context) && pairingDeclared && parseOk;
  },
  'F2P-016': (context) => {
    const lock = attempt(() => context.readJson('packages/locale/package-lock.json'));
    const aligned = dependencyPinInEffect(
      context,
      'packages/locale/package.json',
      'devDependencies',
      '@js-joda/core',
      '^6.3.2'
    );
    return (
      targetVersionInEffect(context) &&
      lock.ok &&
      aligned &&
      JSON.stringify(lock.value).indexOf('"version":"6.3.2"') !== -1
    );
  },
  'F2P-017': (context) => {
    const lock = attempt(() => context.readJson('packages/core/package-lock.json'));
    if (!lock.ok) {
      return false;
    }
    const rootPackage = lock.value.packages && lock.value.packages[''];
    return (
      targetVersionInEffect(context) &&
      lock.value.version === TARGET_VERSION &&
      rootPackage &&
      rootPackage.version === TARGET_VERSION
    );
  },
  'F2P-018': (context) => {
    const fixes = coreFixes(context);
    return targetVersionInEffect(context) && fixes.preEpochTruncation;
  },
  'F2P-019': (context) => {
    const manifest = attempt(() => context.readJson('packages/core/package.json'));
    if (!manifest.ok) {
      return false;
    }
    const scripts = manifest.value.scripts || {};
    return (
      targetVersionInEffect(context) &&
      scripts['build-dist'] === 'npx rollup -c rollup.config.js' &&
      manifest.value.main === 'dist/js-joda.js' &&
      context.core.LocalDate.parse('2020-01-02').toString() === '2020-01-02'
    );
  },
  'F2P-020': (context) => {
    const locale = loadLocale(context);
    const { DateTimeFormatter, LocalDate } = context.core;
    const english = DateTimeFormatter.ofPattern('MMMM')
      .withLocale(locale.Locale.US)
      .format(LocalDate.of(2020, 1, 15));
    const german = DateTimeFormatter.ofPattern('MMMM')
      .withLocale(locale.Locale.GERMANY)
      .format(LocalDate.of(2020, 1, 15));
    return (
      targetVersionInEffect(context) &&
      locale.Locale.US &&
      english === 'January' &&
      german === 'Januar'
    );
  },
  'F2P-021': (context) => {
    const locale = loadLocale(context);
    const { DateTimeFormatter, LocalDate } = context.core;
    const english = DateTimeFormatter.ofPattern('MMMM')
      .withLocale(locale.Locale.US)
      .format(LocalDate.of(2020, 1, 15));
    const german = DateTimeFormatter.ofPattern('MMMM')
      .withLocale(locale.Locale.GERMANY)
      .format(LocalDate.of(2020, 1, 15));
    const plug = attempt(() =>
      require(path.join(context.repoRoot, 'packages/locale/src/plug.js'))
    );
    return (
      targetVersionInEffect(context) &&
      plug.ok &&
      english === 'January' &&
      german === 'Januar'
    );
  },
  'F2P-022': (context) => {
    const lock = attempt(() => context.readJson('packages/core/package-lock.json'));
    const fixes = coreFixes(context);
    return (
      targetVersionInEffect(context) &&
      lock.ok &&
      lock.value.version === TARGET_VERSION &&
      fixes.offsetParse
    );
  },
  'F2P-023': (context) => {
    return manifestVersionInEffect(context, 'packages/core/package.json', TARGET_VERSION);
  },
  'F2P-024': (context) => {
    const { ZoneId } = context.core;
    const invalidShort = assertThrows(() => ZoneId.of('A'));
    const invalidSuffix = assertThrows(() => ZoneId.of('GMT+x'));
    const validPrefix =
      ZoneId.of('UTC+01:00').rules().equals(context.core.ZoneOffset.ofHours(1).rules()) &&
      ZoneId.of('GMT+01:00').rules().equals(context.core.ZoneOffset.ofHours(1).rules());
    return targetVersionInEffect(context) && invalidShort && invalidSuffix && validPrefix;
  },
};

function evaluateAll(repoRoot) {
  const context = createContext(repoRoot);
  return OBSERVATION_ORDER.map((observationId) => {
    const evaluation = attempt(() => PROBES[observationId](context));
    let status;
    let detail;
    if (evaluation.ok) {
      status = evaluation.value ? 'PASS' : 'FAIL';
      detail = { coreVersion: context.coreVersion };
    } else if (isModuleNotFound(evaluation.error)) {
      status = 'INFRA_ERROR';
      detail = { coreVersion: context.coreVersion, error: String(evaluation.error.message) };
    } else {
      status = 'FAIL';
      detail = { coreVersion: context.coreVersion, error: String(evaluation.error.message) };
    }
    return {
      observation_id: observationId,
      requirement_ids: OBSERVATION_REQUIREMENTS[observationId],
      status,
      detail,
    };
  });
}

module.exports = {
  TARGET_VERSION,
  SOURCE_VERSION,
  REQUIRED_CORE_EXPORTS,
  OBSERVATION_REQUIREMENTS,
  OBSERVATION_ORDER,
  createContext,
  evaluateAll,
};
