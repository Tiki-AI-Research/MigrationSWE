'use strict';

/*
 * Shared differential case corpus for BATCH-DIFFERENTIAL-0001.
 *
 * The corpus describes observable behaviour that must be identical on the
 * source revision (6.1.0 manifest state) and on the migrated target
 * (6.3.2).  It is intentionally dependency free apart from the Node standard
 * library and the packages that already live in the repository.  Both the
 * source probe and the target harness execute the very same corpus so the
 * differential relation `equal_after_normalization` can be evaluated.
 *
 * Every observation produces a JSON serialisable, deterministic value.
 * Long running commands are always wrapped by the GNU `timeout` binary and
 * never run without an upper bound.
 */

const fs = require('fs');
const path = require('path');
const Module = require('module');
const { spawnSync } = require('child_process');

const BATCH_ID = 'BATCH-DIFFERENTIAL-0001';

const OBSERVATION_IDS = Object.freeze([
    'DIFF-001',
    'DIFF-002',
    'DIFF-003',
    'DIFF-004',
    'DIFF-005',
    'DIFF-006',
    'DIFF-007',
    'DIFF-008',
    'DIFF-009',
    'DIFF-010',
    'DIFF-011',
    'DIFF-012',
    'DIFF-013',
    'DIFF-014',
    'DIFF-015',
    'DIFF-016',
    'DIFF-017',
    'DIFF-018',
    'DIFF-019',
    'DIFF-020',
    'DIFF-021',
]);

const CORE_REQUIRED_EXPORTS = Object.freeze([
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
]);

const PACKAGE_ENTRY_KEYS = Object.freeze(['main', 'module', 'typings']);

function readText(file) {
    return fs.readFileSync(file, 'utf8');
}

function readJson(file) {
    return JSON.parse(readText(file));
}

function fileExists(file) {
    try {
        fs.accessSync(file);
        return true;
    } catch (e) {
        return false;
    }
}

function readManifest(repoRoot, relativePath) {
    const file = path.join(repoRoot, relativePath);
    if (!fileExists(file)) {
        return { __missing: true, __path: relativePath };
    }
    return readJson(file);
}

function pick(manifest, keys) {
    const out = {};
    for (const key of keys) {
        out[key] = manifest && Object.prototype.hasOwnProperty.call(manifest, key)
            ? manifest[key]
            : null;
    }
    return out;
}

function listLocalePackageManifests(repoRoot) {
    const base = path.join(repoRoot, 'packages', 'locale', 'packages');
    if (!fileExists(base)) {
        return [];
    }
    return fs
        .readdirSync(base, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => `packages/locale/packages/${entry.name}/package.json`)
        .filter((relative) => fileExists(path.join(repoRoot, relative)))
        .sort();
}

function runWithTimeout(command, args, options) {
    const timeoutSec = options.timeoutSec;
    const wrapped = ['timeout', '--kill-after=10', String(timeoutSec), command].concat(args);
    const result = spawnSync(wrapped[0], wrapped.slice(1), {
        cwd: options.cwd,
        encoding: 'utf8',
        maxBuffer: 64 * 1024 * 1024,
        env: Object.assign({}, process.env, { NODE_ENV: 'test', CI: '1' }),
    });
    if (result.error) {
        return {
            ran: false,
            ok: false,
            error: result.error.code || String(result.error.message),
        };
    }
    return {
        ran: true,
        ok: result.status === 0,
        status: result.status,
        signal: result.signal || null,
        timedOut: result.status === 124 || result.signal === 'SIGKILL',
    };
}

function gitChangedPaths(repoRoot) {
    const rev = spawnSync('git', ['-C', repoRoot, 'rev-parse', '--is-inside-work-tree'], {
        encoding: 'utf8',
    });
    if (rev.status !== 0 || String(rev.stdout).trim() !== 'true') {
        return null;
    }
    const diff = spawnSync(
        'git',
        ['-C', repoRoot, 'diff', '--name-only', 'HEAD', '--', '.'],
        { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 }
    );
    if (diff.status !== 0) {
        return null;
    }
    return String(diff.stdout)
        .split('\n')
        .map((line) => line.trim())
        .filter((line) => line.length > 0)
        .sort();
}

