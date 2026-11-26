"use strict";

/*
 * BATCH-INTEGRATION-0001 shared harness core.
 *
 * The INTEGRATION batch merges the dependency batches and owns the two live
 * harness entries (source/runner and target/runner.js). This module is the
 * shared implementation both entries delegate to. It runs every required
 * runtime observation through the real batch probes that already exist in the
 * published asset tree:
 *
 *   - P2P/DIFFERENTIAL  : the shared DIFFERENTIAL adapters (Superstruct
 *                         validate on the source side, Zod safeParse on the
 *                         target side) over a combined shared-case corpus.
 *   - F2P-0001/0002/0003: the three shared semantic predicate engines.
 *
 * Nothing is hardcoded by side: the same predicates are evaluated against the
 * repository the runner is pointed at and the raw observation is reported.
 *
 * At runtime the asset root only contains shared/, source/, target/ and
 * fixtures/ (plus the entries). This file therefore reads only those merged
 * paths and never the authoring-only dependencies/ or fragments/ trees.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const ASSETS_ROOT = path.resolve(__dirname, "..", "..");
const OBSERVATIONS_PATH = path.join(
  ASSETS_ROOT,
  "shared",
  "integration",
  "observations.json"
);
const DIFFERENTIAL_BATCH_CORPUS = path.join(
  ASSETS_ROOT,
  "fixtures",
  "batches",
  "BATCH-DIFFERENTIAL-0001",
  "fixtures.json"
);
const DIFFERENTIAL_EXTRAS = path.join(
  ASSETS_ROOT,
  "fixtures",
  "integration",
  "differential-extras.json"
);
const P2P_CASES = path.join(
  ASSETS_ROOT,
  "fixtures",
  "integration",
  "p2p-cases.json"
);

const PROBE_TIMEOUT_MS = 600000;

function resolveRepoRoot() {
  const keys = ["MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"];
  for (const key of keys) {
    const value = process.env[key];
    if (value && value.trim()) return path.resolve(value.trim());
  }
  return "/workspace/repo";
}

function depDirCandidates(repoRoot) {
  const candidates = [
    path.join(repoRoot, "node_modules"),
    process.env.DEPS_NODE_MODULES,
    process.env.MUSHROOM_DEPS,
    "/opt/migration/dependencies/node_modules",
  ];
  const seen = [];
  for (const candidate of candidates) {
    if (!candidate) continue;
    const resolved = path.resolve(candidate);
    if (seen.includes(resolved)) continue;
    try {
      if (fs.statSync(resolved).isDirectory()) seen.push(resolved);
    } catch (_) {
      /* not present */
    }
  }
  return seen;
}

function childEnv(repoRoot) {
  const dirs = depDirCandidates(repoRoot);
  const env = Object.assign({}, process.env);
  const nodePath = [path.join(repoRoot, "node_modules")]
    .concat(dirs)
    .concat(env.NODE_PATH ? env.NODE_PATH.split(path.delimiter) : [])
    .filter(Boolean);
  env.NODE_PATH = Array.from(new Set(nodePath)).join(path.delimiter);
  env.DEPS_NODE_MODULES = dirs[0] || path.join(repoRoot, "node_modules");
  env.MUSHROOM_DEPS = dirs[0] || path.join(repoRoot, "node_modules");
  env.MIGRATION_REPO_ROOT = repoRoot;
  return env;
}

function runNode(scriptPath, args, repoRoot, stdoutTarget) {
  return spawnSync(process.execPath, [scriptPath].concat(args), {
    cwd: ASSETS_ROOT,
    env: childEnv(repoRoot),
    encoding: "utf8",
    timeout: PROBE_TIMEOUT_MS,
    maxBuffer: 64 * 1024 * 1024,
  });
}

function readJSON(file) {
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

function readJSONStripped(file) {
  let text = fs.readFileSync(file, "utf8").trim();
  const fenced = text.match(/```(?:json)?\s*\n([\s\S]*?)\n```/);
  if (fenced) text = fenced[1].trim();
  return JSON.parse(text);
}

function readInventory() {
  const raw = readJSON(OBSERVATIONS_PATH);
  return raw.observations;
}

function tmpFile(name) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "migration-integration-"));
  return path.join(dir, name);
}

/* ---------------------------------------------------------------- F2P ---- */

function collectF2P0001(side, repoRoot, out) {
  const script = path.join(
    ASSETS_ROOT,
    side,
    "batches",
    "BATCH-F2P-0001",
    "probe.js"
  );
  const reportPath = tmpFile("f2p-0001.json");
  const proc = runNode(script, [repoRoot, reportPath], repoRoot);
  try {
    const report = readJSON(reportPath);
    for (const entry of report.results || []) {
      out[entry.observation_id] = {
        status: entry.status,
        value: { result: entry.status },
      };
    }
    return true;
  } catch (err) {
    void proc;
    return false;
  }
}

function collectF2P0002(side, repoRoot, out) {
  const script = path.join(
    ASSETS_ROOT,
    side,
    "batches",
    "BATCH-F2P-0002",
    "probe.cjs"
  );
  const reportPath = tmpFile("f2p-0002.json");
  runNode(script, [repoRoot, reportPath], repoRoot);
  try {
    const report = readJSON(reportPath);
    for (const entry of report.results || []) {
      out[entry.observation_id] = {
        status: entry.status,
        value: { result: entry.status },
      };
    }
    return true;
  } catch (err) {
    return false;
  }
}

