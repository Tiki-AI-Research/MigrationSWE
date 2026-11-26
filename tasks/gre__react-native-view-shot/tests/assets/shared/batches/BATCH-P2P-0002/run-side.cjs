#!/usr/bin/env node
"use strict";

/*
 * Shared P2P side runner for BATCH-P2P-0002.
 *
 * Executes the repository's own Jest suite inside the given side container
 * (source or target) and maps every P2P observation owned by this batch to a
 * concrete, executable test result. For each observation it emits the raw
 * PASS/FAIL of that side plus a stable common projection
 * ({ "result": "PASS" | "FAIL" }) so the normalized values compare equal
 * across source and target.
 *
 * Design notes:
 *  - The repository root is resolved independently of the process cwd so the
 *    harness works both when launched from the asset tree and from the repo.
 *  - Every potentially long-running command is wrapped in an explicit
 *    `timeout`; the runner never waits forever. Privileged or browser/device
 *    suites are intentionally excluded.
 *  - The report always covers every required observation id, even when the
 *    underlying suite fails to start.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const BATCH_ID = "BATCH-P2P-0002";
const DEFAULT_TIMEOUT_SEC = 1800;
const REPO_PACKAGE_NAME = "react-native-view-shot";

function parseArgs(argv) {
  const args = { side: "target", repo: null, report: null, timeout: null };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--side") args.side = argv[++i];
    else if (arg === "--repo") args.repo = argv[++i];
    else if (arg === "--report") args.report = argv[++i];
    else if (arg === "--timeout") args.timeout = Number(argv[++i]);
    else if (!arg.startsWith("-") && !args.report) args.report = arg;
  }
  return args;
}

function looksLikeRepo(dir) {
  if (!dir) return false;
  const pkg = path.join(dir, "package.json");
  if (fs.existsSync(pkg)) {
    try {
      const parsed = JSON.parse(fs.readFileSync(pkg, "utf8"));
      if (parsed && parsed.name === REPO_PACKAGE_NAME) return true;
    } catch (err) {
      /* ignore malformed package.json */
    }
  }
  return (
    fs.existsSync(path.join(dir, "jest.config.cjs")) &&
    fs.existsSync(path.join(dir, "src", "__tests__"))
  );
}

function resolveRepo(explicit) {
  const candidates = [explicit, process.env.REPO_ROOT, "/workspace/repo", process.cwd()];
  for (const candidate of candidates) {
    if (candidate && looksLikeRepo(path.resolve(candidate))) {
      return path.resolve(candidate);
    }
  }
  let dir = process.cwd();
  while (dir && dir !== path.dirname(dir)) {
    if (looksLikeRepo(dir)) return dir;
    dir = path.dirname(dir);
  }
  return explicit ? path.resolve(explicit) : process.cwd();
}

function run(command, cwd, timeoutSec) {
  const wrapped = ["timeout", "--signal=SIGKILL", String(timeoutSec), ...command];
  const result = spawnSync(wrapped[0], wrapped.slice(1), {
    cwd,
    encoding: "utf8",
    env: Object.assign({}, process.env, { CI: "true" }),
    maxBuffer: 64 * 1024 * 1024,
    timeout: (timeoutSec + 30) * 1000,
    killSignal: "SIGKILL",
  });
  return {
    status: typeof result.status === "number" ? result.status : 124,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    error: result.error ? String(result.error.message || result.error) : null,
    timedOut: Boolean(result.error && result.error.code === "ETIMEDOUT"),
  };
}

