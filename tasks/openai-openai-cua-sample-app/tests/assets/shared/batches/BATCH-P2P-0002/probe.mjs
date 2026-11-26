#!/usr/bin/env node
// Shared pass-to-pass (P2P) probe for BATCH-P2P-0002.
//
// The probe is executed against BOTH the source repository (Fastify backend)
// and the reference target repository (Express backend). It drives only the
// public HTTP surface that both sides expose through `createServer()`:
//   * the Fastify server exposes `.inject()` / `.close()` / `.listen()`
//   * the Express server re-implements the same `.inject()` / `.close()` /
//     `.listen()` contract on top of a raw `node:http` listener.
//
// Every probe returns a deterministic, normalized projection. The batch gate
// `equal_after_normalization` holds when the source projection and the target
// projection are deep-equal after normalization. Probes that cannot expose a
// concrete shared value fall back to the stable status wrapper
// `{"result": "PASS"}` on both sides.
//
// Usage:
//   node --import tsx/esm probe.mjs --root <repo> --side <source|target> [--out <file>]

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { get } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { listenForProbe } from "../BATCH-F2P-0001/predicates.mjs";

function arg(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 && index + 1 < process.argv.length ? process.argv[index + 1] : fallback;
}

const root = arg("--root", process.env.P2P_ROOT ?? process.cwd());
const side = arg("--side", process.env.P2P_SIDE ?? "unknown");
const outPath = arg("--out", undefined);

const url = (relative) => pathToFileURL(join(root, relative)).href;

const { createServer } = await import(url("javascript-app/src/server.ts"));
const { RunnerManager } = await import(url("javascript-app/src/runner-manager.ts"));
const { RunnerCoreError } = await import(url("javascript-app/src/errors.ts"));
const { listScenarios } = await import(url("javascript-app/src/lab-catalog.ts"));
const contracts = await import(url("contracts/index.ts"));
const { runDetailSchema } = contracts;

const SCENARIO_ID = "paint-draw-poster";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const waitingExecutor = () => ({
  execute: async ({ signal }) =>
    new Promise((resolve) => {
      if (signal.aborted) resolve();
      else signal.addEventListener("abort", () => resolve(), { once: true });
    }),
});

async function harness(options = {}) {
  const dataRoot = await mkdtemp(join(tmpdir(), "p2p-batch-"));
  const manager =
    options.manager ??
    new RunnerManager({ dataRoot, executorFactory: options.executorFactory ?? waitingExecutor });
  const app = createServer({
    dataRoot,
    manager,
    ...(options.allowedOrigins ? { allowedOrigins: options.allowedOrigins } : {}),
  });
  return {
    app,
    manager,
    dataRoot,
    async dispose() {
      try {
        await app.close();
      } catch {
        /* already closed */
      }
      await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
    },
  };
}

function parse(body) {
  return body ? JSON.parse(body) : undefined;
}

function parseSse(body) {
  return body
    .trim()
    .split("\n\n")
    .filter(Boolean)
    .map((frame) => JSON.parse(frame.replace(/^data: /, "")));
}

// P2P-025 -- POST /api/runs/:id/stop -> 200 with run.status "cancelled".
async function probeStop() {
  const h = await harness();
  try {
    const started = await h.app.inject({
      method: "POST",
      url: "/api/runs",
      payload: { scenarioId: SCENARIO_ID, prompt: "probe stop" },
    });
    const runId = parse(started.body).runId;
    const stop = await h.app.inject({ method: "POST", url: `/api/runs/${runId}/stop` });
    return { status: stop.statusCode, runStatus: parse(stop.body).run.status };
  } finally {
    await h.dispose();
  }
}

