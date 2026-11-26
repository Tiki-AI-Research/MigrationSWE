'use strict';

/**
 * Shared support for BATCH-F2P-0003 (js-joda core 6.1.0 -> 6.3.2).
 *
 * Consumes the shared corpus produced by BATCH-SKELETON-0001 through the
 * read-only dependency snapshot declared in
 * /assets/dependencies/dependency-manifest.json.  The dependency files are
 * never copied or rewritten: the skeleton module is loaded in place from
 * shared/skeleton and only used for its frozen metadata.
 */

const fs = require('fs');
const path = require('path');
const { createRequire } = require('module');

function loadSkeleton() {
  // 1. Assembled asset tree: <assets>/shared/batches/BATCH-F2P-0003 -> shared/skeleton
  try {
    return require(path.join(__dirname, '..', '..', 'skeleton'));
  } catch (err) {
    /* continue */
  }
  // 2. Read-only dependency snapshot, consumed through dependency-manifest.json.
  const depRoot = process.env.MIGRATION_DEPENDENCIES_DIR || '/assets/dependencies';
  try {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(depRoot, 'dependency-manifest.json'), 'utf8'),
    );
    for (const batch of manifest.batches || []) {
      if ((batch.files || []).indexOf('shared/skeleton') !== -1) {
        try {
          return require(path.join(depRoot, 'batches', batch.batch_id, 'shared', 'skeleton'));
        } catch (inner) {
          /* try next batch */
        }
      }
    }
  } catch (err) {
    /* dependency snapshot unavailable */
  }
  // 3. Last-resort frozen fallback (keeps this module usable in isolation).
  return {
    MIGRATION: Object.freeze({
      category: 'version_upgrade',
      name: 'js-joda',
      sourceVersion: '6.1.0',
      targetVersion: '6.3.2',
    }),
  };
}

const SKELETON = loadSkeleton();

const MIGRATION = SKELETON.MIGRATION;
const TARGET_VERSION = MIGRATION.targetVersion;
const SOURCE_VERSION = MIGRATION.sourceVersion;

let babelRegistered = false;
function registerBabel(req) {
  if (babelRegistered) {
    return;
  }
  babelRegistered = true;
  try {
    req('@babel/register')({
      babelrc: false,
      configFile: false,
      cache: false,
      presets: [
        [
          req.resolve('@babel/preset-env'),
          { targets: { node: 'current' }, modules: 'commonjs', loose: true },
        ],
      ],
      ignore: [/node_modules/],
      only: [/packages[\\/][^\\/]+[\\/]src/],
      extensions: ['.js'],
    });
  } catch (err) {
    // Babel is only required when distribution bundles are absent; probes
    // degrade to INCONCLUSIVE instead of failing when it cannot be loaded.
    babelRegistered = false;
  }
}

function safeReadJson(absPath) {
  return JSON.parse(fs.readFileSync(absPath, 'utf8'));
}

