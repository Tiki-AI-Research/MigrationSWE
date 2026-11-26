'use strict';

/*
 * Shared runtime support for the BATCH-INTEGRATION-0001 harness.
 *
 * The integration entry points (source/runner and harness.js) must run the
 * executable projections produced by the dependency batches against a
 * concrete js-joda checkout.  This module centralises the process-level setup
 * that those projections rely on:
 *
 *   - workspace `node_modules` on NODE_PATH (so shared batch code loads its
 *     own dependencies),
 *   - Babel registration for the in-repo ES module sources,
 *   - a monorepo module redirect that maps the bare `@js-joda/*` specifiers to
 *     the in-repo entry files when the workspace link is absent (the built
 *     `main` bundle when available, otherwise the ES `src` entry),
 *   - a bounded process spawn helper that always wraps long commands with the
 *     GNU `timeout` binary.
 *
 * Nothing here reads `dependencies/`, `fragments/` or any authoring private
 * path: only the assembled asset tree and the repository under test.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const Module = require('module');
const { spawnSync } = require('child_process');
const { createRequire } = require('module');

const ASSET_ROOT = path.resolve(__dirname, '..', '..');
const ORIGINAL_RESOLVE = Module._resolveFilename;

/*
 * Capture the evaluator-owned report writers before any candidate module is
 * loaded.  The harness later ``require``s the candidate under test in this very
 * process, so a candidate can monkey-patch ``fs.writeFileSync`` (or
 * ``JSON.stringify``) to rewrite the observation report it is being scored on.
 * Writing through these captured references keeps the report genuine.
 */
const ORIGINAL_WRITE_FILE_SYNC = fs.writeFileSync;
const ORIGINAL_MKDIR_SYNC = fs.mkdirSync;
const ORIGINAL_JSON_STRINGIFY = JSON.stringify;
/*
 * The candidate also shares this process, so it can register a
 * ``process.on('exit')`` hook that rewrites the report file after the harness
 * has serialised it but before the Python verifier reads it.  Capturing the
 * stdout writer and the digest primitive here lets the harness emit a
 * content-bound digest of the exact bytes it wrote over a channel a candidate
 * exit hook cannot rewrite; the verifier then compares that digest against the
 * file it is about to score.
 */
const ORIGINAL_STDOUT_WRITE = process.stdout.write.bind(process.stdout);
const ORIGINAL_CREATE_HASH = crypto.createHash;
const REPORT_DIGEST_PREFIX = '#MIGRATION-REPORT-SHA256:';

let redirectInstalled = false;
let babelInstalled = false;

function assetRoot() {
  return ASSET_ROOT;
}

function nodeModulesDir(repoRoot) {
  return path.join(repoRoot, 'node_modules');
}

function addNodePath(repoRoot) {
  const nm = nodeModulesDir(repoRoot);
  const parts = (process.env.NODE_PATH || '')
    .split(path.delimiter)
    .filter((entry) => entry.length > 0);
  if (!parts.includes(nm)) {
    parts.push(nm);
  }
  const repoNm = path.join(repoRoot, 'packages', 'core', 'node_modules');
  if (fs.existsSync(repoNm) && !parts.includes(repoNm)) {
    parts.push(repoNm);
  }
  process.env.NODE_PATH = parts.join(path.delimiter);
  Module._initPaths();
}

/*
 * Resolve a workspace package to a concrete entry file.
 *
 * The built `main` bundle is preferred when it exists so that every consumer
 * (the integration dispatch, the dependency probes and the spawned Mocha
 * suites) observes a single module instance.  When the distribution has not
 * been built yet the ES `src` entry is used instead and Babel compiles it.
 */
function packageEntry(repoRoot, name, srcRel) {
  const dir = path.join(repoRoot, 'packages', name);
  const manifest = readJsonSafe(path.join(dir, 'package.json'));
  if (manifest && typeof manifest.main === 'string' && manifest.main.length > 0) {
    const mainAbs = path.join(dir, manifest.main);
    if (fs.existsSync(mainAbs)) {
      return mainAbs;
    }
  }
  const srcAbs = path.join(dir, srcRel);
  if (fs.existsSync(srcAbs)) {
    return srcAbs;
  }
  return mainAbs;
}

function installRedirect(repoRoot) {
  if (redirectInstalled) {
    return;
  }
  redirectInstalled = true;
  const map = {
    '@js-joda/core': packageEntry(repoRoot, 'core', path.join('src', 'js-joda.js')),
    '@js-joda/timezone': packageEntry(repoRoot, 'timezone', path.join('src', 'js-joda-timezone.js')),
    '@js-joda/locale': packageEntry(repoRoot, 'locale', path.join('src', 'js-joda-locale.js')),
    '@js-joda/extra': packageEntry(repoRoot, 'extra', path.join('src', 'js-joda-extra.js')),
  };
  Module._resolveFilename = function resolveFilename(request, parent, isMain, options) {
    if (Object.prototype.hasOwnProperty.call(map, request) && fs.existsSync(map[request])) {
      return map[request];
    }
    return ORIGINAL_RESOLVE.call(this, request, parent, isMain, options);
  };
}