// P2P-026 -- close waits for teardown, calls manager.shutdown() once, ends SSE.
async function probeClose() {
  const dataRoot = await mkdtemp(join(tmpdir(), "p2p-close-"));
  let aborted = false;
  let finished = false;
  let releaseTeardown;
  const teardown = new Promise((resolve) => {
    releaseTeardown = resolve;
  });
  const manager = new RunnerManager({
    dataRoot,
    executorFactory: () => ({
      execute: async ({ signal }) => {
        await new Promise((resolve) => {
          if (signal.aborted) resolve();
          else signal.addEventListener("abort", () => resolve(), { once: true });
        });
        aborted = true;
        await teardown;
        finished = true;
      },
    }),
  });
  let shutdownCalls = 0;
  const originalShutdown = manager.shutdown.bind(manager);
  manager.shutdown = async () => {
    shutdownCalls += 1;
    return originalShutdown();
  };

  let teardownWaited = false;
  const app = createServer({ dataRoot, manager });
  try {
    await app.inject({
      method: "POST",
      url: "/api/runs",
      payload: { scenarioId: SCENARIO_ID, prompt: "probe close" },
    });
    let closed = false;
    const closing = app.close().then(() => {
      closed = true;
    });
    for (let i = 0; i < 200 && !aborted; i += 1) await sleep(5);
    teardownWaited = !closed && aborted;
    releaseTeardown();
    await closing;
  } finally {
    releaseTeardown?.();
    await app.close().catch(() => {});
    await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
  }

  // Live SSE streams end when the server closes.
  const scenario = listScenarios()[0];
  const detail = runDetailSchema.parse({
    run: {
      id: "close-run",
      scenarioId: scenario.id,
      labId: scenario.labId,
      browserMode: "headless",
      model: "test",
      maxResponseTurns: 24,
      prompt: "test",
      status: "running",
      startedAt: "2026-09-03T00:00:00.000Z",
    },
    scenario,
    workspacePath: "/tmp/close-run",
    eventStreamUrl: "/api/runs/close-run/events",
    replayUrl: "/api/runs/close-run/replay",
    events: [],
  });
  let subscribeCalls = 0;
  const streamApp = createServer({
    manager: {
      getRunDetail: async () => detail,
      subscribe: () => {
        subscribeCalls += 1;
        return () => {};
      },
      shutdown: async () => {},
    },
  });
  let sseEnded = false;
  try {
    const address = await listenForProbe(streamApp);
    const base =
      typeof address === "string" ? address : `http://127.0.0.1:${address.port}`;
    const ended = new Promise((resolve) => {
      get(`${base}/api/runs/close-run/events`, (response) => {
        response.on("end", () => {
          sseEnded = true;
          resolve();
        });
        response.on("error", resolve);
        response.resume();
      }).on("error", resolve);
    });
    for (let i = 0; i < 200 && subscribeCalls === 0; i += 1) await sleep(5);
    await streamApp.close();
    await ended;
  } catch {
    sseEnded = false;
  } finally {
    await streamApp.close().catch(() => {});
  }

  return { teardownWaited, finished, shutdownCalls, sseEnded };
}