function hasBrowserBinary() {
    for (const envKey of ['CHROME_BIN', 'FIREFOX_BIN']) {
        if (process.env[envKey] && fileExists(process.env[envKey])) {
            return true;
        }
    }
    for (const binary of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'firefox']) {
        const probe = spawnSync('sh', ['-c', `command -v ${binary}`], { encoding: 'utf8' });
        if (probe.status === 0 && String(probe.stdout).trim().length > 0) {
            return true;
        }
    }
    return false;
}

function resolveFrom(repoRoot, request) {
    try {
        return require.resolve(request, { paths: [path.join(repoRoot, 'node_modules')] });
    } catch (e) {
        return request;
    }
}

/*
 * Loads the package sources through @babel/register.  `@js-joda/core` is
 * redirected to the in-repo core entry so the plugin packages under test use
 * the same library instance as the corpus.
 */
function loadRepository(repoRoot) {
    const babelRegister = require(resolveFrom(repoRoot, '@babel/register'));
    const presetEnv = resolveFrom(repoRoot, '@babel/preset-env');
    const coreEntry = path.join(repoRoot, 'packages', 'core', 'src', 'js-joda.js');

    const originalResolve = Module._resolveFilename;
    Module._resolveFilename = function resolveFilename(request) {
        if (request === '@js-joda/core') {
            return coreEntry;
        }
        return originalResolve.apply(this, arguments);
    };

    babelRegister({
        cwd: repoRoot,
        extensions: ['.js'],
        ignore: [/node_modules/],
        babelrc: false,
        configFile: false,
        presets: [[presetEnv, { targets: { node: 'current' }, modules: 'commonjs', loose: true }]],
    });

    const joda = require(coreEntry);
    let timezone = null;
    let extra = null;
    let locale = null;

    try {
        timezone = require(path.join(repoRoot, 'packages', 'timezone', 'src', 'js-joda-timezone.js'));
    } catch (e) {
        timezone = { __loadError: String(e && e.message) };
    }
    try {
        extra = require(path.join(repoRoot, 'packages', 'extra', 'src', 'js-joda-extra.js'));
    } catch (e) {
        extra = { __loadError: String(e && e.message) };
    }
    try {
        locale = require(path.join(repoRoot, 'packages', 'locale', 'src', 'js-joda-locale.js'));
        const { registerLocaleData } = locale;
        for (const name of ['en', 'en-GB', 'fr', 'de', 'ko', 'ja']) {
            registerLocaleData(
                `main/${name}/ca-gregorian.json`,
                require(`cldr-data/main/${name}/ca-gregorian.json`)
            );
            registerLocaleData(
                `main/${name}/timeZoneNames.json`,
                require(`cldr-data/main/${name}/timeZoneNames.json`)
            );
        }
    } catch (e) {
        locale = { __loadError: String(e && e.message) };
    }

    return { joda, timezone, extra, locale, coreEntry };
}

function normalizeNowTimestamp(value) {
    return String(value).replace(
        /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?/,
        '<NOW>'
    );
}

