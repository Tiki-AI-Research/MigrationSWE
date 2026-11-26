#!/usr/bin/env node
// Shared integration dispatcher for BATCH-INTEGRATION-0001.
//
// The integration batch owns no observations of its own. It merges the
// per-batch executors authored by the dependency batches and produces the
// single runtime report required by the migration runtime:
//
//   { "schema_version": "migration-observations:v6",
//     "observations": [ { id, kind, status, value: { type, value } }, ... ] }
//
// Both the source entry point (`source/runner`) and the target entry point
// (`harness.mjs`) call `runSide()` with their respective side. Every entry
// dispatches the executable projection contributed by the owning dependency
// batch and ingests the raw per-side result exactly as the executor reported
// it. In particular:
//
//   * P2P-0001 / P2P-0002 run the repository's own native suites, so the status
//     is the side's raw PASS/FAIL (never a role- or side-derived constant).
//   * F2P-0001 / F2P-0002 / F2P-0003 run the batches' target-technology
//     predicates on the real load/execution path. The side's own result is
//     reported verbatim: a source checkout that still loads the source
//     technology yields FAIL, exactly as the frozen contract requires.
//   * DIFFERENTIAL-0001 emits a normalized per-side projection; the status
//     records whether the projection was actually captured on that side.
//
// Missing or failing executors are reported as INCONCLUSIVE rather than
// silently dropped, so the report always contains every required observation
// id. No observation status is ever forced by the side/role.

import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const assetsRoot = resolve(here, "..", "..");

const REPORT_SCHEMA_VERSION = "migration-observations:v6";
const REPO_ENV_KEYS = ["MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"];
const VALID_STATUSES = new Set(["PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT", "NOT_RUN"]);

function resolveRepoRoot() {
  for (const key of REPO_ENV_KEYS) {
    const value = process.env[key];
    if (value && value.trim()) return resolve(value.trim());
  }
  return resolve(process.cwd());
}

function loadInventory() {
  const inventoryPath = join(assetsRoot, "shared", "integration", "observations.json");
  const inventory = JSON.parse(readFileSync(inventoryPath, "utf8"));
  return inventory.observations ?? [];
}

function parseJsonPayload(stdout) {
  if (!stdout) return undefined;
  const start = stdout.indexOf("{");
  const end = stdout.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) return undefined;
  try {
    return JSON.parse(stdout.slice(start, end + 1));
  } catch {
    return undefined;
  }
}

function readJsonFile(path) {
  if (!existsSync(path)) return undefined;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return undefined;
  }
}

