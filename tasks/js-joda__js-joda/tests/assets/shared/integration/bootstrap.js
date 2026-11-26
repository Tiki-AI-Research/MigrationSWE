'use strict';

/*
 * Process bootstrap for integration-dispatched child processes.
 *
 * It is injected through NODE_OPTIONS=--require so that any batch executable
 * spawned by the integration harness (and the grandchild test processes it
 * spawns) observes the same module environment as the in-process dispatch:
 * the workspace `node_modules` on NODE_PATH and bare `@js-joda/*` specifiers
 * mapped to the in-repo entry files.  This keeps the dependency batch
 * projections runnable on a fresh workspace whose package links are absent.
 */

const fs = require('fs');
const path = require('path');
const Module = require('module');

const repoRoot = process.env.REPO_ROOT || process.env.MIGRATION_REPO_ROOT;

if (repoRoot && !process.env.INTEGRATION_BOOTSTRAPPED) {
  process.env.INTEGRATION_BOOTSTRAPPED = '1';
  process.env.REPO_ROOT = repoRoot;

  const nodeModules = path.join(repoRoot, 'node_modules');
  const parts = (process.env.NODE_PATH || '')
    .split(path.delimiter)
    .filter((entry) => entry.length > 0);
  for (const entry of [nodeModules, path.join(repoRoot, 'packages', 'core', 'node_modules')]) {
    if (!parts.includes(entry)) {
      parts.push(entry);
    }
  }
  process.env.NODE_PATH = parts.join(path.delimiter);
  Module._initPaths();

  const entry = (name, srcRel) => {
    const dir = path.join(repoRoot, 'packages', name);
    let main = null;
    try {
      main = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8')).main;
    } catch (e) {
      main = null;
    }
    if (typeof main === 'string' && main.length > 0 && fs.existsSync(path.join(dir, main))) {
      return path.join(dir, main);
    }
    return path.join(dir, srcRel);
  };
  const map = {
    '@js-joda/core': entry('core', path.join('src', 'js-joda.js')),
    '@js-joda/timezone': entry('timezone', path.join('src', 'js-joda-timezone.js')),
    '@js-joda/locale': entry('locale', path.join('src', 'js-joda-locale.js')),
    '@js-joda/extra': entry('extra', path.join('src', 'js-joda-extra.js')),
  };
  const originalResolve = Module._resolveFilename;
  Module._resolveFilename = function resolveFilename(request, parent, isMain, options) {
    if (Object.prototype.hasOwnProperty.call(map, request) && fs.existsSync(map[request])) {
      return map[request];
    }
    return originalResolve.call(this, request, parent, isMain, options);
  };

  try {
    const { createRequire } = require('module');
    const req = createRequire(path.join(repoRoot, 'package.json'));
    const registerModule = req('@babel/register');
    const register = registerModule && registerModule.default ? registerModule.default : registerModule;
    const escapedRoot = repoRoot.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    register({
      cwd: repoRoot,
      babelrc: false,
      configFile: false,
      cache: false,
      only: [new RegExp(`^${escapedRoot}[\\\\/]`), new RegExp(`^${escapedRoot}$`)],
      ignore: [/[\\/]node_modules[\\/]/],
      extensions: ['.js'],
      presets: [['@babel/preset-env', { modules: 'commonjs' }]],
    });
  } catch (e) {
    /* Babel is optional; probes degrade to INCONCLUSIVE without it. */
  }
}