function resolveBatchFile(repo, name) {
  const candidates = [
    path.join(repo, "shared", "batches", BATCH_ID, name),
    path.resolve(__dirname, name),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function loadObservations(repo) {
  const file = resolveBatchFile(repo, "observations.json");
  if (!file) throw new Error("observations.json not found for " + BATCH_ID);
  const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
  return Array.isArray(parsed.observations) ? parsed.observations : [];
}

function collectJestTests(repo, timeoutSec) {
  const outFile = path.join(os.tmpdir(), "p2p-0002-jest-" + process.pid + ".json");
  const command = [
    "npm",
    "test",
    "--",
    "--ci",
    "--runInBand",
    "--json",
    "--outputFile=" + outFile,
  ];
  const execution = run(command, repo, timeoutSec);
  let parsed = null;
  if (fs.existsSync(outFile)) {
    try {
      parsed = JSON.parse(fs.readFileSync(outFile, "utf8"));
    } catch (err) {
      parsed = null;
    }
    try {
      fs.unlinkSync(outFile);
    } catch (err) {
      /* ignore */
    }
  }
  return { execution, parsed };
}

function normalize(text) {
  return String(text == null ? "" : text)
    .replace(/\s+/g, " ")
    .trim();
}

function relativeTo(repo, file) {
  const rel = path.relative(repo, file);
  return normalize(rel.split(path.sep).join("/"));
}

function buildStatusMap(repo, parsed) {
  const status = new Map();
  if (!parsed || !Array.isArray(parsed.testResults)) return status;
  for (const suite of parsed.testResults) {
    const rel = relativeTo(repo, suite.name || "");
    for (const assertion of suite.assertionResults || []) {
      const value = assertion.status === "passed" ? "PASS" : "FAIL";
      const fullName = normalize(assertion.fullName || "");
      const joined = normalize(
        (assertion.ancestorTitles || []).concat(assertion.title || "").join(" "),
      );
      const bare = normalize(assertion.title || "");
      const keys = [];
      if (rel && fullName) keys.push(rel + "#" + fullName);
      if (rel && joined) keys.push(rel + "#" + joined);
      if (rel && bare) keys.push(rel + "#" + bare);
      if (fullName) keys.push(fullName);
      if (joined) keys.push(joined);
      if (bare) keys.push(bare);
      for (const key of keys) {
        if (key && !status.has(key)) status.set(key, value);
      }
    }
  }
  return status;
}

function findTestStatus(observation, statusMap) {
  const mapping = observation.jest;
  if (!mapping || !mapping.name) return null;
  const candidates = [];
  if (mapping.file) candidates.push(normalize(mapping.file) + "#" + normalize(mapping.name));
  candidates.push(normalize(mapping.name));
  if (mapping.fullName) candidates.push(normalize(mapping.fullName));
  for (const candidate of candidates) {
    if (candidate && statusMap.has(candidate)) return statusMap.get(candidate);
  }
  return null;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const timeoutSec = args.timeout || DEFAULT_TIMEOUT_SEC;
  const repo = resolveRepo(args.repo);

  let observations = [];
  let loadError = null;
  try {
    observations = loadObservations(repo);
  } catch (err) {
    loadError = String(err && err.message ? err.message : err);
  }

  let execution = { status: 124, timedOut: false, error: loadError };
  let parsed = null;
  if (!loadError) {
    const collected = collectJestTests(repo, timeoutSec);
    execution = collected.execution;
    parsed = collected.parsed;
  }

  const suitePassed = execution.status === 0 && parsed && parsed.success !== false;
  const statusMap = buildStatusMap(repo, parsed);

  const results = observations.map((observation) => {
    let status;
    if (observation.suite) {
      status = suitePassed ? "PASS" : "FAIL";
    } else {
      const specific = findTestStatus(observation, statusMap);
      status = specific || "FAIL";
    }
    return {
      observation_id: observation.id,
      surface: observation.surface,
      projection: observation.projection || "observable-result",
      status,
      value: { result: status },
    };
  });

  const everyPass = results.length > 0 && results.every((item) => item.status === "PASS");
  const report = {
    batch_id: BATCH_ID,
    side: args.side,
    repo,
    command: ["timeout", String(timeoutSec), "npm", "test", "--", "--ci", "--runInBand"],
    exit_code: execution.status,
    timed_out: Boolean(execution.timedOut),
    error: execution.error || null,
    result: everyPass ? "PASS" : "FAIL",
    required_runtime_observations: observations.map((observation) => observation.id),
    observations: results,
  };

  const serialized = JSON.stringify(report, null, 2);
  if (args.report) {
    try {
      fs.mkdirSync(path.dirname(path.resolve(args.report)), { recursive: true });
      fs.writeFileSync(args.report, serialized);
    } catch (err) {
      /* never fail to report */
    }
  }
  process.stdout.write(serialized + "\n");
  process.exit(everyPass ? 0 : 1);
}

main();