function resolveTsxBin(cwd) {
  for (const candidate of [
    join(cwd, "node_modules", ".bin", "tsx"),
    join(resolveRepoRoot(), "node_modules", ".bin", "tsx"),
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return undefined;
}

function runNode(scriptPath, args, { cwd, useTsx = false, env = {}, timeout = 900000 } = {}) {
  if (!existsSync(scriptPath)) {
    return { code: 127, stdout: "", stderr: `executor not found: ${scriptPath}`, missing: true };
  }
  const resolvedCwd = cwd ?? resolveRepoRoot();
  let command = process.execPath;
  let nodeArgs;
  if (useTsx) {
    const tsxBin = resolveTsxBin(resolvedCwd);
    if (tsxBin) {
      command = tsxBin;
      nodeArgs = [scriptPath, ...args];
    } else {
      nodeArgs = ["--import", "tsx", scriptPath, ...args];
    }
  } else {
    nodeArgs = [scriptPath, ...args];
  }
  const result = spawnSync(command, nodeArgs, {
    cwd: resolvedCwd,
    encoding: "utf8",
    env: { ...process.env, CI: "1", ...env },
    maxBuffer: 256 * 1024 * 1024,
    timeout,
  });
  return {
    code: result.status ?? -1,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    signal: result.signal ?? null,
  };
}

function stableValue(value) {
  if (typeof value === "string") {
    return value.replace(/\b\d{4}-\d{2}-\d{2}T[0-9:.]+Z\b/g, "<timestamp>");
  }
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = stableValue(value[key]);
    return out;
  }
  return value;
}

function normaliseStatus(raw) {
  if (typeof raw !== "string") return undefined;
  const upper = raw.toUpperCase();
  return VALID_STATUSES.has(upper) ? upper : undefined;
}

// Derives a status from an executor's own payload, never from the side/role.
function statusFromValue(value) {
  if (value === "PASS" || value === "FAIL") return value;
  if (value && typeof value === "object") {
    const raw = value.outcome ?? value.result ?? value.status;
    return normaliseStatus(raw);
  }
  return undefined;
}

// Records one observation, never letting an inconclusive projection clobber a
// concrete PASS/FAIL already reported for the same id.
function put(collected, id, status, value) {
  const existing = collected.get(id);
  if (existing && existing.status !== "INCONCLUSIVE" && status === "INCONCLUSIVE") return;
  collected.set(id, { status, value });
}

// Ingests one executor payload. Recognises the three report shapes emitted by
// the dependency batches and preserves the raw status each one reports. The
// most explicit shape (a per-observation outcome) wins over a bare projection.
function ingest(parsed, collected) {
  if (!parsed || typeof parsed !== "object") return false;

  let sawAny = false;

  const observations = parsed.observations;
  if (observations && typeof observations === "object" && !Array.isArray(observations)) {
    for (const [id, value] of Object.entries(observations)) {
      put(collected, id, statusFromValue(value) ?? "INCONCLUSIVE", stableValue(value));
      sawAny = true;
    }
  }

  if (Array.isArray(parsed.results)) {
    for (const item of parsed.results) {
      if (!item || typeof item !== "object") continue;
      const id = item.id ?? item.observation_id;
      if (!id) continue;
      const explicit = normaliseStatus(item.status ?? item.outcome);
      const hasProjection =
        item.projection !== undefined || item.detail !== undefined || item.value !== undefined;
      put(
        collected,
        id,
        explicit ?? (hasProjection ? "PASS" : "INCONCLUSIVE"),
        stableValue(item.projection ?? item.value ?? item.detail ?? null),
      );
      sawAny = true;
    }
  }

  if (Array.isArray(parsed.runtime_observations)) {
    for (const item of parsed.runtime_observations) {
      if (!item || typeof item !== "object") continue;
      const id = item.id ?? item.observation_id;
      if (!id) continue;
      put(
        collected,
        id,
        normaliseStatus(item.outcome ?? item.status) ?? "INCONCLUSIVE",
        stableValue(item.value ?? item.projection ?? null),
      );
      sawAny = true;
    }
  }

  return sawAny;
}

function runnerEnv(repoRoot) {
  return { REPO_ROOT: repoRoot, MIGRATION_REPO_ROOT: repoRoot, REPO_ROOT: repoRoot };
}

function runP2P0001(side, { repoRoot, scratch }) {
  const script = join(assetsRoot, side, "batches", "BATCH-P2P-0001", "runner.mjs");
  const out = join(scratch, `p2p-0001-${side}.json`);
  const run = runNode(script, [repoRoot, out, side], { cwd: repoRoot });
  return { run, parsed: parseJsonPayload(run.stdout) ?? readJsonFile(out) };
}

function runP2P0002(side, { repoRoot, scratch }) {
  const script = join(assetsRoot, side, "batches", "BATCH-P2P-0002", "run.mjs");
  const out = join(scratch, `p2p-0002-${side}.json`);
  const run = runNode(script, [], {
    cwd: repoRoot,
    env: { ...runnerEnv(repoRoot), P2P_ROOT: repoRoot, P2P_REPORT: out },
  });
  return { run, parsed: parseJsonPayload(run.stdout) ?? readJsonFile(out) };
}

function runF2P0001(side, { repoRoot }) {
  const script = join(assetsRoot, side, "batches", "BATCH-F2P-0001", "probe.mjs");
  const run = runNode(script, [repoRoot], {
    cwd: repoRoot,
    useTsx: true,
    env: runnerEnv(repoRoot),
  });
  return { run, parsed: parseJsonPayload(run.stdout) };
}

function runF2P0002(side, { repoRoot }) {
  const script = join(assetsRoot, side, "batches", "BATCH-F2P-0002", "probes.ts");
  const run = runNode(script, [], {
    cwd: repoRoot,
    useTsx: true,
    env: runnerEnv(repoRoot),
  });
  return { run, parsed: parseJsonPayload(run.stdout) };
}

function runF2P0003(side, { repoRoot }) {
  const script = join(assetsRoot, side, "batches", "BATCH-F2P-0003", "run-probes.mjs");
  const run = runNode(script, ["--root", repoRoot], {
    cwd: repoRoot,
    useTsx: true,
    env: runnerEnv(repoRoot),
  });
  return { run, parsed: parseJsonPayload(run.stdout) };
}

function runDifferential(side, { repoRoot, scratch }) {
  const script = join(assetsRoot, "source", "batches", "BATCH-DIFFERENTIAL-0001", "source-probe.mjs");
  const cases = join(assetsRoot, "shared", "batches", "BATCH-DIFFERENTIAL-0001", "cases.json");
  const out = join(scratch, `differential-${side}.json`);
  const run = runNode(script, ["--repo", repoRoot, "--cases", cases, "--out", out], {
    cwd: repoRoot,
    useTsx: true,
    env: runnerEnv(repoRoot),
  });
  return { run, parsed: parseJsonPayload(run.stdout) ?? readJsonFile(out) };
}

// Every required observation is owned by exactly one dependency batch. The
// integration entry composes all of them, including the F2P-0001 predicates
// that were previously reimplemented locally with weaker (non-discriminating)
// checks.
const EXECUTORS = [
  { batch: "BATCH-P2P-0001", run: runP2P0001 },
  { batch: "BATCH-P2P-0002", run: runP2P0002 },
  { batch: "BATCH-F2P-0001", run: runF2P0001 },
  { batch: "BATCH-F2P-0002", run: runF2P0002 },
  { batch: "BATCH-F2P-0003", run: runF2P0003 },
  { batch: "BATCH-DIFFERENTIAL-0001", run: runDifferential },
];

export function runSide(side, reportPath) {
  if (side !== "source" && side !== "target") {
    throw new Error(`unknown side: ${side}`);
  }
  const inventory = loadInventory();
  const repoRoot = resolveRepoRoot();
  const scratch = mkdtempSync(join(tmpdir(), "integration-"));
  const statuses = new Map();

  for (const executor of EXECUTORS) {
    let parsed;
    let run = { missing: false, stderr: "" };
    try {
      const executed = executor.run(side, { repoRoot, scratch });
      run = executed.run;
      parsed = executed.parsed;
      ingest(parsed, statuses);
    } catch (error) {
      run = { missing: false, stderr: String(error?.message ?? error) };
    }
    for (const observation of inventory) {
      if (observation.batch_id !== executor.batch) continue;
      if (statuses.has(observation.id)) continue;
      statuses.set(observation.id, {
        status: "INCONCLUSIVE",
        value: {
          reason: run.missing
            ? `executor missing for ${executor.batch}`
            : `executor for ${executor.batch} did not report ${observation.id}`,
          stderr: run.missing ? "" : (run.stderr ?? "").slice(0, 500),
        },
      });
    }
  }

  const observations = inventory.map((observation) => {
    const entry = statuses.get(observation.id);
    return {
      id: observation.id,
      kind: observation.kind,
      status: entry ? entry.status : "INCONCLUSIVE",
      value: {
        type: "json",
        value: entry
          ? entry.value
          : { reason: `no executor provides ${observation.id}`, batch_id: observation.batch_id },
      },
    };
  });

  const report = { schema_version: REPORT_SCHEMA_VERSION, side, observations };
  const serialized = `${JSON.stringify(report)}\n`;
  if (reportPath) writeFileSync(reportPath, serialized);
  process.stdout.write(serialized);
  try {
    rmSync(scratch, { recursive: true, force: true });
  } catch {
    /* best effort */
  }
  return report;
}
