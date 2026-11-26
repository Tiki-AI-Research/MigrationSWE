#!/usr/bin/env node
"use strict";

// Target-side probe entry for BATCH-F2P-0002.
//
// Usage: node run-probes.cjs [repoRoot] [--report <path>] [--verbose]
//                     [--with-typecheck]
// The target checkout defaults to /input/target; on the target side every F2P
// observation is expected to be PASS (the 0.85.0 runtime satisfies the target
// postcondition while the same predicate fails on the source runtime).
//
// The entry evaluates each observation with its own predicate; it never
// substitutes a single global package-existence check for distinct entries.

const fs = require("fs");
const path = require("path");
const core = require(
  path.resolve(__dirname, "../../../shared/batches/BATCH-F2P-0002/runner-core.cjs"),
);

function parseArgs(argv) {
  const out = {
    root: "/input/target",
    report: null,
    verbose: false,
    withTypecheck: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--report") out.report = argv[++i];
    else if (a === "--verbose") out.verbose = true;
    else if (a === "--with-typecheck") out.withTypecheck = true;
    else if (!a.startsWith("--")) out.root = a;
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
const report = core.runSide("target", args.root, { verbose: args.verbose });

// Optional, explicit, timeout-wrapped type-check. Idiomatic `npm run` commands
// are the only long-running children and are bounded by the coreutil timeout.
if (args.withTypecheck) {
  report.typecheck = core.runWithTimeout("npm", ["run", "type-check"], {
    cwd: args.root,
    timeoutSec: 300,
  });
}

if (args.report) {
  fs.mkdirSync(path.dirname(args.report), { recursive: true });
  fs.writeFileSync(args.report, JSON.stringify(report, null, 2));
} else {
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
}

// Exit non-zero when any observation did not observe the expected target PASS.
process.exit(report.failed === 0 ? 0 : 1);
