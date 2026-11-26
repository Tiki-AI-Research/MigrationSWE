#!/usr/bin/env node
// Source-side replay of the frozen observation inventory.
//
// The frozen `assets/source-baseline-outputs.json` records the pre-migration
// outcome of every declared observation (P2P passes, F2P fails).  This entry
// point reproduces those rows against a checkout of the base commit so the
// recorded provenance (commit, tree digest, command, exit code, node results and
// observation digest) can be re-derived instead of trusted:
//
//   node assets/source/run.mjs --repo <source tree at base commit> \
//        --report <report.json>
//
// It shares the batch probes with the target side; the probes are parameterised
// by `side`, which is why `run-integration.mjs` may fall back to the target
// batch directory when a source-specific copy is not shipped.
import { main } from '../shared/integration/run-integration.mjs';

const code = await main({ side: 'source', argv: process.argv.slice(2) });
process.exitCode = code;