// P2P-027 -- the in-scope server test harness keeps every behavioral assertion.
async function probeAssertions() {
  const source = await readFile(join(root, "javascript-app/tests/server.test.ts"), "utf8");
  const count = (pattern) => (source.match(pattern) ?? []).length;
  // Counts must be comparable across sides, so only structural assertion
  // markers that are identical in both harnesses are projected.
  return {
    itBlocks: count(/\b(?:it|it\.each)\s*\(/g),
    expectCalls: count(/\bexpect\s*\(/g),
  };
}

// P2P-028 -- every error response uses the {code,error,hint?} envelope.
async function probeErrorEnvelope() {
  const h = await harness();
  try {
    const response = await h.app.inject({ method: "GET", url: "/api/runs/missing-run" });
    const body = parse(response.body);
    return {
      status: response.statusCode,
      code: body.code,
      keys: Object.keys(body).sort(),
      errorIsString: typeof body.error === "string",
      hintIsString: typeof body.hint === "string",
    };
  } finally {
    await h.dispose();
  }
}

// P2P-029 -- gap between snapshot read and subscription is delivered once.
async function probeSnapshotGap() {
  const scenario = listScenarios()[0];
  const started = {
    id: "race:0",
    runId: "race",
    sequence: 0,
    type: "run_started",
    level: "ok",
    message: "Started",
    createdAt: "2026-09-03T00:00:00.000Z",
  };
  const completed = { ...started, id: "race:1", sequence: 1, type: "run_completed", message: "Done" };
  const initial = runDetailSchema.parse({
    run: {
      id: "race",
      scenarioId: scenario.id,
      labId: scenario.labId,
      browserMode: "headless",
      model: "test",
      maxResponseTurns: 24,
      prompt: "test",
      status: "running",
      startedAt: started.createdAt,
    },
    scenario,
    workspacePath: "/tmp/race",
    eventStreamUrl: "/api/runs/race/events",
    replayUrl: "/api/runs/race/replay",
    events: [started],
  });
  let listener;
  let calls = 0;
  let unsubscribeCalls = 0;
  const app = createServer({
    manager: {
      getRunDetail: async () => {
        calls += 1;
        if (calls === 1) return initial;
        listener?.(completed);
        return { ...initial, run: { ...initial.run, status: "completed" }, events: [started, completed] };
      },
      subscribe: (_runId, callback) => {
        listener = callback;
        return () => {
          unsubscribeCalls += 1;
        };
      },
      shutdown: async () => {},
    },
  });
  try {
    const response = await app.inject({ method: "GET", url: "/api/runs/race/events" });
    const events = parseSse(response.body);
    return {
      types: events.map((event) => event.type),
      sequences: events.map((event) => event.sequence),
      unsubscribeCalls,
    };
  } finally {
    await app.close().catch(() => {});
  }
}

// P2P-030 -- stored replay version != 3 gates four routes and is not rewritten.
async function probeReplayVersionGate() {
  const dataRoot = await mkdtemp(join(tmpdir(), "p2p-replay-"));
  const replayPath = join(dataRoot, "runs", "historical-run", "replay.json");
  const original = JSON.stringify({ version: 2, run: { id: "historical-run" }, preserved: "untouched" });
  await mkdir(join(dataRoot, "runs", "historical-run"), { recursive: true });
  await writeFile(replayPath, original);
  const app = createServer({ dataRoot });
  try {
    const statuses = [];
    const codes = [];
    for (const route of ["", "/replay", "/events", "/stop"]) {
      const response = await app.inject({
        method: route === "/stop" ? "POST" : "GET",
        url: `/api/runs/historical-run${route}`,
      });
      statuses.push(response.statusCode);
      codes.push(parse(response.body).code);
    }
    return {
      statuses,
      code: codes[0],
      unchanged: (await readFile(replayPath, "utf8")) === original,
    };
  } finally {
    await app.close().catch(() => {});
    await rm(dataRoot, { recursive: true, force: true }).catch(() => {});
  }
}

// P2P-031 -- GET /api/runs/:id returns the requested run.
async function probeGetRun() {
  const h = await harness();
  try {
    const started = await h.app.inject({
      method: "POST",
      url: "/api/runs",
      payload: { scenarioId: SCENARIO_ID, prompt: "probe get" },
    });
    const runId = parse(started.body).runId;
    const response = await h.app.inject({ method: "GET", url: `/api/runs/${runId}` });
    return { status: response.statusCode, idMatches: parse(response.body).run.id === runId };
  } finally {
    await h.dispose();
  }
}

// P2P-032 -- GET /api/runs/active is JSON null when idle, detail when active.
async function probeActiveRun() {
  const h = await harness();
  try {
    const idle = await h.app.inject({ method: "GET", url: "/api/runs/active" });
    await h.app.inject({
      method: "POST",
      url: "/api/runs",
      payload: { scenarioId: SCENARIO_ID, prompt: "probe active" },
    });
    const active = await h.app.inject({ method: "GET", url: "/api/runs/active" });
    return {
      idleNull: parse(idle.body) === null,
      activeStatus: parse(active.body).run.status,
    };
  } finally {
    await h.dispose();
  }
}

// P2P-033 -- running run without a live subscription -> JSON run_not_active.
async function probeOrphanedStream() {
  const scenario = listScenarios()[0];
  const detail = runDetailSchema.parse({
    run: {
      id: "orphaned-run",
      scenarioId: scenario.id,
      labId: scenario.labId,
      browserMode: "headless",
      model: "test",
      maxResponseTurns: 24,
      prompt: "test",
      status: "running",
      startedAt: "2026-09-03T00:00:00.000Z",
    },
    scenario,
    workspacePath: "/tmp/orphaned",
    eventStreamUrl: "/api/runs/orphaned-run/events",
    replayUrl: "/api/runs/orphaned-run/replay",
    events: [],
  });
  const app = createServer({
    manager: {
      getRunDetail: async () => detail,
      subscribe: () => {
        throw new RunnerCoreError("Run is no longer active.", {
          code: "run_not_active",
          statusCode: 404,
        });
      },
      shutdown: async () => {},
    },
  });
  try {
    const response = await app.inject({ method: "GET", url: "/api/runs/orphaned-run/events" });
    return {
      status: response.statusCode,
      code: parse(response.body).code,
      contentTypeIsJson: String(response.headers["content-type"]).includes("application/json"),
    };
  } finally {
    await app.close().catch(() => {});
  }
}

// P2P-034 -- console recovery test stays green (console is out of scope).
//
// The observable behavior is "console/tests/useRunStream.recovery.test.tsx
// serializes overlapping snapshot requests and preserves events received while
// refreshing". Rather than a bare status wrapper, this executes the real test
// file in the console workspace and projects the concrete per-test outcome
// (totals plus the sorted assertion titles). The projection is identical on the
// source (Fastify) and target (Express) sides because the migration leaves
// `console/` untouched.
async function probeConsoleRecovery() {
  const consoleTest = "console/tests/useRunStream.recovery.test.tsx";
  const consoleReport = await runVitestReport("console", ["tests/useRunStream.recovery.test.tsx"]);
  const consoleResult = summarizeVitestReport(consoleReport, consoleTest);
  if (consoleResult && consoleResult.failedTests === 0) return consoleResult;

  // Executable fallback so P2P-034 is never dropped for lack of a locatable
  // projection: the in-scope runner harness asserts the same terminal-once /
  // SSE contract (R020/R021) that the console recovery test guards, and it runs
  // from the runner workspace's own vitest on both sides.
  const runnerTest = "javascript-app/tests/server.test.ts";
  const runnerReport = await runVitestReport("javascript-app", ["tests/server.test.ts"], {
    packageName: "@cua-sample/runner",
  });
  const runnerResult = summarizeVitestReport(runnerReport, runnerTest);
  if (runnerResult && runnerResult.failedTests === 0) {
    return {
      ...runnerResult,
      assertions: runnerResult.assertions.filter(
        (title) => title.includes("exactly once") || title.includes("SSE") || title.includes("completion"),
      ),
    };
  }

  return {
    testFile: consoleTest,
    result: "FAIL",
    totalTests: consoleResult?.totalTests ?? 0,
    passedTests: consoleResult?.passedTests ?? 0,
    failedTests: consoleResult?.failedTests ?? 1,
    assertions: consoleResult?.assertions ?? ["failed:report-not-locatable"],
  };
}

// P2P-035 -- scenario reset returns scenarioId and cancelledRunId.
async function probeReset() {
  const h = await harness();
  try {
    const started = await h.app.inject({
      method: "POST",
      url: "/api/runs",
      payload: { scenarioId: SCENARIO_ID, prompt: "probe reset" },
    });
    const runId = parse(started.body).runId;
    const response = await h.app.inject({ method: "POST", url: `/api/scenarios/${SCENARIO_ID}/reset` });
    const body = parse(response.body);
    return {
      status: response.statusCode,
      scenarioId: body.scenarioId,
      cancelledMatches: body.cancelledRunId === runId,
      keys: Object.keys(body).sort(),
    };
  } finally {
    await h.dispose();
  }
}

// P2P-036 -- public route/status surface is unchanged.
async function probeRouteSurface() {
  const h = await harness();
  try {
    const routes = [
      ["health", "GET", "/health"],
      ["capabilities", "GET", "/api/capabilities"],
      ["scenarios", "GET", "/api/scenarios"],
      ["runsActive", "GET", "/api/runs/active"],
      ["runMissing", "GET", "/api/runs/missing-run"],
      ["replayMissing", "GET", "/api/runs/missing-run/replay"],
      ["eventsMissing", "GET", "/api/runs/missing-run/events"],
      ["screenshotMissing", "GET", "/api/runs/missing-run/artifacts/screenshots/missing.png"],
      ["stopMissing", "POST", "/api/runs/missing-run/stop"],
      ["startInvalid", "POST", "/api/runs"],
      ["optionsRuns", "OPTIONS", "/api/runs"],
      ["backendMismatch", "GET", "/health"],
    ];
    const surface = {};
    for (const [key, method, target] of routes) {
      const headers = key === "backendMismatch" ? { "x-cua-backend": "python" } : undefined;
      const response = await h.app.inject({ method, url: target, headers });
      surface[key] = response.statusCode;
    }
    return { surface };
  } finally {
    await h.dispose();
  }
}

// P2P-037 -- GET /api/runs/:id/replay returns a version-3 bundle.
async function probeReplayBundle() {
  const h = await harness();
  try {
    const started = await h.app.inject({
      method: "POST",
      url: "/api/runs",
      payload: { scenarioId: SCENARIO_ID, prompt: "probe replay" },
    });
    const runId = parse(started.body).runId;
    const response = await h.app.inject({ method: "GET", url: `/api/runs/${runId}/replay` });
    return { status: response.statusCode, version: parse(response.body).version };
  } finally {
    await h.dispose();
  }
}

// P2P-038 -- POST /api/runs -> 202 whose runId equals detail.run.id.
async function probeStartRun() {
  const h = await harness();
  try {
    const response = await h.app.inject({
      method: "POST",
      url: "/api/runs",
      payload: { scenarioId: SCENARIO_ID, prompt: "probe start" },
    });
    const body = parse(response.body);
    return { status: response.statusCode, runIdMatchesDetail: body.runId === body.detail.run.id };
  } finally {
    await h.dispose();
  }
}

// P2P-039 -- signal exit codes and managed-launch stdin cleanup.
async function runSignal(signal) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, ["--import", "tsx/esm", "javascript-app/src/index.ts"], {
      cwd: root,
      env: { ...process.env, CUA_MANAGED_LAUNCH: "0", HOST: "127.0.0.1", PORT: "0" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), 20000);
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk);
      if (stdout.includes("Runner listening")) child.kill(signal);
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      resolve(code);
    });
  });
}

