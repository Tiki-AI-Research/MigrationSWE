#!/usr/bin/env node
'use strict';

// Target-side (Fastify 4.28.1) integration runner.
//
// Usage: node target/runner.js [repo-root]
// The repo root otherwise comes from MIGRATION_REPO_ROOT / MIGRATION_REPO /
// REPO_ROOT / REPO. The migration-observations:v6 report is written to fd 1 by
// the shared runner core.

require('../shared/integration/main').run('target').then(
  () => process.exit(0),
  (err) => {
    process.stderr.write('target runner error: ' + String((err && err.stack) || err) + '\n');
    process.exit(1);
  },
);
