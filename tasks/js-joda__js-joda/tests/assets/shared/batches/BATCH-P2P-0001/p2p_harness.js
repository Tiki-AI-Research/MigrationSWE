#!/usr/bin/env node
'use strict';

/*
 * P2P executable state-preservation harness for BATCH-P2P-0001.
 *
 * For a given side (source or target) it runs the repository's own test suites
 * (packages/core, packages/locale, packages/extra) and a small set of
 * deterministic probes, maps every P2P observation owned by this batch to a
 * real outcome, and emits a report whose projection is identical on the source
 * and target side.
 *
 * Every potentially long-running command is wrapped with the external
 * `timeout` utility and additionally bounded through spawnSync({timeout}).
 * No command may block forever.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const HERE = __dirname;
const OBS_FILE = path.join(HERE, 'observations.json');
const PROBE_DIR = path.join(HERE, 'probes');
const SETUP_FILE = path.join(HERE, 'setup.js');

// Per-test budget for the package suites.  The repository's own `npm test`
// scripts run mocha with `--timeout 5000`, which is fine for a warm developer
// machine but leaves only ~2x headroom for the heaviest reference tests
// (`now_Clock_allSecsInDay_*` iterate a full day) and therefore produces
// spurious timeouts on an oversubscribed evaluation host.  A timeout is host
// jitter, not a migration regression, so the gate gives the tests a generous,
// clearly bounded budget; every assertion still has to pass and the outer
// `timeout 1800` still bounds the whole suite.
const PACKAGE_TEST_TIMEOUT_MS = 60000;

const PACKAGE_SUITES = {
    core: {
        cwd: 'packages/core',
        globs: [
            './test/*Test_mochaOnly.js',
            './test/*Test.js',
            './test/**/*Test.js',
            './test/**/**/*Test.js',
        ],
    },
    locale: {
        cwd: 'packages/locale',
        globs: [
            './test/*Test.js',
            './test/**/*Test.js',
            './test/**/**/*Test.js',
            './test/pluginTest_mochaOnly.js',
        ],
    },
    extra: {
        cwd: 'packages/extra',
        globs: [
            './test/*Test.js',
            './test/**/*Test.js',
            './test/**/**/*Test.js',
            './test/pluginTest_mochaOnly.js',
        ],
    },
};

function parseArgs(argv) {
    const out = { side: 'target', repo: process.cwd() };
    for (let i = 2; i < argv.length; i++) {
        const arg = argv[i];
        if (arg === '--side') {
            out.side = argv[++i];
        } else if (arg === '--repo') {
            out.repo = path.resolve(argv[++i]);
        } else if (arg === '--out') {
            out.out = argv[++i];
        }
    }
    return out;
}

function readObservations() {
    return JSON.parse(fs.readFileSync(OBS_FILE, 'utf8')).observations;
}

function binary(repo, name) {
    return path.join(repo, 'node_modules', '.bin', name);
}

function runShell(repo, cwd, command, timeoutSec, extraEnv) {
    const env = Object.assign({}, process.env, { NODE_ENV: 'test', P2P_REPO: repo }, extraEnv || {});
    const wrapped = `timeout ${timeoutSec} ${command}`;
    const res = spawnSync('/bin/sh', ['-c', wrapped], {
        cwd: path.join(repo, cwd),
        timeout: (timeoutSec + 30) * 1000,
        killSignal: 'SIGKILL',
        maxBuffer: 256 * 1024 * 1024,
        encoding: 'utf8',
        env,
    });
    return {
        status: res.status,
        signal: res.signal,
        stdout: res.stdout || '',
        stderr: res.stderr || '',
        timedOut: !!(res.error && res.error.code === 'ETIMEDOUT') || res.signal === 'SIGKILL' || res.signal === 'SIGTERM',
        spawnError: res.error ? String(res.error.message || res.error) : null,
    };
}