function createContext(repoRoot) {
  const repo = path.resolve(repoRoot);
  const req = createRequire(path.join(repo, 'package.json'));
  registerBabel(req);

  const cache = Object.create(null);

  const readJson = (relPath) => safeReadJson(path.join(repo, relPath));
  const readText = (relPath) => fs.readFileSync(path.join(repo, relPath), 'utf8');

  const pkgJson = (name) => readJson(path.join('packages', name, 'package.json'));
  const lockJson = (name) => readJson(path.join('packages', name, 'package-lock.json'));

  function loadModule(specifier, fallbackRelPath) {
    if (cache[specifier] !== undefined) {
      return cache[specifier];
    }
    let loaded = { ok: false, error: null, module: null };
    try {
      loaded = { ok: true, error: null, module: req(specifier) };
    } catch (primaryError) {
      if (typeof fallbackRelPath === 'string' && fallbackRelPath.length > 0) {
        try {
          loaded = { ok: true, error: null, module: req(path.join(repo, fallbackRelPath)) };
        } catch (fallbackError) {
          loaded = { ok: false, error: fallbackError, module: null };
        }
      } else {
        loaded = { ok: false, error: primaryError, module: null };
      }
    }
    cache[specifier] = loaded;
    return loaded;
  }

  const ctx = {
    repoRoot: repo,
    req,
    migration: MIGRATION,
    targetName: MIGRATION.name,
    targetVersion: TARGET_VERSION,
    sourceVersion: SOURCE_VERSION,
    readJson,
    readText,
    pkgJson,
    lockJson,
    coreManifest: () => pkgJson('core'),
    coreVersion: () => pkgJson('core').version,
    isTargetCoreVersion: () => pkgJson('core').version === TARGET_VERSION,
    loadCore() {
      return loadModule('@js-joda/core', path.join('packages', 'core', 'src', 'js-joda.js'));
    },
    loadTimezone() {
      return loadModule('@js-joda/timezone', path.join('packages', 'timezone', 'src', 'js-joda-timezone.js'));
    },
    loadLocale() {
      return loadModule('@js-joda/locale', path.join('packages', 'locale', 'src', 'js-joda-locale.js'));
    },
    loadLocalePackage(locale) {
      return loadModule(
        `@js-joda/locale_${locale}`,
        path.join('packages', 'locale', 'packages', locale),
      );
    },
    loadExtra() {
      return loadModule('@js-joda/extra', path.join('packages', 'extra', 'src', 'js-joda-extra.js'));
    },
    entryPoints(name) {
      const manifest = pkgJson(name);
      return {
        main: manifest.main,
        module: manifest.module,
        typings: manifest.typings,
      };
    },
  };

  return ctx;
}

function classifyOutcome(outcome, result) {
  if (outcome === true) {
    result.status = 'PASS';
    result.detail = 'target postcondition satisfied';
  } else if (outcome === false) {
    result.status = 'FAIL';
    result.detail = 'target postcondition not satisfied';
  } else if (outcome && outcome.inconclusive) {
    result.status = 'INCONCLUSIVE';
    result.detail = outcome.reason || 'required runtime unavailable';
  } else if (outcome && outcome.inconclusive === false) {
    result.status = outcome.pass ? 'PASS' : 'FAIL';
    result.detail = outcome.detail || '';
  } else {
    result.status = 'FAIL';
    result.detail = 'probe returned a non-boolean outcome';
  }
  return result;
}

function classifyError(err, result) {
  const e = err || {};
  const message = String(e.message || '');
  if (e.code === 'MODULE_NOT_FOUND' || e.name === 'JsJodaException' || /not imported/i.test(message)) {
    result.status = 'INCONCLUSIVE';
    result.detail = `unrelated execution/setup error: ${e.name || 'Error'}: ${message.slice(0, 160)}`;
  } else if (
    e.name === 'DateTimeParseException' ||
    e.name === 'DateTimeException' ||
    e.name === 'ZoneRulesException'
  ) {
    result.status = 'FAIL';
    result.detail = `${e.name}: ${message.slice(0, 160)}`;
  } else {
    result.status = 'INCONCLUSIVE';
    result.detail = `probe error: ${e.name || 'Error'}: ${message.slice(0, 160)}`;
  }
  return result;
}

async function runObservation(ctx, observation) {
  const result = { observation_id: observation.id, status: 'INCONCLUSIVE', detail: '' };
  try {
    const outcome = await observation.probe(ctx);
    return classifyOutcome(outcome, result);
  } catch (err) {
    return classifyError(err, result);
  }
}

async function runAll(ctx, observations) {
  const results = [];
  for (const observation of observations) {
    results.push(await runObservation(ctx, observation));
  }
  return results;
}

module.exports = {
  MIGRATION,
  TARGET_VERSION,
  SOURCE_VERSION,
  createContext,
  runObservation,
  runAll,
};
