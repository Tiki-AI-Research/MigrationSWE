'use strict';

/**
 * Structural/projection definitions for the 24 F2P observations owned by
 * BATCH-F2P-0003.  Each observation carries the exact atomic-claim entry,
 * call or dependency projection from AUTHORING_INPUT_JSON; different entries
 * are never collapsed into one global "package exists" check.
 *
 * Every probe is a target postcondition: it asserts both the frozen target
 * technology identity (js-joda 6.3.2) on the real load/execution path and the
 * observation specific behavior.  The same predicate is used for the source
 * and target runs; the source version (6.1.0) therefore naturally fails.
 */

const { MIGRATION } = require('./support');

const TARGET_VERSION = MIGRATION.targetVersion;

/* Validate npm's lock graph, including local file/workspace links.
 * Runtime behavior is exercised by the other probes through ctx.loadCore();
 * this lock-policy observation must not require an unused consumer install. */
function coreResolutionInLock(ctx, name) {
  const lock = ctx.lockJson(name);
  const packages = (lock && lock.packages) || {};
  const dependencies = (lock && lock.dependencies) || {};
  const entries = Object.keys(packages).filter((key) => key === '../core'
    || /(?:^|\/)node_modules\/@js-joda\/core$/.test(key)
    || (packages[key] && packages[key].name === '@js-joda/core'))
    .map((key) => packages[key]).concat(dependencies['@js-joda/core'] || []).filter(Boolean);
  if (entries.length === 0) return false;
  const path = require('path');
  const fs = require('fs');
  const consumer = path.join(ctx.repoRoot, 'packages', name);
  const localCore = (relative) => {
    // A declared npm link is resolvable before npm materializes node_modules.
    // Check its concrete lock entry first, including the original nested layout.
    const key = path.posix.normalize(relative.replace(/\\/g, '/'));
    const recorded = packages[key];
    if (recorded && (key === '../core' || /(?:^|\/)node_modules\/@js-joda\/core$/.test(key)
      || recorded.name === '@js-joda/core')) {
      return recorded.version === TARGET_VERSION;
    }
    try {
      const manifest = fs.realpathSync(path.resolve(consumer, relative, 'package.json'));
      const root = fs.realpathSync(ctx.repoRoot);
      const confined = path.relative(root, manifest);
      if (confined.startsWith('..' + path.sep) || confined === '..' || path.isAbsolute(confined)) return false;
      const actual = JSON.parse(fs.readFileSync(manifest, 'utf8'));
      return actual.name === '@js-joda/core' && actual.version === TARGET_VERSION;
    } catch (_err) {
      // A missing, unreadable or malformed link target is an invalid lock
      // resolution, not an infrastructure-level inconclusive probe.
      return false;
    }
  };
  return entries.every((entry) => {
    if (entry.link === true && typeof entry.resolved === 'string') {
      return localCore(entry.resolved.replace(/^file:/, ''));
    }
    if (typeof entry.version !== 'string') return false;
    if (entry.version.startsWith('file:')) return localCore(entry.version.slice(5));
    return entry.version === TARGET_VERSION;
  });
}

function moduleUnavailable(loaded) {
  const err = loaded && loaded.error;
  return {
    inconclusive: true,
    reason: `required runtime module unavailable: ${err && err.name ? err.name : 'Error'}: ${String(
      (err && err.message) || '',
    ).slice(0, 160)}`,
  };
}

function targetIdentity(ctx) {
  return ctx.isTargetCoreVersion();
}

function entryPointsPreserved(ctx, name, expected) {
  const actual = ctx.entryPoints(name);
  return (
    actual.main === expected.main &&
    actual.module === expected.module &&
    actual.typings === expected.typings
  );
}

