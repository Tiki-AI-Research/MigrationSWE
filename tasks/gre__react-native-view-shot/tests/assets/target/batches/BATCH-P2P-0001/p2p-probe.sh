#!/bin/sh
# BATCH-P2P-0001 target-side executable P2P probe.
#
# P2P is executable state preservation: this probe runs the repository's own
# test suite (Jest) inside the target checkout and maps every observation
# owned by the batch, keyed by observation id, to the raw result of that run.
# The projection is derived from the real execution (Jest report + structural
# checks) and is never hardcoded per role, so a behavioral regression changes
# it. No surface is addressed with a runner-specific `test:` prefix, so every
# observation is projected by this probe itself.
#
# There are no privileged requirements and no blocking/interactive cases:
# `--ci` disables watch mode and `--runInBand` avoids process-pool stalls.
# Every potentially long-running command is wrapped in an explicit `timeout`
# so the harness can never wait forever.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ASSET_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)
HELPER="$ASSET_ROOT/shared/batches/BATCH-P2P-0001/p2p-run.cjs"
OBSERVATIONS="$ASSET_ROOT/shared/batches/BATCH-P2P-0001/observations.json"

REPO="${REPO:-/workspace/repo}"
if [ ! -d "$REPO/src" ]; then
  for cand in /input/target "$ASSET_ROOT" /workspace/repo; do
    if [ -d "$cand/src" ]; then
      REPO="$cand"
      break
    fi
  done
fi

TIMEOUT_SECS="${P2P_TIMEOUT_SECS:-900}"

NODE_MODULES=""
for cand in "$REPO/node_modules" "$ASSET_ROOT/node_modules" /opt/migration/dependencies/node_modules; do
  if [ -d "$cand/jest/bin" ]; then
    NODE_MODULES="$cand"
    break
  fi
done

JEST_JSON="${P2P_JEST_JSON:-/tmp/p2p-target-jest.$$.json}"
JEST_LOG="${P2P_JEST_LOG:-/tmp/p2p-target-jest.$$.log}"
JEST_RC=1

if [ -n "$NODE_MODULES" ] && [ -f "$NODE_MODULES/jest/bin/jest.js" ]; then
  set +e
  ( cd "$REPO" && NODE_PATH="$NODE_MODULES" CI=true \
      timeout "$TIMEOUT_SECS" node "$NODE_MODULES/jest/bin/jest.js" \
      --ci --runInBand --json --outputFile="$JEST_JSON" ) \
    >"$JEST_LOG" 2>&1
  JEST_RC=$?
  set -e
fi

if [ -f "$HELPER" ]; then
  P2P_REPO="$REPO" \
  P2P_JEST_JSON="$JEST_JSON" \
  P2P_JEST_RC="$JEST_RC" \
  P2P_OBSERVATIONS="$OBSERVATIONS" \
  P2P_NODE_MODULES="$NODE_MODULES" \
    timeout "$TIMEOUT_SECS" node "$HELPER"
  exit 0
fi

P2P_REPO="$REPO" \
P2P_JEST_JSON="$JEST_JSON" \
P2P_JEST_RC="$JEST_RC" \
P2P_OBSERVATIONS="$OBSERVATIONS" \
  timeout "$TIMEOUT_SECS" node - <<'NODE'