function defineChecks(ctx) {
    const { repoRoot } = ctx;
    const gitPaths = gitChangedPaths(repoRoot);

    function manifestCheck(relative, keys) {
        return { manifest: relative, values: pick(readManifest(repoRoot, relative), keys) };
    }

    const checks = {};

    checks['DIFF-001'] = function diff001() {
        return manifestCheck('packages/core/package.json', PACKAGE_ENTRY_KEYS);
    };

    checks['DIFF-002'] = function diff002() {
        const { joda } = ctx.load();
        const missing = CORE_REQUIRED_EXPORTS.filter((name) => typeof joda[name] === 'undefined');
        return { missing };
    };

    checks['DIFF-003'] = function diff003() {
        return {
            timezone: manifestCheck('packages/timezone/package.json', PACKAGE_ENTRY_KEYS).values,
            locale: manifestCheck('packages/locale/package.json', PACKAGE_ENTRY_KEYS).values,
            extra: manifestCheck('packages/extra/package.json', PACKAGE_ENTRY_KEYS).values,
        };
    };

    checks['DIFF-004'] = function diff004() {
        const { locale } = ctx.load();
        let callable = false;
        try {
            locale.registerLocaleData('bogus/locale.json', {});
            callable = true;
        } catch (e) {
            callable = false;
        }
        return {
            exports: {
                Locale: typeof locale.Locale === 'function' || typeof locale.Locale === 'object',
                WeekFields: typeof locale.WeekFields === 'function',
                registerLocaleData: typeof locale.registerLocaleData === 'function',
            },
            callable,
        };
    };

    checks['DIFF-005'] = function diff005() {
        const manifests = listLocalePackageManifests(repoRoot);
        const failures = [];
        for (const relative of manifests) {
            const manifest = readManifest(repoRoot, relative);
            const peers = manifest.peerDependencies || {};
            const meta = manifest.peerDependenciesMeta || {};
            const ok =
                Object.prototype.hasOwnProperty.call(peers, '@js-joda/core') &&
                Object.prototype.hasOwnProperty.call(peers, '@js-joda/locale') &&
                Object.prototype.hasOwnProperty.call(peers, '@js-joda/timezone') &&
                meta['@js-joda/timezone'] &&
                meta['@js-joda/timezone'].optional === true;
            if (!ok) {
                failures.push(relative);
            }
        }
        return { failures };
    };

    checks['DIFF-006'] = function diff006() {
        const { joda } = ctx.load();
        const zone = joda.ZoneId.of('Europe/Berlin');
        return {
            id: zone.id(),
            rulesType: typeof zone.rules === 'function' ? 'function' : 'missing',
        };
    };

    checks['DIFF-007'] = function diff007() {
        const { joda } = ctx.load();
        const date = joda.LocalDate.of(2020, 1, 1);
        const dateBefore = date.toString();
        const dateNext = date.plusDays(1);
        const instant = joda.Instant.parse('2020-01-01T00:00:00Z');
        const instantBefore = instant.toString();
        const instantNext = instant.plusSeconds(1);
        const zoned = joda.ZonedDateTime.parse('2020-01-01T00:00:00Z');
        const zonedBefore = zoned.toString();
        const zonedNext = zoned.withYear(2021);
        return {
            newInstances: date !== dateNext && instant !== instantNext && zoned !== zonedNext,
            receiverStable:
                date.toString() === dateBefore &&
                instant.toString() === instantBefore &&
                zoned.toString() === zonedBefore,
        };
    };

    checks['DIFF-008'] = function diff008() {
        const { joda } = ctx.load();
        const now = joda.ZonedDateTime.now().toString();
        const roundTripped = joda.ZonedDateTime.parse(now).toString();
        return {
            render: normalizeNowTimestamp(now).replace(/\[[^\]]*\]$/, '<ZONE>'),
            roundTrip: normalizeNowTimestamp(roundTripped).replace(/\[[^\]]*\]$/, '<ZONE>'),
            hasOffset: /(?:Z|[+-]\d{2}:\d{2})(?:\[[^\]]*\])?$/.test(now),
        };
    };

    checks['DIFF-009'] = function diff009() {
        const { joda, extra } = ctx.load();
        const range = extra.LocalDateRange.of(joda.LocalDate.of(2020, 1, 1), joda.LocalDate.of(2020, 2, 1));
        const interval = extra.Interval.of(
            joda.Instant.parse('2020-01-01T00:00:00Z'),
            joda.Instant.parse('2020-02-01T00:00:00Z')
        );
        return {
            rangeToString: range.toString(),
            rangeContainsInside: range.contains(joda.LocalDate.of(2020, 1, 15)),
            rangeContainsEnd: range.contains(joda.LocalDate.of(2020, 2, 1)),
            intervalToString: interval.toString(),
            intervalContainsInside: interval.contains(joda.Instant.parse('2020-01-15T00:00:00Z')),
            intervalContainsEnd: interval.contains(joda.Instant.parse('2020-02-01T00:00:00Z')),
        };
    };

    function formatMonth(locale, date) {
        const { joda } = ctx.load();
        return joda.DateTimeFormatter.ofPattern('MMMM').withLocale(locale).format(date);
    }

    checks['DIFF-010'] = function diff010() {
        const { joda, locale } = ctx.load();
        const date = joda.LocalDate.of(2020, 1, 15);
        return {
            enUS: formatMonth(locale.Locale.US, date),
            deDE: formatMonth(locale.Locale.GERMANY, date),
        };
    };

    checks['DIFF-011'] = function diff011() {
        const scripts = [];
        for (const name of ['test', 'test-browser', 'test-ts-definitions', 'lint']) {
            const manifest = readManifest(repoRoot, 'packages/core/package.json');
            if (typeof (manifest.scripts || {})[name] === 'string') {
                scripts.push(name);
            }
        }
        scripts.sort();
        return { declaredScripts: scripts };
    };

    checks['DIFF-012'] = function diff012() {
        const manifests = {};
        for (const name of ['core', 'timezone', 'locale', 'extra']) {
            const manifest = readManifest(repoRoot, `packages/${name}/package.json`);
            const scripts = manifest.scripts || {};
            manifests[name] = {
                buildDist: typeof scripts['build-dist'] === 'string',
                buildLocaleDist: typeof scripts['build-locale-dist'] === 'string',
            };
        }
        return { declared: manifests };
    };

    checks['DIFF-013'] = function diff013() {
        const { joda } = ctx.load();
        return {
            chronoUnitSeconds: typeof joda.ChronoUnit.SECONDS === 'object',
            isoLocalDate: typeof joda.DateTimeFormatter.ISO_LOCAL_DATE === 'object',
            periodZero: typeof joda.Period.ZERO === 'object',
            firstDayOfMonth: typeof joda.TemporalAdjusters.firstDayOfMonth === 'function',
        };
    };

    checks['DIFF-014'] = function diff014() {
        const { joda } = ctx.load();
        const entryValues = [
            'packages/core/package.json',
            'packages/timezone/package.json',
            'packages/locale/package.json',
            'packages/extra/package.json',
        ].map((relative) => ({
            path: relative,
            values: pick(readManifest(repoRoot, relative), PACKAGE_ENTRY_KEYS),
        }));
        return {
            missing: CORE_REQUIRED_EXPORTS.filter((name) => typeof joda[name] === 'undefined'),
            entryValues,
        };
    };

    checks['DIFF-015'] = function diff015() {
        const { joda } = ctx.load();
        return {
            localTime: joda.LocalTime.parse('12:30').toString(),
            yearMonth: joda.YearMonth.parse('2020-06').toString(),
            leapDay: joda.LocalDate.parse('2020-02-29').plusDays(1).toString(),
            duration90: joda.Duration.ofSeconds(90).toString(),
            epoch: joda.Instant.parse('1970-01-01T00:00:00Z').toString(),
        };
    };

    function scopeFailure(paths) {
        if (paths === null) {
            return { outOfScope: [] };
        }
        const allowed = [
            /^lerna\.json$/,
            /^package\.json$/,
            /^package-lock\.json$/,
            /^packages\/[^/]+\/package\.json$/,
            /^packages\/[^/]+\/package-lock\.json$/,
            /^packages\/[^/]+\/js-joda[^/]*\.js$/,
            /^packages\/[^/]+\/src\/js-joda[^/]*\.js$/,
            /^packages\/[^/]+\/typings\//,
            /^packages\/locale\/packages\/[^/]+\/package\.json$/,
        ];
        const outOfScope = paths.filter((p) => !allowed.some((re) => re.test(p)));
        return { outOfScope };
    }

    checks['DIFF-016'] = function diff016() {
        return scopeFailure(gitPaths);
    };

    checks['DIFF-017'] = function diff017() {
        if (gitPaths === null) {
            return { modified: [] };
        }
        const modified = gitPaths.filter((p) => p.startsWith('packages/timezone/data/packed/'));
        return { modified };
    };

    checks['DIFF-018'] = function diff018() {
        const { joda, locale } = ctx.load();
        const date = joda.LocalDate.of(2020, 1, 15);
        let dataModified = [];
        if (gitPaths !== null) {
            dataModified = gitPaths.filter(
                (p) =>
                    /^packages\/locale\/.*(cldr|supplemental|dist\/prebuilt)/i.test(p) &&
                    !/package(-lock)?\.json$/.test(p)
            );
        }
        return {
            months: {
                en: formatMonth(locale.Locale.US, date),
                de: formatMonth(locale.Locale.GERMANY, date),
                fr: formatMonth(locale.Locale.FRANCE, date),
                ja: formatMonth(locale.Locale.JAPAN, date),
            },
            dataModified,
        };
    };

    checks['DIFF-019'] = function diff019() {
        const uiNames = ['react', 'react-dom', 'vue', 'angular', 'svelte', 'jquery'];
        const offending = [];
        for (const rel of [
            'packages/core/package.json',
            'packages/timezone/package.json',
            'packages/locale/package.json',
            'packages/extra/package.json',
        ]) {
            const manifest = readManifest(repoRoot, rel);
            const values = pick(manifest, PACKAGE_ENTRY_KEYS);
            for (const key of PACKAGE_ENTRY_KEYS) {
                if (values[key] && /browser|dom|ui\//i.test(String(values[key]))) {
                    offending.push(`${rel}:${key}=${values[key]}`);
                }
            }
            const deps = Object.assign({}, manifest.dependencies, manifest.devDependencies);
            for (const name of uiNames) {
                if (Object.prototype.hasOwnProperty.call(deps, name)) {
                    offending.push(`${rel}:dep=${name}`);
                }
            }
        }
        return { uiEntryAdded: offending.length > 0 ? offending : false };
    };

    checks['DIFF-020'] = function diff020() {
        if (gitPaths === null) {
            return { changedTests: [] };
        }
        const changedTests = gitPaths.filter((p) => /^packages\/[^/]+\/test\//.test(p));
        return { changedTests };
    };

    checks['DIFF-021'] = function diff021() {
        if (gitPaths === null) {
            return { escaped: [] };
        }
        const escaped = gitPaths.filter(
            (p) => path.isAbsolute(p) || p.split('/').indexOf('..') !== -1
        );
        return { escaped };
    };

    return checks;
}