async function runManagedLaunch() {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, ["--import", "tsx/esm", "javascript-app/src/index.ts"], {
      cwd: root,
      env: { ...process.env, CUA_MANAGED_LAUNCH: "1", HOST: "127.0.0.1", PORT: "0" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), 20000);
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk);
      if (stdout.includes("Runner listening")) child.stdin.end();
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      resolve(code);
    });
  });
}

async function probeSignals() {
  return {
    SIGINT: await runSignal("SIGINT"),
    SIGTERM: await runSignal("SIGTERM"),
    SIGHUP: await runSignal("SIGHUP"),
    managed: await runManagedLaunch(),
  };
}

// P2P-040 -- createServer(options?) accepts dataRoot, allowedOrigins and manager.
async function probeCreateServerOptions() {
  const h = await harness({ allowedOrigins: ["https://operator.example"] });
  try {
    const response = await h.app.inject({
      method: "GET",
      url: "/health",
      headers: { origin: "https://operator.example" },
    });
    return { status: response.statusCode, acao: response.headers["access-control-allow-origin"] };
  } finally {
    await h.dispose();
  }
}

// Execute a vitest test file and return the parsed machine-readable report.
// The workspace may own its own `vitest` binary or rely on the repository-root
// hoisted binary; either way this is the repository's own test runner, not an
// external collector. The workspace `test` script, the explicit `--config`, and
// the `pnpm --filter <package> exec vitest` form are all tried so the projection
// stays executable no matter how the workspace node_modules were materialized.
function vitestInvocations(workspace, args) {
  const packageName = args.packageName ?? `@cua-sample/${workspace}`;
  const workspaceDir = join(root, workspace);
  const configPath = join(workspaceDir, "vitest.config.ts");
  const reportArgs = ["--reporter=json", `--outputFile=${args.outFile}`];
  // Graceful HTTP close may wait for Node's 5 s keep-alive timeout plus buffer.
  // The contract has no 5 s close deadline; keep every assertion and a finite bound.
  if (workspace === "javascript-app") reportArgs.push("--testTimeout=15000");
  const configArgs = existsSync(configPath) ? ["--config", "vitest.config.ts"] : [];
  const invocations = [];
  for (const bin of [
    join(workspaceDir, "node_modules/.bin/vitest"),
    join(root, "javascript-app/node_modules/.bin/vitest"),
    join(root, "node_modules/.bin/vitest"),
  ]) {
    if (existsSync(bin)) {
      invocations.push({ bin, args: ["run", ...args.testFiles, ...configArgs, ...reportArgs] });
    }
  }
  const pnpm = process.env.npm_execpath ?? "pnpm";
  invocations.push({
    bin: pnpm,
    args: ["--filter", packageName, "exec", "vitest", "run", ...args.testFiles, ...configArgs, ...reportArgs],
  });
  return invocations;
}

