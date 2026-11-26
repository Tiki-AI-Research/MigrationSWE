"use strict";

// Runner core for BATCH-F2P-0002.
//
// Loads the shared observations and predicates and evaluates every observation
// against a checkout. The SAME evaluation is used for the source and target
// sides; each side then compares the observed status with its frozen
// expectation (source => FAIL, target => PASS) so no side is hardcoded.
//
// Any command that could run for a long time is spawned through the explicit
// `timeout` coreutil; the runner never waits unbounded on a child process.

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const predicates = require("./predicates.cjs");

const OBSERVATIONS_PATH = path.join(__dirname, "observations.json");
const DEFAULT_COMMAND_TIMEOUT_SEC = 300;

function loadObservations() {
  return JSON.parse(fs.readFileSync(OBSERVATIONS_PATH, "utf8"));
}

function runWithTimeout(command, args, options) {
  const timeoutSec =
    (options && options.timeoutSec) || DEFAULT_COMMAND_TIMEOUT_SEC;
  // Wrap every potentially long-running child in an explicit `timeout` so the
  // harness can never block forever, and pass a hard kill signal after a grace
  // period.
  const wrapped = spawnSync(
    "timeout",
    ["--kill-after=10", String(timeoutSec)].concat([command]).concat(args),
    {
      cwd: (options && options.cwd) || process.cwd(),
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
      timeout: (timeoutSec + 15) * 1000,
    },
  );
  return {
    status: wrapped.status,
    signal: wrapped.signal,
    stdout: wrapped.stdout || "",
    stderr: wrapped.stderr || "",
    error: wrapped.error ? String(wrapped.error) : null,
  };
}

function runSide(side, root, options) {
  const opts = options || {};
  const doc = loadObservations();
  const expected = side === "source" ? "FAIL" : "PASS";
  const results = [];

  for (const spec of doc.observations) {
    const sideExpectation = side === "source" ? spec.source_expectation : spec.target_expectation;
    let observed;
    try {
      observed = predicates.evaluate(root, spec);
    } catch (err) {
      results.push({
        observation_id: spec.id,
        status: "INFRA_ERROR",
        error: String(err && err.stack ? err.stack : err),
        expectation: sideExpectation,
      });
      continue;
    }
    results.push(
      Object.assign({}, observed, {
        expectation: sideExpectation,
        expectation_met: observed.status === sideExpectation,
      }),
    );
  }

  const failed = results.filter(r => r.status !== expected);
  const infraErrors = results.filter(r => r.status === "INFRA_ERROR");

  const report = {
    batch_id: doc.batch_id,
    kind: doc.kind,
    side,
    root,
    expected_observation_status: expected,
    total: results.length,
    passed: results.length - failed.length,
    failed: failed.length,
    infra_errors: infraErrors.length,
    results,
  };

  if (opts.verbose) {
    for (const r of results) {
      process.stdout.write(
        `${r.observation_id}\t${r.status}\texpect=${r.expectation}\ttech=${r.target_technology && r.target_technology.id}:${r.target_technology && r.target_technology.ok}\n`,
      );
    }
  }

  return report;
}

module.exports = { loadObservations, runSide, runWithTimeout };