"use strict";
const fs = require("fs");
const path = require("path");
const repo = process.env.P2P_REPO || "/workspace/repo";
const jestJsonPath = process.env.P2P_JEST_JSON || "";
const jestRc = Number(process.env.P2P_JEST_RC || "1");
const observationsPath = process.env.P2P_OBSERVATIONS || "";
const OWNED_IDS = [
  "P2P-004", "P2P-021", "P2P-001", "P2P-003", "P2P-005", "P2P-006",
  "P2P-007", "P2P-012", "P2P-013", "P2P-015", "P2P-020", "P2P-002",
  "P2P-008", "P2P-009", "P2P-010", "P2P-011", "P2P-016", "P2P-017",
  "P2P-018", "P2P-019", "P2P-022", "P2P-023", "P2P-024", "P2P-014"
];
function guarded(fn) { try { return fn(); } catch (e) { return null; } }
function repoHas(rel) {
  try { return fs.existsSync(path.join(repo, rel)); } catch (e) { return false; }
}
const fileResults = new Map();
let suitePass = false;
const report = guarded(function () {
  if (!jestJsonPath || !fs.existsSync(jestJsonPath)) return null;
  return JSON.parse(fs.readFileSync(jestJsonPath, "utf8"));
});
if (report) {
  const results = Array.isArray(report.testResults) ? report.testResults : [];
  for (const tr of results) {
    const file = typeof tr.name === "string" ? tr.name.replace(/\\/g, "/") : "";
    const assertions = Array.isArray(tr.assertionResults) ? tr.assertionResults : [];
    let passed = 0; let failed = 0;
    for (const a of assertions) {
      if (a && a.status === "passed") passed += 1;
      else if (a && a.status === "failed") failed += 1;
    }
    if (file) fileResults.set(file, {passed: passed, failed: failed});
  }
  const total = typeof report.numTotalTests === "number" ? report.numTotalTests : fileResults.size;
  const failed = typeof report.numFailedTests === "number" ? report.numFailedTests : 0;
  suitePass = jestRc === 0 && report.success === true && total > 0 && failed === 0;
}
function filePass(rel) {
  const norm = rel.replace(/\\/g, "/");
  let found = false; let ok = true;
  for (const [file, st] of fileResults) {
    if (file.endsWith(norm)) {
      found = true;
      if (st.failed > 0 || st.passed === 0) ok = false;
    }
  }
  return found ? ok : suitePass;
}
function packageExportPresent() {
  const pkg = guarded(function () {
    return JSON.parse(fs.readFileSync(path.join(repo, "package.json"), "utf8"));
  });
  return !!(pkg && pkg.exports && pkg.exports["./package.json"]);
}
const VALIDATION_TEST = "src/__tests__/validateOptions.test.ts";
const VIEWSHOT_TEST = "src/__tests__/ViewShot.test.tsx";
const CAPTURE_TEST = "src/__tests__/captureRef.test.ts";
const RESOLVERS = {
  "P2P-001": function () { return suitePass; },
  "P2P-002": function () { return suitePass && repoHas("src/specs/NativeRNViewShot.ts"); },
  "P2P-003": function () { return filePass(VALIDATION_TEST); },
  "P2P-004": function () { return suitePass && repoHas("example/e2e/tests/viewshot-all-screens.test.js"); },
  "P2P-005": function () { return suitePass; },
  "P2P-006": function () { return suitePass && repoHas("windows/RNViewShot/RNViewShotModule.cs"); },
  "P2P-007": function () { return suitePass && repoHas("windows/RNViewShot/Helpers.cs"); },
  "P2P-008": function () { return suitePass && repoHas("src/specs/NativeRNViewShot.ts"); },
  "P2P-009": function () { return filePass(CAPTURE_TEST); },
  "P2P-010": function () {
    return suitePass &&
      repoHas("src/__tests__/validateOptions.test.ts") &&
      repoHas("src/__tests__/ViewShot.test.tsx") &&
      repoHas("src/__tests__/captureRef.test.ts") &&
      repoHas("src/__tests__/RNViewShot.web.test.ts") &&
      repoHas("android/src/test") && repoHas("windows/RNViewShot.Tests");
  },
  "P2P-011": function () { return filePass(VALIDATION_TEST); },
  "P2P-012": function () { return suitePass; },
  "P2P-013": function () {
    return suitePass && repoHas("src/specs/NativeRNViewShot.ts") && repoHas("windows/RNViewShot/RNViewShotModule.cs");
  },
  "P2P-014": function () { return filePass(VIEWSHOT_TEST); },
  "P2P-015": function () { return suitePass && repoHas("windows/RNViewShot/RNViewShotModule.cs"); },
  "P2P-016": function () { return filePass(VALIDATION_TEST); },
  "P2P-017": function () { return filePass(VIEWSHOT_TEST); },
  "P2P-018": function () { return suitePass; },
  "P2P-019": function () { return suitePass; },
  "P2P-020": function () { return suitePass && repoHas("src/specs/NativeRNViewShot.ts"); },
  "P2P-021": function () { return suitePass && repoHas("example-web/e2e/viewshot.spec.ts"); },
  "P2P-022": function () { return suitePass && packageExportPresent(); },
  "P2P-023": function () { return suitePass; },
  "P2P-024": function () { return suitePass && repoHas("src/specs/NativeRNViewShot.ts"); }
};
function loadIds() {
  const ids = [];
  if (observationsPath && fs.existsSync(observationsPath)) {
    const doc = guarded(function () {
      return JSON.parse(fs.readFileSync(observationsPath, "utf8"));
    });
    const surfaces = doc && Array.isArray(doc.surfaces) ? doc.surfaces : [];
    for (const s of surfaces) {
      if (s && s.observation_id) ids.push(s.observation_id);
    }
  }
  return ids.length > 0 ? ids : OWNED_IDS;
}
const projection = {};
for (const id of loadIds()) {
  const resolver = RESOLVERS[id];
  projection[id] = {result: (typeof resolver === "function" && guarded(resolver) === true) ? "PASS" : "FAIL"};
}
process.stdout.write(JSON.stringify(projection) + "\n");
NODE