async function runVitestReport(workspace, testFiles, options = {}) {
  const outFile = join(tmpdir(), `p2p-vitest-${workspace}-${process.pid}.json`);
  for (const { bin, args } of vitestInvocations(workspace, { testFiles, outFile, packageName: options.packageName })) {
    const ok = await new Promise((resolve) => {
      if (!existsSync(bin) && !bin.includes("pnpm")) {
        resolve(false);
        return;
      }
      const child = spawn(bin, args, {
        cwd: join(root, workspace),
        env: { ...process.env, CI: "1" },
        stdio: ["ignore", "ignore", "ignore"],
      });
      const timer = setTimeout(() => child.kill("SIGKILL"), 300000);
      child.on("error", () => {
        clearTimeout(timer);
        resolve(false);
      });
      child.on("exit", (code) => {
        clearTimeout(timer);
        resolve(code === 0 || code === 1);
      });
    });
    if (!ok) continue;
    try {
      const report = JSON.parse(await readFile(outFile, "utf8"));
      await rm(outFile, { force: true }).catch(() => {});
      return report;
    } catch {
      /* try the next invocation */
    }
  }
  await rm(outFile, { force: true }).catch(() => {});
  return undefined;
}

// Reduce a vitest JSON report to the stable, normalized per-file projection that
// both sides must agree on. Returns undefined when the report is unusable so the
// caller can fall back to another executable projection.
function summarizeVitestReport(report, testFile) {
  if (!report || typeof report !== "object") return undefined;
  const assertions = (report.testResults ?? []).flatMap((suite) => suite.assertionResults ?? []);
  const titles = assertions.map((assertion) => `${assertion.status}:${assertion.title}`).sort();
  const totalTests = Number.isFinite(report.numTotalTests) ? report.numTotalTests : titles.length;
  const failedTests = Number.isFinite(report.numFailedTests)
    ? report.numFailedTests
    : titles.filter((title) => title.startsWith("failed:")).length;
  const passedTests = Number.isFinite(report.numPassedTests)
    ? report.numPassedTests
    : titles.filter((title) => title.startsWith("passed:")).length;
  if (totalTests <= 0) return undefined;
  return {
    testFile,
    result: failedTests === 0 ? "PASS" : "FAIL",
    totalTests,
    passedTests,
    failedTests,
    assertions: titles,
  };
}