/*
 * Executions are only performed on the target side.  Every command is wrapped
 * by the GNU `timeout` binary and results are reported separately from the
 * side invariant `value` so the differential value stays comparable between
 * source and target.  Tests that need a browser binary the environment does
 * not provide are skipped instead of blocking forever.
 */
function defineExecutions(ctx) {
    const { repoRoot, options } = ctx;

    const executions = {};

    executions['DIFF-011'] = function diff011Execution() {
        const coreDir = path.join(repoRoot, 'packages', 'core');
        const test = runWithTimeout('npm', ['test'], { cwd: coreDir, timeoutSec: options.testTimeoutSec });
        const lint = runWithTimeout('npm', ['run', 'lint'], { cwd: coreDir, timeoutSec: options.lintTimeoutSec });
        const distExists = fileExists(path.join(coreDir, 'dist', 'js-joda.js'));
        const tsDefinitions = distExists
            ? runWithTimeout('npm', ['run', 'test-ts-definitions'], {
                  cwd: coreDir,
                  timeoutSec: options.tsTimeoutSec,
              })
            : { ran: false, ok: null, error: 'no-dist' };
        const browser = hasBrowserBinary()
            ? runWithTimeout('npm', ['run', 'test-browser'], {
                  cwd: coreDir,
                  timeoutSec: options.browserTimeoutSec,
              })
            : { ran: false, ok: null, error: 'no-browser-binary' };
        return {
            test,
            lint,
            tsDefinitions,
            browser,
            passed:
                test.ok === true &&
                lint.ok === true &&
                (tsDefinitions.ok === true || tsDefinitions.ok === null) &&
                (browser.ok === true || browser.ok === null),
        };
    };

    executions['DIFF-012'] = function diff012Execution() {
        const coreDir = path.join(repoRoot, 'packages', 'core');
        const build = runWithTimeout('npm', ['run', 'build-dist'], {
            cwd: coreDir,
            timeoutSec: options.buildTimeoutSec,
        });
        const distDir = path.join(coreDir, 'dist');
        return {
            buildDist: build,
            coreDist: ['js-joda.js', 'js-joda.esm.js', 'js-joda.min.js'].filter((f) =>
                fileExists(path.join(distDir, f))
            ),
            passed: build.ok === true,
        };
    };

    return executions;
}

