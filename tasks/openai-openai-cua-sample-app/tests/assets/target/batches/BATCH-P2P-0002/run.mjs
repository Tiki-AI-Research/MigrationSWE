#!/usr/bin/env node
// Target-side entry point for BATCH-P2P-0002. Executes the shared probe against
// the reference Express migration and prints the normalized P2P projection for
// every observation owned by this batch.
//
// Every observation is mapped to a real, executable projection. The probe drives
// the Express server's re-implemented `.inject()` / `.listen()` / `.close()`
// contract over the public HTTP surface and runs the repository's own vitest
// suites: the in-scope `javascript-app/tests/server.test.ts` harness plus the
// out-of-scope `console/tests/useRunStream.recovery.test.tsx` suite owned by
// P2P-034, with a terminal-once runner test as an executable fallback. The
// report always covers all sixteen observation IDs, so no P2P is left to the
// external collector.
//
// Dispatch contract (kept intentionally permissive so the integration entry can
// invoke this file however it prefers):
//   * the repository root is taken from P2P_ROOT / REPO_ROOT / a positional
//     directory argument / the current working directory (never a hardcoded
//     authoring-only path), preferring a repo root that also has node_modules;
//   * the report path is taken from P2P_REPORT or a `--out` / `--report`
//     argument, and the JSON report is always mirrored to stdout;
//   * `collect()` is exported so the entry can also be imported and awaited.
import { spawn } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const BATCH_ID = "BATCH-P2P-0002";
const SIDE = "target";
const SIDE_ROOTS = ["/workspace/repo", "/input/target"];

function exists(file) {
  try {
    return Boolean(file) && existsSync(file);
  } catch {
    return false;
  }
}

function asDirectory(candidate) {
  if (!candidate) return undefined;
  try {
    return statSync(candidate).isDirectory() ? resolve(candidate) : undefined;
  } catch {
    return undefined;
  }
}

const isRepoRoot = (dir) => exists(dir) && exists(resolve(dir, "javascript-app/src/server.ts"));
const hasModules = (dir) => exists(dir) && exists(resolve(dir, "node_modules"));

function candidateRoots(argv) {
  return [
    process.env.P2P_ROOT,
    process.env.REPO_ROOT,
    process.env.MIGRATION_REPO_ROOT,
    process.env.WORKSPACE,
    argv.map(asDirectory).find(Boolean),
    process.cwd(),
    ...SIDE_ROOTS,
  ].filter(Boolean);
}

function pickRoot(argv) {
  const candidates = candidateRoots(argv);
  const repoRoots = candidates.filter(isRepoRoot);
  return repoRoots.find(hasModules) ?? repoRoots[0] ?? candidates[0] ?? process.cwd();
}

function resolveProbe() {
  return (
    process.env.P2P_PROBE ??
    [
      resolve(here, `../../../shared/batches/${BATCH_ID}/probe.mjs`),
      resolve(here, `../../shared/batches/${BATCH_ID}/probe.mjs`),
      resolve(here, `../shared/batches/${BATCH_ID}/probe.mjs`),
      `/assets/shared/batches/${BATCH_ID}/probe.mjs`,
      `/workspace/repo/shared/batches/${BATCH_ID}/probe.mjs`,
    ].find(exists)
  );
}

function parseReport(argv) {
  if (process.env.P2P_REPORT) return process.env.P2P_REPORT;
  for (let i = 0; i < argv.length; i += 1) {
    const value = argv[i];
    if (!value) continue;
    if (value.startsWith("--out=") || value.startsWith("--report=")) {
      return value.slice(value.indexOf("=") + 1);
    }
    if ((value === "--out" || value === "--report") && argv[i + 1]) return argv[i + 1];
    if (value.endsWith(".json")) return value;
  }
  return undefined;
}

export function collect({ root = pickRoot([]), side = SIDE, report } = {}) {
  const probe = resolveProbe();
  if (!probe) return Promise.reject(new Error("shared BATCH-P2P-0002 probe not found"));
  const args = ["--import", "tsx/esm", probe, "--root", root, "--side", side];
  if (report) args.push("--out", report);
  return new Promise((resolvePromise, reject) => {
    let stdout = "";
    const child = spawn(process.execPath, args, {
      cwd: root,
      env: { ...process.env, P2P_ROOT: root, P2P_SIDE: side },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      process.stdout.write(chunk);
    });
    child.stderr.on("data", (chunk) => process.stderr.write(chunk));
    child.on("error", reject);
    child.on("exit", (code) => {
      let parsed;
      const lastLine = stdout.trim().split("\n").filter(Boolean).pop();
      try {
        parsed = JSON.parse(lastLine);
      } catch {
        parsed = undefined;
      }
      if (!parsed && report && exists(report)) {
        try {
          parsed = JSON.parse(readFileSync(report, "utf8"));
        } catch {
          parsed = undefined;
        }
      }
      if (!parsed) reject(new Error(`BATCH-P2P-0002 target probe produced no report (exit ${code})`));
      else resolvePromise(parsed);
    });
  });
}

export default collect;

const invokedDirectly = process.argv[1] && /(^|[/\\])run\.mjs$/.test(process.argv[1]);
if (invokedDirectly || process.argv[1] === pathToFileURL(here + "/run.mjs").pathname) {
  const argv = process.argv.slice(2);
  collect({ root: pickRoot(argv), side: SIDE, report: parseReport(argv) }).catch((error) => {
    process.stderr.write(`${String(error?.stack ?? error)}\n`);
    process.exitCode = 1;
  });
}