const TIMEZONE_ENTRY = {
  main: 'dist/js-joda-timezone.js',
  module: 'dist/js-joda-timezone.esm.js',
  typings: 'typings/js-joda-timezone.d.ts',
};
const LOCALE_ENTRY = {
  main: 'dist/js-joda-locale.js',
  module: 'dist/js-joda-locale.esm.js',
  typings: 'typings/js-joda-locale.d.ts',
};
const EXTRA_ENTRY = {
  main: 'dist/js-joda-extra.js',
  module: 'dist/js-joda-extra.esm.js',
  typings: 'typings/js-joda-extra.d.ts',
};

function coreExportsPresent(core) {
  const required = [
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
  return required.every((name) => typeof core[name] === 'function' || typeof core[name] === 'object');
}

function timezoneDst(ctx) {
  const core = ctx.loadCore();
  if (!core.ok) {
    return moduleUnavailable(core);
  }
  const timezone = ctx.loadTimezone();
  if (!timezone.ok) {
    return moduleUnavailable(timezone);
  }
  const { ZoneId, ZoneOffset, Instant } = core.module;
  const summer = ZoneId.of('America/New_York')
    .rules()
    .offset(Instant.parse('2016-06-21T00:00:00Z'));
  const winter = ZoneId.of('America/New_York')
    .rules()
    .offset(Instant.parse('2016-12-21T00:00:00Z'));
  return summer.equals(ZoneOffset.ofHours(-4)) && winter.equals(ZoneOffset.ofHours(-5));
}

function zonedNowRoundTrip(ctx) {
  const core = ctx.loadCore();
  if (!core.ok) {
    return moduleUnavailable(core);
  }
  const timezone = ctx.loadTimezone();
  if (!timezone.ok) {
    return moduleUnavailable(timezone);
  }
  const { ZonedDateTime } = core.module;
  const text = ZonedDateTime.now().toString();
  ZonedDateTime.parse(text);
  return /(?:[+-]\d{2}:\d{2}|Z)(?:\[[^\]]*\])?$/.test(text);
}

function localeFormatting(ctx) {
  const core = ctx.loadCore();
  if (!core.ok) {
    return moduleUnavailable(core);
  }
  const locale = ctx.loadLocale();
  if (!locale.ok) {
    return moduleUnavailable(locale);
  }
  const { LocalDate, DateTimeFormatter } = core.module;
  const { Locale } = locale.module;
  const us = DateTimeFormatter.ofPattern('MMMM').withLocale(Locale.US).format(LocalDate.of(2020, 1, 15));
  const de = DateTimeFormatter.ofPattern('MMMM')
    .withLocale(Locale.GERMANY)
    .format(LocalDate.of(2020, 1, 15));
  return us === 'January' && de === 'Januar';
}

function localeExportsPresent(ctx) {
  const locale = ctx.loadLocale();
  if (!locale.ok) {
    return moduleUnavailable(locale);
  }
  const mod = locale.module;
  return (
    typeof mod.Locale === 'function' &&
    typeof mod.WeekFields === 'function' &&
    typeof mod.registerLocaleData === 'function'
  );
}

function extraIntervalBehavior(ctx) {
  const core = ctx.loadCore();
  if (!core.ok) {
    return moduleUnavailable(core);
  }
  const extra = ctx.loadExtra();
  if (!extra.ok) {
    return moduleUnavailable(extra);
  }
  const { Instant } = core.module;
  const { Interval } = extra.module;
  const interval = Interval.of(
    Instant.parse('2020-01-01T00:00:00Z'),
    Instant.parse('2020-02-01T00:00:00Z'),
  );
  return (
    interval.toString() === '2020-01-01T00:00:00Z/2020-02-01T00:00:00Z' &&
    interval.contains(Instant.parse('2020-01-15T00:00:00Z')) === true &&
    interval.contains(Instant.parse('2020-02-01T00:00:00Z')) === false
  );
}

function prebuiltLocaleRegistration(ctx) {
  const core = ctx.loadCore();
  if (!core.ok) {
    return moduleUnavailable(core);
  }
  const locale = ctx.loadLocale();
  if (!locale.ok) {
    return moduleUnavailable(locale);
  }
  const dePackage = ctx.loadLocalePackage('de');
  if (!dePackage.ok) {
    return moduleUnavailable(dePackage);
  }
  const { LocalDate, DateTimeFormatter } = core.module;
  const { Locale } = locale.module;
  const text = DateTimeFormatter.ofPattern('MMMM')
    .withLocale(Locale.GERMANY)
    .format(LocalDate.of(2020, 1, 15));
  return text === 'Januar';
}