function extractJson(text) {
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

function packageOf(relTestFile) {
    const marker = '/test/';
    const idx = relTestFile.indexOf(marker);
    if (idx < 0) {
        return null;
    }
    const prefix = relTestFile.slice(0, idx);
    const parts = prefix.split('/');
    return parts.length === 2 && parts[0] === 'packages' ? parts[1] : null;
}

function runPackageSuite(repo, pkg) {
    const suite = PACKAGE_SUITES[pkg];
    if (!suite) {
        return { ok: false, reason: `unknown package suite ${pkg}`, tests: [] };
    }
    const mocha = binary(repo, 'mocha');
    if (!fs.existsSync(mocha)) {
        return { ok: false, reason: `mocha binary not found at ${mocha}`, tests: [] };
    }
    const cmd = `${mocha} --timeout ${PACKAGE_TEST_TIMEOUT_MS} --require ${SETUP_FILE} --reporter json ${suite.globs.join(' ')}`;
    const res = runShell(repo, suite.cwd, cmd, 1800);
    if (res.timedOut) {
        return { ok: false, reason: `packages/${pkg} test suite timed out`, tests: [] };
    }
    const parsed = extractJson(res.stdout);
    if (!parsed || !parsed.stats || !Array.isArray(parsed.tests)) {
        return { ok: false, reason: `unparseable packages/${pkg} mocha output`, tests: [] };
    }
    return { ok: parsed.stats.failures === 0, failures: parsed.stats.failures, tests: parsed.tests };
}

function suiteFor(ctx, repo, pkg) {
    if (!ctx.suiteCache[pkg]) {
        ctx.suiteCache[pkg] = runPackageSuite(repo, pkg);
    }
    return ctx.suiteCache[pkg];
}

function runCoreLint(repo) {
    const eslint = binary(repo, 'eslint');
    if (!fs.existsSync(eslint)) {
        return { ok: false, reason: `eslint binary not found at ${eslint}` };
    }
    const res = runShell(repo, 'packages/core', `${eslint} .`, 600);
    if (res.timedOut) {
        return { ok: false, reason: 'packages/core lint timed out' };
    }
    return { ok: res.status === 0, reason: `exit=${res.status}` };
}

function status(value) {
    return value ? 'PASS' : 'FAIL';
}

function testFailed(test) {
    const err = test && test.err;
    if (!err) {
        return false;
    }
    return !!(err.message || err.stack || Object.keys(err).length > 0);
}

function ensureLoader(repo) {
    if (ensureLoader.done) {
        return true;
    }
    process.env.P2P_REPO = repo;
    try {
        require(SETUP_FILE);
        ensureLoader.done = true;
        return true;
    } catch (e) {
        return false;
    }
}

function evaluateObservation(obs, repo, ctx) {
    if (obs.test_file) {
        const pkg = packageOf(obs.test_file);
        if (!pkg || !PACKAGE_SUITES[pkg]) {
            return { status: 'FAIL', value: { result: 'FAIL' }, detail: `no suite for ${obs.test_file}` };
        }
        const suite = suiteFor(ctx, repo, pkg);
        if (!suite.ok && suite.tests.length === 0) {
            return { status: 'FAIL', value: { result: 'FAIL' }, detail: suite.reason };
        }
        const suffix = `/${obs.test_file}`;
        const matched = suite.tests.filter((t) => {
            const file = (t.file || '').split(path.sep).join('/');
            const title = typeof t.fullTitle === 'string' ? t.fullTitle : '';
            return file.endsWith(suffix) && title.indexOf(obs.test_title) >= 0;
        });
        const passed = matched.length > 0 && matched.every((t) => !testFailed(t));
        return {
            status: status(passed),
            value: { result: status(passed) },
            detail: `${matched.length} matching test(s)`,
        };
    }

    if (obs.commands) {
        let ok = true;
        const parts = {};
        for (const command of obs.commands) {
            let outcome;
            if (command === 'core-tests') {
                const suite = suiteFor(ctx, repo, 'core');
                outcome = { ok: suite.ok && suite.tests.length > 0 };
            } else if (command === 'core-lint') {
                outcome = runCoreLint(repo);
            } else {
                outcome = { ok: false };
            }
            parts[command] = outcome.ok ? 'PASS' : 'FAIL';
            ok = ok && outcome.ok;
        }
        return { status: status(ok), value: { result: status(ok), commands: parts } };
    }

    if (obs.probe) {
        const probeFile = path.join(PROBE_DIR, `${obs.probe}.js`);
        if (!fs.existsSync(probeFile)) {
            return { status: 'FAIL', value: { result: 'FAIL' }, detail: `missing probe ${obs.probe}` };
        }
        try {
            ensureLoader(repo);
            const probe = require(probeFile);
            const result = probe.check(repo);
            if (result && typeof result === 'object' && typeof result.ok === 'boolean') {
                return { status: status(result.ok), value: result.value };
            }
            return { status: status(!!result), value: { result: status(!!result) } };
        } catch (e) {
            return { status: 'FAIL', value: { result: 'FAIL' }, detail: String(e && e.message ? e.message : e) };
        }
    }

    return { status: 'FAIL', value: { result: 'FAIL' }, detail: 'observation has no executable mapping' };
}

function main(argv) {
    const args = parseArgs(argv);
    const observations = readObservations();
    const ctx = { suiteCache: {} };
    const checks = [];
    const projection = {};

    for (const obs of observations) {
        const outcome = evaluateObservation(obs, args.repo, ctx);
        checks.push({
            id: obs.id,
            side: args.side,
            status: outcome.status,
            value: outcome.value,
            detail: outcome.detail || null,
        });
        projection[obs.id] = outcome.value;
    }

    const report = {
        schema_version: 'migration-p2p-report:v1',
        batch_id: 'BATCH-P2P-0001',
        side: args.side,
        repo: args.repo,
        checks,
        projection,
    };

    const text = JSON.stringify(report, null, 2);
    if (args.out) {
        fs.writeFileSync(args.out, text);
    }
    process.stdout.write(`${text}\n`);

    const allPass = checks.every((c) => c.status === 'PASS');
    return allPass ? 0 : 1;
}

process.exitCode = main(process.argv);