/*
 * Isolate the module state between dependency-batch projections.
 *
 * The batches load the repository packages with different entry points (the
 * frozen `src` sources for the differential corpus, the built `dist` bundle
 * for the target postconditions).  Because `require` caches by file path, a
 * package loaded by one batch would otherwise leak into the next with an
 * inconsistent companion core instance and make otherwise-stable projections
 * fail.  Clearing the in-repo modules and reinstating the redirect before each
 * batch gives every projection a clean, consistent module graph.
 */
function resetModuleState(repoRoot) {
  const roots = [
    path.join(repoRoot, 'packages'),
    path.join(ASSET_ROOT, 'shared', 'batches'),
  ];
  for (const key of Object.keys(require.cache)) {
    for (const root of roots) {
      if (key === root || key.indexOf(root + path.sep) === 0) {
        delete require.cache[key];
        break;
      }
    }
  }
  redirectInstalled = false;
  installRedirect(repoRoot);
}

function registerBabel(repoRoot) {
  if (babelInstalled) {
    return;
  }
  babelInstalled = true;
  try {
    const req = createRequire(path.join(repoRoot, 'package.json'));
    const registerModule = req('@babel/register');
    const register = registerModule && registerModule.default ? registerModule.default : registerModule;
    register({
      cwd: repoRoot,
      babelrc: false,
      configFile: false,
      cache: false,
      only: [/packages[\\/][^\\/]+[\\/]src/],
      ignore: [/node_modules/],
      extensions: ['.js'],
      presets: [['@babel/preset-env', { modules: 'commonjs' }]],
    });
  } catch (e) {
    babelInstalled = false;
  }
}

/*
 * Export the module environment to grandchild processes.
 *
 * The dependency batch projections spawn their own Node processes (for
 * example the Mocha suite runner used by BATCH-P2P-0002).  Those processes do
 * not see the in-process module redirect installed above, so `@js-joda/*`
 * imports would fail to resolve and the suite would silently produce no
 * executable projection.  Injecting `bootstrap.js` through NODE_OPTIONS makes
 * every descendant inherit the very same NODE_PATH and `@js-joda/*` redirect.
 */
function exportChildBootstrap() {
  const bootstrap = path.join(__dirname, 'bootstrap.js');
  const option = `--require ${bootstrap}`;
  const current = process.env.NODE_OPTIONS || '';
  if (current.indexOf(bootstrap) === -1) {
    process.env.NODE_OPTIONS = current.length > 0 ? `${current} ${option}` : option;
  }
}

function setupRuntime(repoRoot) {
  process.env.REPO_ROOT = repoRoot;
  addNodePath(repoRoot);
  registerBabel(repoRoot);
  installRedirect(repoRoot);
  exportChildBootstrap();
}

/*
 * Bounded command execution.  Every potentially long command is wrapped with
 * the GNU `timeout` binary and additionally bounded through spawnSync so the
 * harness can never wait indefinitely.
 */
function runBounded(argv, options, timeoutSec) {
  const command = argv[0];
  const args = ['--kill-after=10', String(timeoutSec), command].concat(argv.slice(1));
  return spawnSync('timeout', args, {
    cwd: (options && options.cwd) || process.cwd(),
    encoding: 'utf8',
    maxBuffer: 256 * 1024 * 1024,
    timeout: (timeoutSec + 30) * 1000,
    killSignal: 'SIGKILL',
    env: Object.assign({}, process.env, (options && options.env) || {}),
  });
}

function readJsonSafe(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return null;
  }
}

function writeJson(file, value) {
  const dir = path.dirname(file);
  try {
    ORIGINAL_MKDIR_SYNC.call(fs, dir, { recursive: true });
  } catch (e) {
    /* directory may already exist */
  }
  const text = serializeJson(value);
  ORIGINAL_WRITE_FILE_SYNC.call(fs, file, text);
  return text;
}

/*
 * Serialise a value with the captured ``JSON.stringify`` so a candidate cannot
 * influence the bytes that are written to disk, echoed on stdout and hashed.
 */
function serializeJson(value) {
  return `${ORIGINAL_JSON_STRINGIFY(value, null, 2)}\n`;
}

/*
 * Write evaluator output through the captured stdout writer so a candidate
 * cannot intercept the report or the integrity marker by monkey-patching
 * ``process.stdout.write``.
 */
function writeStdout(text) {
  return ORIGINAL_STDOUT_WRITE(text);
}

/*
 * Emit a content-bound digest of the exact report bytes written to disk.  The
 * verifier compares this marker with the bytes it later reads from the report
 * file: a candidate ``process.on('exit')`` hook can rewrite the file, but it
 * cannot rewrite this already-flushed marker, so any post-write change is
 * detectable.
 */
function emitReportDigest(text) {
  const hash = ORIGINAL_CREATE_HASH.call(crypto, 'sha256');
  hash.update(text, 'utf8');
  const digest = hash.digest('hex');
  ORIGINAL_STDOUT_WRITE(`\n${REPORT_DIGEST_PREFIX}${digest}\n`);
  return digest;
}

function extractJson(text) {
  if (typeof text !== 'string') {
    return null;
  }
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start < 0 || end <= start) {
    return null;
  }
  try {
    return JSON.parse(text.slice(start, end + 1));
  } catch (e) {
    return null;
  }
}

module.exports = {
  assetRoot,
  setupRuntime,
  resetModuleState,
  exportChildBootstrap,
  runBounded,
  readJsonSafe,
  writeJson,
  serializeJson,
  writeStdout,
  emitReportDigest,
  extractJson,
  nodeModulesDir,
};