function collectF2P0003(side, repoRoot, out) {
  const script = path.join(
    ASSETS_ROOT,
    side,
    "batches",
    "BATCH-F2P-0003",
    "probe.cjs"
  );
  const proc = runNode(script, [repoRoot], repoRoot);
  try {
    const report = JSON.parse((proc.stdout || "").trim());
    for (const [id, status] of Object.entries(report.results || {})) {
      out[id] = { status: status, value: { result: status } };
    }
    return true;
  } catch (err) {
    return false;
  }
}

/* ----------------------------------------------------- P2P / DIFFERENTIAL */

function buildCombinedCorpus() {
  const corpus = [];
  corpus.push(...readJSON(DIFFERENTIAL_BATCH_CORPUS).corpus);
  corpus.push(...readJSON(DIFFERENTIAL_EXTRAS).cases);
  corpus.push(...readJSON(P2P_CASES).cases);
  const file = tmpFile("corpus.json");
  fs.writeFileSync(file, JSON.stringify({ corpus: corpus }, null, 2));
  return { file: file, cases: corpus };
}

function runSharedCorpus(side, repoRoot) {
  const { file, cases } = buildCombinedCorpus();
  const script = path.join(
    ASSETS_ROOT,
    side,
    "batches",
    "BATCH-DIFFERENTIAL-0001",
    "probe.js"
  );
  const outPath = tmpFile("normalized.json");
  runNode(script, [repoRoot, file, outPath], repoRoot);
  let results = {};
  let ok = false;
  try {
    results = readJSON(outPath);
    ok = true;
  } catch (err) {
    results = {};
  }
  return { ok: ok, results: results, cases: cases };
}

function groupCaseIds(cases) {
  const groups = {};
  for (const c of cases) {
    const observationId = c.observation_id || c.id.split("-").slice(0, 2).join("-");
    const prefix = c.id.replace(/-[a-z]$/, "");
    const key = c.observation_id || prefix;
    if (!groups[key]) groups[key] = [];
    groups[key].push(c.id);
    void observationId;
  }
  return groups;
}

function projectCases(caseIds, results) {
  const projection = {};
  for (const id of caseIds) projection[id] = id in results ? results[id] : null;
  return projection;
}

function statusForExpected(expectations, results) {
  let status = "PASS";
  for (const expectation of expectations) {
    const result = results[expectation.id];
    if (!result || typeof result !== "object") {
      return "INCONCLUSIVE";
    }
    if (expectation.expect && result.decision !== expectation.expect) {
      status = "FAIL";
    }
  }
  return status;
}

/* ------------------------------------------------------------- assembly --- */

function buildReport(side, repoRoot) {
  const inventory = readInventory();
  const f2p = {};
  const notes = [];

  if (!collectF2P0001(side, repoRoot, f2p)) notes.push("F2P-0001 probe unavailable");
  if (!collectF2P0002(side, repoRoot, f2p)) notes.push("F2P-0002 probe unavailable");
  if (!collectF2P0003(side, repoRoot, f2p)) notes.push("F2P-0003 probe unavailable");

  const shared = runSharedCorpus(side, repoRoot);
  if (!shared.ok) notes.push("P2P/DIFFERENTIAL shared corpus unavailable");

  const p2pConfig = readJSON(P2P_CASES);
  const p2pExpectations = {};
  for (const c of p2pConfig.cases) {
    if (!p2pExpectations[c.observation_id]) p2pExpectations[c.observation_id] = [];
    p2pExpectations[c.observation_id].push(c);
  }

  const sharedGroups = groupCaseIds(shared.cases);

  const observations = [];
  for (const entry of inventory) {
    const kind = entry.kind;
    if (kind === "F2P") {
      const observed = f2p[entry.id];
      observations.push({
        id: entry.id,
        kind: kind,
        status: observed ? observed.status : "INCONCLUSIVE",
        value: {
          type: "json",
          value: observed ? observed.value : { result: "INCONCLUSIVE" },
        },
      });
      continue;
    }

    const caseIds = sharedGroups[entry.id] || [];
    const projection = projectCases(caseIds, shared.results);

    if (kind === "P2P") {
      const expectations = p2pExpectations[entry.id] || [];
      const status = shared.ok
        ? statusForExpected(expectations, shared.results)
        : "INCONCLUSIVE";
      observations.push({
        id: entry.id,
        kind: kind,
        status: status,
        value: { type: "json", value: projection },
      });
      continue;
    }

    observations.push({
      id: entry.id,
      kind: kind,
      status: shared.ok ? "PASS" : "INCONCLUSIVE",
      value: { type: "json", value: projection },
    });
  }

  return {
    schema_version: "migration-observations:v6",
    observations: observations,
    notes: notes.length ? notes : undefined,
  };
}

function main(side, reportPath) {
  const repoRoot = resolveRepoRoot();
  const report = buildReport(side, repoRoot);
  const target = reportPath || "/logs/result.json";
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
  } catch (_) {
    /* directory already exists */
  }
  fs.writeFileSync(target, JSON.stringify(report, null, 2) + "\n");
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
  return report;
}

module.exports = { main, buildReport, resolveRepoRoot, ASSETS_ROOT };

if (require.main === module) {
  const side = process.argv[2];
  const reportPath = process.argv[3];
  if (side !== "source" && side !== "target") {
    process.stderr.write("usage: runner-core.cjs <source|target> <reportPath>\n");
    process.exit(2);
  }
  main(side, reportPath);
}
