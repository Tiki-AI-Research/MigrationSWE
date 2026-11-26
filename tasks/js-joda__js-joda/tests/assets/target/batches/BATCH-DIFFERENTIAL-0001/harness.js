#!/usr/bin/env node
'use strict';

/*
 * Target harness for BATCH-DIFFERENTIAL-0001.
 *
 * Usage: node harness.js <report-path> [repo-root]
 *
 * Executes the shared differential corpus against the migrated workspace
 * (default `/workspace/repo`), compares the normalized results with the
 * frozen reference fixture and writes a JSON report.  Every potentially long
 * running command is bounded by the GNU `timeout` binary inside the corpus;
 * this wrapper never waits indefinitely.
 */

const fs = require('fs');
const path = require('path');

function findCorpus(startDir) {
    let dir = startDir;
    for (let i = 0; i < 8; i += 1) {
        const candidates = [
            path.join(dir, 'shared', 'batches', 'BATCH-DIFFERENTIAL-0001', 'corpus.js'),
            path.join(dir, 'batches', 'BATCH-DIFFERENTIAL-0001', 'corpus.js'),
            path.join(dir, 'corpus.js'),
        ];
        for (const candidate of candidates) {
            if (fs.existsSync(candidate)) {
                return candidate;
            }
        }
        const parent = path.dirname(dir);
        if (parent === dir) {
            break;
        }
        dir = parent;
    }
    return null;
}

function findExpected(startDir) {
    let dir = startDir;
    for (let i = 0; i < 8; i += 1) {
        const candidates = [
            path.join(dir, 'fixtures', 'batches', 'BATCH-DIFFERENTIAL-0001', 'expected.json'),
            path.join(dir, 'batches', 'BATCH-DIFFERENTIAL-0001', 'expected.json'),
            path.join(dir, 'expected.json'),
        ];
        for (const candidate of candidates) {
            if (fs.existsSync(candidate)) {
                return candidate;
            }
        }
        const parent = path.dirname(dir);
        if (parent === dir) {
            break;
        }
        dir = parent;
    }
    return null;
}

function main(argv) {
    const reportPath = argv[2] || '/logs/result.json';
    const repoRoot = argv[3] || process.env.REPO_ROOT || '/workspace/repo';

    const corpusPath = findCorpus(__dirname);
    if (!corpusPath) {
        return fail(reportPath, 'corpus-not-found');
    }
    const corpus = require(corpusPath);

    let report;
    try {
        const actual = corpus.runCorpus(repoRoot, { execute: true });
        const expectedPath = findExpected(__dirname);
        let differences = [];
        let expected = null;
        if (expectedPath) {
            expected = JSON.parse(fs.readFileSync(expectedPath, 'utf8'));
            differences = corpus.diffReports(expected, actual);
        }
        const failed = Object.keys(actual.observations).filter(
            (id) => actual.observations[id].passed === false
        );
        report = {
            schema_version: 'migration-differential-report:v1',
            batch_id: corpus.BATCH_ID,
            side: 'target',
            repo_root: repoRoot,
            expected_path: expectedPath,
            expected_present: expected !== null,
            observations: actual.observations,
            failed_observations: failed,
            differences,
            passed: failed.length === 0 && differences.length === 0,
        };
    } catch (e) {
        return fail(reportPath, `${(e && e.constructor && e.constructor.name) || 'Error'}: ${e && e.message}`);
    }

    writeReport(reportPath, report);
    return report.passed ? 0 : 1;
}

function fail(reportPath, detail) {
    writeReport(reportPath, {
        schema_version: 'migration-differential-report:v1',
        batch_id: 'BATCH-DIFFERENTIAL-0001',
        side: 'target',
        passed: false,
        error: detail,
        observations: {},
        failed_observations: [],
        differences: [],
    });
    return 1;
}

function writeReport(reportPath, report) {
    try {
        fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    } catch (e) {
        /* directory may already exist */
    }
    fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
    process.stdout.write(`${JSON.stringify({ passed: report.passed, failed: report.failed_observations })}\n`);
}

process.exitCode = main(process.argv);
