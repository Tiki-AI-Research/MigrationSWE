"use strict";

// Target-side entry for BATCH-F2P-0003.
//
// Runs the shared probe engine against the migrated repository (authored
// against the private reference target at /input/target). The repository root
// can be overridden with argv[2] or MUSHROOM_TARGET_REPO so the INTEGRATION
// runner can point it at the candidate solution /workspace/repo. It reports the
// raw per-observation predicate result for the repository it is given; it never
// hardcodes PASS by role.

const path = require("path");

const { evaluate } = require(
  path.join(__dirname, "../../../shared/batches/BATCH-F2P-0003/probe.cjs")
);

const repoRoot =
  process.argv[2] || process.env.MUSHROOM_TARGET_REPO || "/input/target";

process.stdout.write(JSON.stringify(evaluate(repoRoot), null, 2) + "\n");
