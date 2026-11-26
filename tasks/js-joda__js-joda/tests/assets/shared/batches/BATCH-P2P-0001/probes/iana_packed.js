'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

/*
 * Executable projection for the packaged IANA time zone data (P2P-022 / R029).
 *
 * R029 forbids the version upgrade from modifying anything under
 * packages/timezone/data/packed/**. The digest below is computed from the
 * unmodified baseline data (it is identical in the 6.1.0 source checkout and
 * the 6.3.2 target checkout), so both sides observe the same concrete
 * projection: the fixed digest. A migration that rewrites, adds or removes a
 * packed file changes the digest and this observation fails on that side.
 *
 * The expected value is embedded here on purpose: the harness must be
 * self-contained and must not depend on an asset fixture being present inside
 * the migrated repository at run time.
 */

const EXPECTED_PACKED_DIGEST = '84af88305c42339a85a1788605043512c509c64198928f9d6bc23eadc960536c';

function digestPacked(repo) {
    const root = path.join(repo, 'packages', 'timezone', 'data', 'packed');
    if (!fs.existsSync(root)) {
        return null;
    }
    const files = [];
    (function walk(dir) {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
            const full = path.join(dir, entry.name);
            if (entry.isDirectory()) {
                walk(full);
            } else if (entry.isFile()) {
                files.push(full);
            }
        }
    })(root);
    files.sort();
    const lines = files.map((file) => {
        const rel = path.relative(root, file).split(path.sep).join('/');
        const sha = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
        return `${rel}:${sha}`;
    });
    return crypto.createHash('sha256').update(lines.join('\n')).digest('hex');
}

function check(repo) {
    const digest = digestPacked(repo);
    const ok = digest !== null && digest === EXPECTED_PACKED_DIGEST;
    return { ok, value: { result: ok ? 'PASS' : 'FAIL' } };
}

module.exports = { check };