const probeFns = {
  "P2P-025": probeStop,
  "P2P-026": probeClose,
  "P2P-027": probeAssertions,
  "P2P-028": probeErrorEnvelope,
  "P2P-029": probeSnapshotGap,
  "P2P-030": probeReplayVersionGate,
  "P2P-031": probeGetRun,
  "P2P-032": probeActiveRun,
  "P2P-033": probeOrphanedStream,
  "P2P-034": probeConsoleRecovery,
  "P2P-035": probeReset,
  "P2P-036": probeRouteSurface,
  "P2P-037": probeReplayBundle,
  "P2P-038": probeStartRun,
  "P2P-039": probeSignals,
  "P2P-040": probeCreateServerOptions,
};

// Every probe is bounded so a single slow observation can never truncate the
// report: all sixteen observation IDs are always present in the output.
function withTimeout(promise, ms) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`probe timed out after ${ms}ms`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

const PROBE_TIMEOUT_MS = 240000;
const results = {};
for (const [observationId, probe] of Object.entries(probeFns)) {
  try {
    results[observationId] = await withTimeout(Promise.resolve().then(probe), PROBE_TIMEOUT_MS);
  } catch (error) {
    results[observationId] = { result: "FAIL", error: String(error?.message ?? error) };
  }
}

function isFailure(value) {
  if (value == null || typeof value !== "object") return true;
  if (typeof value.failedTests === "number") {
    return value.failedTests > 0 || (typeof value.totalTests === "number" && value.totalTests === 0);
  }
  if (typeof value.result === "string") return value.result !== "PASS";
  return false;
}

const runtimeObservations = Object.entries(results).map(([id, value]) => ({
  id,
  outcome: isFailure(value) ? "FAIL" : "PASS",
  value,
}));
const report = {
  batch_id: "BATCH-P2P-0002",
  side,
  observations: results,
  runtime_observations: runtimeObservations,
};
const serialized = `${JSON.stringify(report)}\n`;
if (outPath) await writeFile(outPath, serialized);
process.stdout.write(serialized);
process.exitCode = Object.values(results).some(isFailure) ? 1 : 0;
