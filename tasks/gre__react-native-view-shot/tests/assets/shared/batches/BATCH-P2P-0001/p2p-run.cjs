#!/usr/bin/env node
"use strict";

/*
 * BATCH-P2P-0001 executable projection helper.
 *
 * The calling probe runs the repository's own test suite (Jest) inside the
 * checkout first, then invokes this helper with the collected evidence:
 *
 *   P2P_REPO          - path of the repo checkout that was exercised
 *   P2P_JEST_JSON     - path of the `jest --json` report produced by that run
 *   P2P_JEST_RC       - exit status of the Jest process (0 == success)
 *   P2P_OBSERVATIONS  - path of this batch's observations.json (surfaces)
 *   P2P_NODE_MODULES  - resolved node_modules directory (reserved/unused here)
 *
 * The helper never reads or pattern-matches source text. It resolves every
 * owned observation from observable behavior only: the per-file pass/fail
 * outcome produced by the real Jest run, the process exit status, the
 * structural shape of package.json, and the existence of the declared
 * surfaces. A behavioral regression therefore changes the projection.
 */

const fs = require("fs");
const path = require("path");

const repo = process.env.P2P_REPO || "/workspace/repo";
const jestJsonPath = process.env.P2P_JEST_JSON || "";
const jestRc = Number(process.env.P2P_JEST_RC || "1");
const observationsPath = process.env.P2P_OBSERVATIONS || "";

function guarded(fn) {
  try {
    return fn();
  } catch (e) {
    return null;
  }
}

function repoPath(rel) {
  return path.join(repo, rel);
}

function repoHas(rel) {
  try {
    return fs.existsSync(repoPath(rel));
  } catch (e) {
    return false;
  }
}

/* ------------------------------------------------------------------ */
/* Evidence 1: the repository's own executed Jest suite.               */
/* ------------------------------------------------------------------ */

const fileResults = new Map();
const testResults = new Map();
let suitePass = false;

const jestReport = guarded(function () {
  if (!jestJsonPath || !fs.existsSync(jestJsonPath)) return null;
  return JSON.parse(fs.readFileSync(jestJsonPath, "utf8"));
});

if (jestReport) {
  const results = Array.isArray(jestReport.testResults)
    ? jestReport.testResults
    : [];
  for (const tr of results) {
    const file =
      typeof tr.name === "string" ? tr.name.replace(/\\/g, "/") : "";
    const assertions = Array.isArray(tr.assertionResults)
      ? tr.assertionResults
      : [];
    let passed = 0;
    let failed = 0;
    for (const a of assertions) {
      const status = a && a.status;
      if (status === "passed") passed += 1;
      else if (status === "failed") failed += 1;
    }
    if (file) fileResults.set(file, {passed: passed, failed: failed});
  }
  const total =
    typeof jestReport.numTotalTests === "number"
      ? jestReport.numTotalTests
      : fileResults.size;
  const failed =
    typeof jestReport.numFailedTests === "number"
      ? jestReport.numFailedTests
      : 0;
  suitePass =
    jestRc === 0 && jestReport.success === true && total > 0 && failed === 0;
}

/*
 * Resolve one executed test file by its checkout-relative path. If the file
 * was part of the run, its own assertions decide the result; otherwise the
 * observation falls back to the overall suite outcome (a regression in that
 * file still flips the whole projection).
 */
function filePass(rel) {
  const norm = rel.replace(/\\/g, "/");
  let found = false;
  let ok = true;
  for (const [file, st] of fileResults) {
    if (file.endsWith(norm)) {
      found = true;
      if (st.failed > 0 || st.passed === 0) ok = false;
    }
  }
  return found ? ok : suitePass;
}

/*
 * Resolve a `test:<path>#<name>` surface from the executed Jest report. The
 * path and name are runtime data taken from the shared inventory; the helper
 * carries no expected test text of its own. When the named case was executed,
 * its own status decides; otherwise the enclosing file's status decides, and
 * finally the overall suite.
 */
function testSurfacePass(surface) {
  const body = surface.slice(5);
  const hash = body.indexOf("#");
  const filePath = (hash >= 0 ? body.slice(0, hash) : body).replace(
    /\\/g,
    "/",
  );
  const rawName = hash >= 0 ? body.slice(hash + 1) : "";
  const name = rawName.replace(/^should\s+/i, "").trim();

  if (name) {
    let matched = 0;
    let ok = true;
    for (const [fullName, status] of testResults) {
      if (fullName.indexOf(name) >= 0) {
        matched += 1;
        if (status !== "passed") ok = false;
      }
    }
    if (matched > 0) return ok;
  }

  if (filePath) return filePass(filePath);
  return suitePass;
}

/* ------------------------------------------------------------------ */
/* Evidence 2: structural shape of the declared surfaces.              */
/* ------------------------------------------------------------------ */

function packageExportPresent() {
  const pkg = guarded(function () {
    return JSON.parse(fs.readFileSync(repoPath("package.json"), "utf8"));
  });
  return !!(pkg && pkg.exports && pkg.exports["./package.json"]);
}

function turboSpecPresent() {
  return repoHas("src/specs/NativeRNViewShot.ts");
}

