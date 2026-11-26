'use strict';

/*
 * Shared module loader for the BATCH-P2P-0001 state-preservation harness.
 *
 * It registers @babel/register with an explicit CommonJS transform so the ESM
 * sources of every workspace package can be required from a single process even
 * when the current working directory is a nested package, and it aliases every
 * in-repo `@js-joda/*` package to its local `src` entry so the repository's own
 * tests run against this checkout instead of a prebuilt `dist` bundle.
 */

const path = require('path');
const Module = require('module');

const repo = process.env.P2P_REPO || process.cwd();

function babelRegisterPath() {
    return path.join(repo, 'node_modules', '@babel', 'register');
}

try {
    require(babelRegisterPath())({
        babelrc: false,
        configFile: false,
        cwd: repo,
        presets: [['@babel/preset-env', { modules: 'commonjs' }]],
        extensions: ['.js'],
    });
} catch (e) {
    /* a pre-registered loader is acceptable */
}

const alias = {
    '@js-joda/core': path.join(repo, 'packages', 'core', 'src', 'js-joda.js'),
    '@js-joda/locale': path.join(repo, 'packages', 'locale', 'src', 'js-joda-locale.js'),
    '@js-joda/timezone': path.join(repo, 'packages', 'timezone', 'src', 'js-joda-timezone.js'),
    '@js-joda/extra': path.join(repo, 'packages', 'extra', 'src', 'js-joda-extra.js'),
};

if (!Module.__p2pAliased) {
    const originalResolve = Module._resolveFilename;
    Module._resolveFilename = function (request) {
        if (Object.prototype.hasOwnProperty.call(alias, request)) {
            return require.resolve(alias[request]);
        }
        return originalResolve.apply(this, arguments);
    };
    Module.__p2pAliased = true;
}

module.exports = { alias, repo };