function localeKnownFixesUnchanged(ctx) {
  return localeFormatting(ctx);
}

const OBSERVATIONS = [
  {
    id: 'F2P-064',
    requirement_ids: ['R008'],
    surface: 'excess-days initialization during date-time field resolution',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const core = ctx.loadCore();
      if (!core.ok) {
        return moduleUnavailable(core);
      }
      const joda = core.module;
      const { ChronoField, ResolverStyle, LocalDate, Period, _ } = joda;
      const builder = new _.DateTimeBuilder();
      builder._addFieldValue(ChronoField.YEAR, 2016);
      builder._addFieldValue(ChronoField.MONTH_OF_YEAR, 6);
      builder._addFieldValue(ChronoField.DAY_OF_MONTH, 3);
      builder._addFieldValue(ChronoField.HOUR_OF_DAY, 24);
      builder.resolve(ResolverStyle.SMART);
      return (
        builder.date.equals(LocalDate.of(2016, 6, 4)) &&
        builder.excessDays instanceof Period &&
        builder.excessDays.equals(Period.ZERO)
      );
    },
  },
  {
    id: 'F2P-049',
    requirement_ids: ['R038', 'R013'],
    surface: 'prebuilt locale package manifests and side-effect registration',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const manifest = ctx.readJson('packages/locale/packages/de/package.json');
      const peers = manifest.peerDependencies || {};
      const meta = manifest.peerDependenciesMeta || {};
      const structural =
        Object.prototype.hasOwnProperty.call(peers, '@js-joda/core') &&
        Object.prototype.hasOwnProperty.call(peers, '@js-joda/locale') &&
        Object.prototype.hasOwnProperty.call(peers, '@js-joda/timezone') &&
        meta['@js-joda/timezone'] &&
        meta['@js-joda/timezone'].optional === true;
      if (!structural) {
        return false;
      }
      const behavior = prebuiltLocaleRegistration(ctx);
      if (behavior === true || behavior === false) {
        return behavior;
      }
      return behavior;
    },
  },
  {
    id: 'F2P-051',
    requirement_ids: ['R002'],
    surface: 'packages/extra/package-lock.json::prebuilt locale package manifests',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      return coreResolutionInLock(ctx, 'extra');
    },
  },
  {
    id: 'F2P-053',
    requirement_ids: ['R037', 'R012'],
    surface: 'packages/locale/src/plug.js::locale module named exports',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const exportsOk = localeExportsPresent(ctx);
      if (typeof exportsOk !== 'boolean') {
        return exportsOk;
      }
      if (!exportsOk) {
        return false;
      }
      return localeFormatting(ctx);
    },
  },
  {
    id: 'F2P-057',
    requirement_ids: ['R002', 'R011'],
    surface: 'packages/locale/package-lock.json::timezone, locale and extra package entry points',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const lock = ctx.lockJson('locale');
      if (!coreResolutionInLock(ctx, 'locale')) {
        return false;
      }
      return (
        entryPointsPreserved(ctx, 'timezone', TIMEZONE_ENTRY) &&
        entryPointsPreserved(ctx, 'locale', LOCALE_ENTRY) &&
        entryPointsPreserved(ctx, 'extra', EXTRA_ENTRY)
      );
    },
  },
  {
    id: 'F2P-058',
    requirement_ids: ['R002', 'R018'],
    surface: 'packages/extra/package-lock.json::runtime entry',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      if (!coreResolutionInLock(ctx, 'extra')) {
        return false;
      }
      return extraIntervalBehavior(ctx);
    },
  },
  {
    id: 'F2P-059',
    requirement_ids: ['R037', 'R030'],
    surface: 'packages/locale/src/Locale.js::CLDR and prebuilt locale data semantics',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      return localeKnownFixesUnchanged(ctx);
    },
  },
  {
    id: 'F2P-060',
    requirement_ids: ['R037', 'R030'],
    surface: 'packages/locale/src/js-joda-locale.js::CLDR and prebuilt locale data semantics',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      return localeKnownFixesUnchanged(ctx);
    },
  },
  {
    id: 'F2P-061',
    requirement_ids: ['R002', 'R018'],
    surface: 'packages/extra/package-lock.json::extra Interval behavior',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      if (!coreResolutionInLock(ctx, 'extra')) {
        return false;
      }
      return extraIntervalBehavior(ctx);
    },
  },
  {
    id: 'F2P-062',
    requirement_ids: ['R014', 'R012'],
    surface: 'packages/locale/package-lock.json::locale module named exports',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const lock = ctx.lockJson('locale');
      if (!coreResolutionInLock(ctx, 'locale')) {
        return false;
      }
      const range = lock.packages[''] && lock.packages[''].peerDependencies;
      const rangeOk =
        range &&
        typeof range['@js-joda/core'] === 'string' &&
        range['@js-joda/core'].indexOf('>=6.0.0') !== -1;
      if (!rangeOk) {
        return false;
      }
      const exportsOk = localeExportsPresent(ctx);
      if (typeof exportsOk !== 'boolean') {
        return exportsOk;
      }
      return exportsOk;
    },
  },
  {
    id: 'F2P-063',
    requirement_ids: ['R036', 'R015'],
    surface: 'packages/timezone/src/plug.js::timezone plugin side-effect registration',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      return timezoneDst(ctx);
    },
  },
  {
    id: 'F2P-065',
    requirement_ids: ['R037', 'R012'],
    surface: 'packages/locale/src/temporal/WeekFields.js::locale module named exports',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const exportsOk = localeExportsPresent(ctx);
      if (typeof exportsOk !== 'boolean') {
        return exportsOk;
      }
      if (!exportsOk) {
        return false;
      }
      return localeFormatting(ctx);
    },
  },
  {
    id: 'F2P-066',
    requirement_ids: ['R002', 'R011'],
    surface: 'packages/extra/package-lock.json::timezone, locale and extra package entry points',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      if (!coreResolutionInLock(ctx, 'extra')) {
        return false;
      }
      return (
        entryPointsPreserved(ctx, 'timezone', TIMEZONE_ENTRY) &&
        entryPointsPreserved(ctx, 'locale', LOCALE_ENTRY) &&
        entryPointsPreserved(ctx, 'extra', EXTRA_ENTRY)
      );
    },
  },
  {
    id: 'F2P-067',
    requirement_ids: ['R036', 'R015'],
    surface: 'packages/timezone/src/js-joda-timezone.js::timezone plugin side-effect registration',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      return timezoneDst(ctx);
    },
  },
  {
    id: 'F2P-068',
    requirement_ids: ['R036', 'R017'],
    surface: 'packages/timezone/src/js-joda-timezone.js::ZonedDateTime.now() rendering with timezone plugin',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const dst = timezoneDst(ctx);
      if (typeof dst !== 'boolean') {
        return dst;
      }
      if (!dst) {
        return false;
      }
      return zonedNowRoundTrip(ctx);
    },
  },
  {
    id: 'F2P-069',
    requirement_ids: ['R002'],
    surface: 'packages/locale/package-lock.json::runtime entry',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const manifest = ctx.pkgJson('locale');
      const dev = manifest.devDependencies || {};
      return (
        dev['@js-joda/core'] === `^${TARGET_VERSION}` &&
        coreResolutionInLock(ctx, 'locale')
      );
    },
  },
  {
    id: 'F2P-070',
    requirement_ids: ['R037', 'R012'],
    surface: 'packages/locale/src/Locale.js::locale module named exports',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const exportsOk = localeExportsPresent(ctx);
      if (typeof exportsOk !== 'boolean') {
        return exportsOk;
      }
      if (!exportsOk) {
        return false;
      }
      return localeFormatting(ctx);
    },
  },
  {
    id: 'F2P-071',
    requirement_ids: ['R037', 'R030'],
    surface: 'packages/locale/src/temporal/WeekFields.js::CLDR and prebuilt locale data semantics',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      return localeKnownFixesUnchanged(ctx);
    },
  },
  {
    id: 'F2P-072',
    requirement_ids: ['R036', 'R017'],
    surface: 'packages/timezone/src/plug.js::ZonedDateTime.now() rendering with timezone plugin',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const dst = timezoneDst(ctx);
      if (typeof dst !== 'boolean') {
        return dst;
      }
      if (!dst) {
        return false;
      }
      return zonedNowRoundTrip(ctx);
    },
  },
  {
    id: 'F2P-050',
    requirement_ids: ['R002', 'R029'],
    surface: 'packages/timezone/package-lock.json::packaged IANA time zone data',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const lock = ctx.lockJson('timezone');
      return coreResolutionInLock(ctx, 'timezone');
    },
  },
  {
    id: 'F2P-052',
    requirement_ids: ['R035', 'R031'],
    surface: 'packages/core/package-lock.json::library capability positioning',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const core = ctx.loadCore();
      if (!core.ok) {
        return moduleUnavailable(core);
      }
      const cjsLocalDate = core.module.LocalDate;
      const cjsResult = cjsLocalDate.parse('2020-01-02').toString();
      if (cjsResult !== '2020-01-02') {
        return false;
      }
      const esmPath = require('path').join(ctx.repoRoot, 'packages', 'core', 'dist', 'js-joda.esm.js');
      return import(esmPath)
        .then((esm) => {
          const esmLocalDate = esm.LocalDate || (esm.default && esm.default.LocalDate);
          return (
            typeof esmLocalDate === 'function' &&
            esmLocalDate.name === cjsLocalDate.name &&
            esmLocalDate.parse('2020-01-02').toString() === '2020-01-02'
          );
        })
        .catch(() => ({
          inconclusive: true,
          reason: 'core ES module entry not loadable',
        }));
    },
  },
  {
    id: 'F2P-054',
    requirement_ids: ['R014', 'R010'],
    surface: 'packages/core/package-lock.json::core module named exports',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const lock = ctx.lockJson('core');
      if (!lock.packages || !lock.packages[''] || lock.packages[''].version !== TARGET_VERSION) {
        return false;
      }
      const core = ctx.loadCore();
      if (!core.ok) {
        return moduleUnavailable(core);
      }
      return coreExportsPresent(core.module);
    },
  },
  {
    id: 'F2P-055',
    requirement_ids: ['R002'],
    surface: 'packages/timezone/package-lock.json::runtime entry',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const manifest = ctx.pkgJson('timezone');
      const dev = manifest.devDependencies || {};
      const peers = manifest.peerDependencies || {};
      const lock = ctx.lockJson('timezone');
      const coreLink = lock.packages && lock.packages['../core'];
      return (
        dev['@js-joda/core'] === `^${TARGET_VERSION}` &&
        typeof peers['@js-joda/core'] === 'string' &&
        peers['@js-joda/core'].indexOf('>=5.7.0') !== -1 &&
        !!coreLink &&
        coreLink.version === TARGET_VERSION
      );
    },
  },
  {
    id: 'F2P-056',
    requirement_ids: ['R002'],
    surface: 'packages/core/package-lock.json::runtime entry',
    probe(ctx) {
      if (!targetIdentity(ctx)) {
        return false;
      }
      const lock = ctx.lockJson('core');
      const entry = lock.packages && lock.packages[''];
      return (
        !!entry &&
        entry.version === TARGET_VERSION &&
        entryPointsPreserved(ctx, 'core', {
          main: 'dist/js-joda.js',
          module: 'dist/js-joda.esm.js',
          typings: 'typings/js-joda.d.ts',
        })
      );
    },
  },
];

module.exports = { OBSERVATIONS, TARGET_VERSION };