function runObservation(id) {
    const ctx = this;
    const check = ctx.checks[id];
    if (typeof check !== 'function') {
        return { passed: false, error: 'unknown-observation', value: null };
    }
    let value;
    try {
        value = check();
    } catch (e) {
        return {
            passed: false,
            error: `${(e && e.constructor && e.constructor.name) || 'Error'}: ${e && e.message}`,
            value: null,
        };
    }

    if (ctx.options.execute && ctx.executions[id]) {
        try {
            const execution = ctx.executions[id]();
            const executionPassed = execution.passed !== false;
            return { passed: executionPassed, value, execution, error: null };
        } catch (e) {
            return {
                passed: false,
                value,
                execution: null,
                error: `${(e && e.constructor && e.constructor.name) || 'Error'}: ${e && e.message}`,
            };
        }
    }

    return { passed: true, value, execution: null, error: null };
}

function runCorpus(repoRoot, options) {
    const opts = Object.assign(
        {
            execute: false,
            testTimeoutSec: 900,
            lintTimeoutSec: 300,
            tsTimeoutSec: 300,
            browserTimeoutSec: 600,
            buildTimeoutSec: 600,
        },
        options || {}
    );

    let loaded = null;
    const ctx = {
        repoRoot,
        options: opts,
        load() {
            if (!loaded) {
                loaded = loadRepository(repoRoot);
            }
            return loaded;
        },
    };
    ctx.checks = defineChecks(ctx);
    ctx.executions = defineExecutions(ctx);

    const observations = {};
    for (const id of OBSERVATION_IDS) {
        observations[id] = runObservation.call(ctx, id);
    }

    return {
        batch_id: BATCH_ID,
        repo_root: repoRoot,
        execute: opts.execute === true,
        observations,
    };
}