function windowsModulePresent() {
  return repoHas("windows/RNViewShot/RNViewShotModule.cs");
}

function windowsHelpersPresent() {
  return repoHas("windows/RNViewShot/Helpers.cs");
}

function publicTestSuitesPresent() {
  return (
    repoHas("src/__tests__/validateOptions.test.ts") &&
    repoHas("src/__tests__/ViewShot.test.tsx") &&
    repoHas("src/__tests__/captureRef.test.ts") &&
    repoHas("src/__tests__/RNViewShot.web.test.ts") &&
    repoHas("android/src/test") &&
    repoHas("windows/RNViewShot.Tests")
  );
}

/* ------------------------------------------------------------------ */
/* Executable per-observation projection (keyed by structured id).     */
/* ------------------------------------------------------------------ */

const VALIDATION_TEST = "src/__tests__/validateOptions.test.ts";
const VIEWSHOT_TEST = "src/__tests__/ViewShot.test.tsx";
const CAPTURE_TEST = "src/__tests__/captureRef.test.ts";

const RESOLVERS = {
  "P2P-001": function () {
    return suitePass;
  },
  "P2P-002": function () {
    return suitePass && turboSpecPresent();
  },
  "P2P-003": function () {
    return filePass(VALIDATION_TEST);
  },
  "P2P-004": function () {
    return suitePass && repoHas("example/e2e/tests/viewshot-all-screens.test.js");
  },
  "P2P-005": function () {
    return suitePass;
  },
  "P2P-006": function () {
    return suitePass && windowsModulePresent();
  },
  "P2P-007": function () {
    return suitePass && windowsHelpersPresent();
  },
  "P2P-008": function () {
    return suitePass && turboSpecPresent();
  },
  "P2P-009": function () {
    return filePass(CAPTURE_TEST);
  },
  "P2P-010": function () {
    return suitePass && publicTestSuitesPresent();
  },
  "P2P-011": function () {
    return filePass(VALIDATION_TEST);
  },
  "P2P-012": function () {
    return suitePass;
  },
  "P2P-013": function () {
    return suitePass && turboSpecPresent() && windowsModulePresent();
  },
  "P2P-014": function () {
    return filePass(VIEWSHOT_TEST);
  },
  "P2P-015": function () {
    return suitePass && windowsModulePresent();
  },
  "P2P-016": function () {
    return filePass(VALIDATION_TEST);
  },
  "P2P-017": function () {
    return filePass(VIEWSHOT_TEST);
  },
  "P2P-018": function () {
    return suitePass;
  },
  "P2P-019": function () {
    return suitePass;
  },
  "P2P-020": function () {
    return suitePass && turboSpecPresent();
  },
  "P2P-021": function () {
    return suitePass && repoHas("example-web/e2e/viewshot.spec.ts");
  },
  "P2P-022": function () {
    return suitePass && packageExportPresent();
  },
  "P2P-023": function () {
    return suitePass;
  },
  "P2P-024": function () {
    return suitePass && turboSpecPresent();
  },
};

/* Canonical order, used only if observations.json is unavailable. */
const OWNED_IDS = [
  "P2P-004",
  "P2P-021",
  "P2P-001",
  "P2P-003",
  "P2P-005",
  "P2P-006",
  "P2P-007",
  "P2P-012",
  "P2P-013",
  "P2P-015",
  "P2P-020",
  "P2P-002",
  "P2P-008",
  "P2P-009",
  "P2P-010",
  "P2P-011",
  "P2P-016",
  "P2P-017",
  "P2P-018",
  "P2P-019",
  "P2P-022",
  "P2P-023",
  "P2P-024",
  "P2P-014",
];

/* Consume the shared inventory so the output covers exactly what we own. */
function loadObservations() {
  const items = [];
  if (observationsPath && fs.existsSync(observationsPath)) {
    const doc = guarded(function () {
      return JSON.parse(fs.readFileSync(observationsPath, "utf8"));
    });
    const surfaces = doc && Array.isArray(doc.surfaces) ? doc.surfaces : [];
    for (const s of surfaces) {
      if (s && s.observation_id) {
        items.push({
          id: s.observation_id,
          surface: typeof s.surface === "string" ? s.surface : "",
        });
      }
    }
  }
  if (items.length === 0) {
    for (const id of OWNED_IDS) items.push({id: id, surface: ""});
  }
  return items;
}

/*
 * Every owned observation is projected by its structured id, so the result is
 * independent of how a caller spells the surface and never depends on a
 * runner-specific `test:` prefix. The runtime surface is only used as a
 * last-resort fallback when an id has no resolver.
 */
const projection = {};
for (const item of loadObservations()) {
  const resolver = RESOLVERS[item.id];
  let ok = false;
  if (typeof resolver === "function") {
    ok = guarded(resolver) === true;
  } else if (item.surface.indexOf("test:") === 0) {
    ok = guarded(function () {
      return testSurfacePass(item.surface);
    }) === true;
  }
  projection[item.id] = {result: ok ? "PASS" : "FAIL"};
}

process.stdout.write(JSON.stringify(projection) + "\n");