function normalize(value) {
    if (Array.isArray(value)) {
        return value.map(normalize);
    }
    if (value && typeof value === 'object') {
        const out = {};
        for (const key of Object.keys(value).sort()) {
            out[key] = normalize(value[key]);
        }
        return out;
    }
    return value;
}

function diffReports(expected, actual) {
    const differences = [];
    for (const id of OBSERVATION_IDS) {
        const exp = expected && expected.observations ? expected.observations[id] : undefined;
        const act = actual && actual.observations ? actual.observations[id] : undefined;
        const expMatch = !exp || exp.passed !== false;
        const actNormalized = normalize(act ? act.value : null);
        const expNormalized = normalize(exp ? exp.value : null);
        const equal = JSON.stringify(expNormalized) === JSON.stringify(actNormalized);
        if (!actMatch(act) || !equal) {
            differences.push({
                observation_id: id,
                expected_passed: !exp || exp.passed !== false,
                actual_passed: !!(act && act.passed !== false),
                expected_value: expNormalized,
                actual_value: actNormalized,
            });
        }
    }
    return differences;
}

function actMatch(act) {
    return !!act && act.passed !== false;
}

module.exports = {
    BATCH_ID,
    OBSERVATION_IDS,
    CORE_REQUIRED_EXPORTS,
    runCorpus,
    runWithTimeout,
    hasBrowserBinary,
    normalize,
    diffReports,
};
