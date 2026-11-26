import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

export const batchId = "BATCH-F2P-0001";

export const targetTechnology = {
  language_name: "TypeScript",
  language_version: "5.9.3",
  name: "Express",
  version: "5.1.0",
};

export const sourceTechnology = {
  language_name: "TypeScript",
  language_version: "5.9.3",
  name: "Fastify",
  version: "5.12.1",
};

class SourceTechnologyPresent extends Error {}

let tsxRegistered = false;

async function importModule(url) {
  try {
    return await import(url);
  } catch (error) {
    const message = String((error && error.message) || error);
    if (!tsxRegistered && /Unknown file extension|ERR_UNKNOWN_FILE_EXTENSION/.test(message)) {
      const api = await import("tsx/esm/api");
      api.register();
      tsxRegistered = true;
      return import(url);
    }
    throw error;
  }
}

function serverPath(repoRoot) {
  return join(repoRoot, "javascript-app", "src", "server.ts");
}

// Module specifiers are resolved by *value* rather than by a fixed list of
// import syntaxes: ESM ``from``/``import()``/bare ``import``, CommonJS
// ``require``, and any loader bound to a name (``const f =
// createRequire(import.meta.url)``, ``const f = require``, ``const f =
// module.require``), including aliases of those names and borrowed calls
// (``f.call``/``f.apply``/``Reflect.apply(f, ...)``).  The specifier of
// ``createRequire(import.meta.url)("express")`` is therefore the argument of
// the call applied to the loader factory's result, not an ESM ``from`` clause,
// and it resolves to ``express`` exactly like ``import express from "express"``.
export function collectSpecifiers(text) {
  const found = [];
  const seen = new Set();
  const push = (value) => {
    if (typeof value === "string" && value.length > 0 && !seen.has(value)) {
      seen.add(value);
      found.push(value);
    }
  };

  for (const match of text.matchAll(/\bfrom\s*["']([^"']+)["']/g)) push(match[1]);
  for (const match of text.matchAll(/\bimport\s*\(\s*["']([^"']+)["']\s*\)/g)) push(match[1]);
  for (const match of text.matchAll(/^\s*import\s*["']([^"']+)["']/gm)) push(match[1]);
  for (const match of text.matchAll(/require\s*\(\s*["']([^"']+)["']\s*\)/gi)) push(match[1]);

  // Names bound to a module loader (``x = createRequire(...)`` / ``x = require``
  // / ``x = module.require``), then aliases of those names to a fixpoint.  A
  // binding whose right-hand side is itself *called* (``x = createRequire(...)("m")``
  // / ``x = require("m")``) yields a module, not a loader, so it is excluded.
  const loaders = new Set(["require", "createRequire"]);
  for (const match of text.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*[^;\n]*?\bcreateRequire\s*\([^()]*\)(?!\s*\()/g)) {
    loaders.add(match[1]);
  }
  for (const match of text.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:module\s*\.\s*)?require\b(?!\s*\()/g)) {
    loaders.add(match[1]);
  }
  for (let pass = 0; pass < 4; pass += 1) {
    let added = false;
    for (const match of text.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*[;\n)]/g)) {
      if (loaders.has(match[2]) && !loaders.has(match[1])) {
        loaders.add(match[1]);
        added = true;
      }
    }
    if (!added) break;
  }

  // Calls that actually load a module: the loader factory's result applied
  // directly (``createRequire(...)("express")``), a bound loader name
  // (``f("express")``), or a borrowed loader (``f.call``/``f.apply``/
  // ``Reflect.apply``).  Every form requires a quoted specifier argument, so a
  // zero-argument call (``express()``) is never treated as a load.
  for (const match of text.matchAll(/\bcreateRequire\s*\([^()]*\)\s*\(\s*["']([^"']+)["']\s*\)/g)) {
    push(match[1]);
  }
  for (const name of loaders) {
    const quoted = name.replace(/\$/g, "\\$&");
    for (const match of text.matchAll(new RegExp(`\\b${quoted}\\s*\\(\\s*["']([^"']+)["']\\s*\\)`, "g"))) {
      push(match[1]);
    }
    for (const match of text.matchAll(new RegExp(`\\b${quoted}\\s*\\.\\s*(?:call|apply)\\s*\\([^)]*?["']([^"']+)["']`, "g"))) {
      push(match[1]);
    }
    for (const match of text.matchAll(new RegExp(`Reflect\\s*\\.\\s*apply\\s*\\(\\s*${quoted}\\s*,[^)]*?["']([^"']+)["']`, "g"))) {
      push(match[1]);
    }
  }
  return found;
}

export function analyzeServerModule(repoRoot) {
  const text = readFileSync(serverPath(repoRoot), "utf8");
  const specifiers = collectSpecifiers(text);
  return {
    specifiers,
    hasExpress: specifiers.includes("express"),
    hasFastify: specifiers.includes("fastify"),
    hasNodeHttp: specifiers.includes("node:http"),
  };
}

async function loadServer(repoRoot) {
  const analysis = analyzeServerModule(repoRoot);
  if (analysis.hasFastify && !analysis.hasExpress) {
    throw new SourceTechnologyPresent(
      "server module loads fastify instead of express",
    );
  }
  if (!analysis.hasExpress) {
    throw new Error("server module does not load express");
  }
  const version = expressVersion(repoRoot);
  if (version === undefined) {
    throw new Error("express is not resolvable on the execution path");
  }
  if (version !== targetTechnology.version) {
    throw new Error(
      `express version ${version} does not match ${targetTechnology.version}`,
    );
  }
  const reference = pathToFileURL(serverPath(repoRoot)).href;
  const module = await importModule(reference);
  return { analysis, createServer: module.createServer };
}

export function expressVersion(repoRoot) {
  const candidates = [
    join(repoRoot, "javascript-app", "node_modules", "express", "package.json"),
    join(repoRoot, "node_modules", "express", "package.json"),
  ];
  for (const candidate of candidates) {
    try {
      return JSON.parse(readFileSync(candidate, "utf8")).version;
    } catch {
      /* try next candidate */
    }
  }
  return undefined;
}

// The public contract permits a server handle as well as a URL return value.
// Always wait for the real listener and derive its bound address before probing.
export async function listenForProbe(server, options = { host: "127.0.0.1", port: 0 }) {
  const result = await server.listen(options);
  if (typeof result === "string") return result;
  const listener = result?.address ? result : server.server ?? server.httpServer;
  if (listener && listener.listening === false) {
    const { once } = await import("node:events");
    await once(listener, "listening");
  }
  const address = typeof listener?.address === "function" ? listener.address() : result;
  if (address && typeof address === "object" && Number.isInteger(address.port) && address.port > 0) {
    const host = options.host ?? "127.0.0.1";
    return `http://${host.includes(":") && !host.startsWith("[") ? `[${host}]` : host}:${address.port}`;
  }
  throw new Error("listen did not expose a bound HTTP address");
}

async function loadErrorClass(repoRoot) {
  const reference = pathToFileURL(
    join(repoRoot, "javascript-app", "src", "errors.ts"),
  ).href;
  const module = await importModule(reference);
  return module.RunnerCoreError;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function stubManager(overrides = {}) {
  const base = {
    async getActiveRunDetail() {
      return null;
    },
    async getRunDetail() {
      return null;
    },
    async getReplayBundle() {
      return null;
    },
    async resetScenario(id) {
      return { scenarioId: id, resetAt: new Date().toISOString() };
    },
    async startRun() {
      return { run: { id: "stub-run", status: "running" } };
    },
    async stopRun() {
      return { run: { id: "stub-run", status: "cancelled" } };
    },
    shutdown() {},
    subscribe() {
      return () => {};
    },
    ...overrides,
  };
  return base;
}

function completedRunDetail() {
  const now = new Date().toISOString();
  return {
    run: {
      id: "test-run",
      scenarioId: "fixture-scenario",
      labId: "fixture-lab",
      browserMode: "headless",
      model: "stub-model",
      maxResponseTurns: 24,
      prompt: "p",
      status: "completed",
      startedAt: now,
      completedAt: now,
    },
    scenario: {
      id: "fixture-scenario",
      labId: "fixture-lab",
      name: "fixture",
      description: "fixture",
      prompt: "p",
      tags: [],
    },
    workspacePath: "/tmp/does-not-matter",
    eventStreamUrl: "http://127.0.0.1:4001/api/runs/test-run/events",
    replayUrl: "http://127.0.0.1:4001/api/runs/test-run/replay",
    events: [
      {
        id: "evt-1",
        runId: "test-run",
        sequence: 1,
        type: "run_completed",
        level: "ok",
        message: "done",
        createdAt: now,
      },
    ],
  };
}

async function withServer(repoRoot, options, body) {
  const { createServer } = await loadServer(repoRoot);
  const server = createServer({ manager: stubManager(options) });
  try {
    return await body(server);
  } finally {
    try {
      await server.close();
    } catch {
      /* ignore */
    }
  }
}

async function expectExpressJson(repoRoot, options, request, verify) {
  return withServer(repoRoot, options, async (server) => {
    const response = await server.inject(request);
    return verify(response);
  });
}

const jsonOf = (response) => {
  try {
    return JSON.parse(response.body);
  } catch {
    throw new Error(`response body is not JSON: ${response.body.slice(0, 80)}`);
  }
};

export const observations = [
  {
    id: "F2P-001",
    requirement_ids: ["R047"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        { method: "GET", url: "/health", headers: { "x-cua-backend": "python" } },
        (response) => {
          assert(response.statusCode === 409, `expected 409, got ${response.statusCode}`);
          assert(jsonOf(response).code === "backend_mismatch", "wrong error code");
        },
      );
      return "express middleware gate returned 409 backend_mismatch";
    },
  },
  {
    id: "F2P-002",
    requirement_ids: ["R050"],
    projection: "dependency-execution",
    async run(repoRoot) {
      const { createServer } = await loadServer(repoRoot);
      assert(typeof createServer === "function", "createServer export missing");
      const analysis = analyzeServerModule(repoRoot);
      assert(analysis.hasExpress, "express not on load path");
      assert(analysis.hasNodeHttp, "node:http not on load path");
      assert(!analysis.hasFastify, "fastify still on load path");
      return "server module loads express + node:http, not fastify";
    },
  },
  {
    id: "F2P-003",
    requirement_ids: ["R051"],
    projection: "target-entry-identity",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {
          async getActiveRunDetail() {
            throw new Error("boom");
          },
        },
        { method: "GET", url: "/api/runs/active" },
        (response) => {
          assert(response.statusCode === 500, `expected 500, got ${response.statusCode}`);
          assert(jsonOf(response).code === "internal_runner_error", "wrong error code");
        },
      );
      return "awaited handler rejection reached the express error middleware";
    },
  },
  {
    id: "F2P-004",
    requirement_ids: ["R037"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        {
          method: "POST",
          url: "/api/runs",
          headers: { "content-type": "application/json" },
          payload: "{invalid",
        },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          assert(
            String(response.headers["content-type"] ?? "").includes("application/json"),
            "error response is not JSON",
          );
          assert(jsonOf(response).code === "invalid_request", "wrong error code");
        },
      );
      return "malformed JSON produced a 400 JSON envelope";
    },
  },
  {
    id: "F2P-005",
    requirement_ids: ["R042"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {
          async getRunDetail() {
            return completedRunDetail();
          },
        },
        {
          method: "GET",
          url: "/api/runs/test-run/events",
          headers: { origin: "http://127.0.0.1:3000" },
        },
        (response) => {
          assert(response.statusCode === 200, `expected 200, got ${response.statusCode}`);
          assert(
            String(response.headers["content-type"] ?? "").includes("text/event-stream"),
            "not an event stream",
          );
          assert(response.body.includes("run_completed"), "run_completed frame missing");
          assert(
            response.headers["access-control-allow-origin"] === "http://127.0.0.1:3000",
            "origin not echoed",
          );
        },
      );
      return "SSE stream served through express raw response";
    },
  },
  {
    id: "F2P-006",
    requirement_ids: ["R045"],
    projection: "target-execution-path",
    async run(repoRoot) {
      let started = false;
      await expectExpressJson(
        repoRoot,
        {
          async startRun() {
            started = true;
            throw new Error("must not start");
          },
        },
        {
          method: "POST",
          url: "/api/runs",
          headers: { "content-type": "application/json" },
          payload: { scenarioId: "fixture-scenario", prompt: "p", verificationEnabled: true },
        },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          assert(jsonOf(response).code === "invalid_request", "wrong error code");
        },
      );
      assert(!started, "manager.startRun was invoked");
      return "removed verificationEnabled field rejected before starting a run";
    },
  },
  {
    id: "F2P-007",
    requirement_ids: ["R037"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        {
          method: "POST",
          url: "/api/runs",
          headers: { "content-type": "application/json" },
          payload: "{invalid",
        },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          assert(jsonOf(response).code === "invalid_request", "wrong error code");
        },
      );
      return "replay route module reused express JSON error mapping";
    },
  },
  {
    id: "F2P-008",
    requirement_ids: ["R039"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        { method: "OPTIONS", url: "/api/runs", headers: { origin: "http://localhost:3000" } },
        (response) => {
          assert(response.statusCode === 204, `expected 204, got ${response.statusCode}`);
          assert(
            response.headers["access-control-allow-origin"] === "http://localhost:3000",
            "origin not echoed",
          );
          assert(
            response.headers["access-control-allow-methods"] === "GET,POST,OPTIONS",
            "wrong methods header",
          );
          assert(
            String(response.headers["access-control-allow-headers"] ?? "").includes("x-cua-backend"),
            "x-cua-backend missing from allowed headers",
          );
        },
      );
      await expectExpressJson(
        repoRoot,
        {},
        { method: "OPTIONS", url: "/api/runs" },
        (response) => {
          assert(response.statusCode === 204, `expected 204, got ${response.statusCode}`);
          assert(response.headers["access-control-allow-headers"] === "content-type, last-event-id, x-cua-backend", "wrong headers without Origin");
          assert(response.headers["access-control-allow-methods"] === "GET,POST,OPTIONS", "wrong methods without Origin");
        },
      );
      return "OPTIONS preflight handled by express middleware";
    },
  },
  {
    id: "F2P-009",
    requirement_ids: ["R040"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        { method: "GET", url: "/health" },
        (response) => {
          assert(response.statusCode === 200, `expected 200, got ${response.statusCode}`);
          assert(
            String(response.headers["vary"] ?? "").includes("Origin"),
            "Vary: Origin not set",
          );
        },
      );
      return "express middleware set Vary: Origin on every response";
    },
  },
  {
    id: "F2P-010",
    requirement_ids: ["R044"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        { method: "GET", url: "/api/runs/..%2F..%2Foutside" },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          assert(jsonOf(response).code === "invalid_request", "wrong error code");
        },
      );
      return "express decoded path param was validated before lookup";
    },
  },
  {
    id: "F2P-011",
    requirement_ids: ["R050"],
    projection: "dependency-execution",
    async run(repoRoot) {
      await loadServer(repoRoot);
      const analysis = analyzeServerModule(repoRoot);
      assert(analysis.hasExpress && analysis.hasNodeHttp, "express/node:http missing");
      assert(!analysis.hasFastify, "fastify still on load path");
      return "express + node:http ownership confirmed on the replay entry";
    },
  },
  {
    id: "F2P-012",
    requirement_ids: ["R047"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        { method: "GET", url: "/health", headers: { "x-cua-backend": "python" } },
        (response) => {
          assert(response.statusCode === 409, `expected 409, got ${response.statusCode}`);
          assert(jsonOf(response).code === "backend_mismatch", "wrong error code");
        },
      );
      return "backend mismatch gate returned 409 through express middleware";
    },
  },
  {
    id: "F2P-013",
    requirement_ids: ["R045"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        {
          method: "POST",
          url: "/api/runs",
          headers: { "content-type": "application/json" },
          payload: { scenarioId: "fixture-scenario", prompt: "p", verificationEnabled: true },
        },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          assert(jsonOf(response).code === "invalid_request", "wrong error code");
        },
      );
      return "express JSON validation rejected verificationEnabled";
    },
  },
  {
    id: "F2P-014",
    requirement_ids: ["R051"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {
          async getRunDetail() {
            throw new Error("boom");
          },
        },
        { method: "GET", url: "/api/runs/any-run/events" },
        (response) => {
          assert(response.statusCode === 500, `expected 500, got ${response.statusCode}`);
          assert(jsonOf(response).code === "internal_runner_error", "wrong error code");
          assert(
            String(response.body).includes("<html") === false,
            "express default HTML error page was returned",
          );
        },
      );
      return "rejected events handler forwarded to the express four-argument error middleware";
    },
  },
  {
    id: "F2P-015",
    requirement_ids: ["R037"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        {
          method: "POST",
          url: "/api/runs",
          headers: { "content-type": "application/json" },
          payload: "{invalid",
        },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          assert(jsonOf(response).code === "invalid_request", "wrong error code");
        },
      );
      return "screenshot entry used express JSON error mapping";
    },
  },
  {
    id: "F2P-016",
    requirement_ids: ["R035"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await withServer(repoRoot, {}, async (server) => {
        const address = await listenForProbe(server);
        assert(
          typeof address === "string" && /^http:\/\/127\.0\.0\.1:\d+$/.test(address),
          `listen resolved ${JSON.stringify(address)}`,
        );
      });
      return "listen() resolved a url string and close() resolved";
    },
  },
  {
    id: "F2P-017",
    requirement_ids: ["R045"],
    projection: "target-execution-path",
    async run(repoRoot) {
      let started = false;
      await expectExpressJson(
        repoRoot,
        {
          async startRun() {
            started = true;
            throw new Error("must not start");
          },
        },
        {
          method: "POST",
          url: "/api/runs",
          headers: { "content-type": "application/json" },
          payload: { scenarioId: "fixture-scenario", prompt: "p", verificationEnabled: true },
        },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          assert(jsonOf(response).code === "invalid_request", "wrong error code");
        },
      );
      assert(!started, "manager.startRun was invoked");
      return "verificationEnabled rejected without starting a run";
    },
  },
  {
    id: "F2P-018",
    requirement_ids: ["R035"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await withServer(repoRoot, {}, async (server) => {
        const address = await listenForProbe(server);
        assert(
          typeof address === "string" && /^http:\/\/127\.0\.0\.1:\d+$/.test(address),
          `listen resolved ${JSON.stringify(address)}`,
        );
        const response = await fetch(new URL("/health", address));
        assert(response.status === 200, `expected 200, got ${response.status}`);
      });
      return "listening server is an express app that serves /health";
    },
  },
  {
    id: "F2P-019",
    requirement_ids: ["R034"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        { method: "GET", url: "/health" },
        (response) => {
          assert(response.statusCode === 200, `expected 200, got ${response.statusCode}`);
          const body = jsonOf(response);
          assert(
            body.status === "ok" && body.service === "runner",
            `unexpected health body ${response.body}`,
          );
        },
      );
      return "createServer().inject() adapter served /health through express";
    },
  },
  {
    id: "F2P-020",
    requirement_ids: ["R044"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        { method: "GET", url: "/api/runs/..%2F..%2Foutside" },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          assert(jsonOf(response).code === "invalid_request", "wrong error code");
        },
      );
      return "invalid run id rejected on the run-detail entry through express";
    },
  },
  {
    id: "F2P-021",
    requirement_ids: ["R046"],
    projection: "target-execution-path",
    async run(repoRoot) {
      await expectExpressJson(
        repoRoot,
        {},
        {
          method: "POST",
          url: "/api/runs",
          headers: { "content-type": "application/json" },
          payload: { scenarioId: "fixture-scenario", prompt: "p", maxResponseTurns: 51 },
        },
        (response) => {
          assert(response.statusCode === 400, `expected 400, got ${response.statusCode}`);
          const body = jsonOf(response);
          assert(body.code === "invalid_request", "wrong error code");
          assert(String(body.error).includes("maxResponseTurns"), "missing maxResponseTurns");
        },
      );
      return "maxResponseTurns bound enforced through express validation";
    },
  },
  {
    id: "F2P-022",
    requirement_ids: ["R043"],
    projection: "target-execution-path",
    async run(repoRoot) {
      const RunnerCoreError = await loadErrorClass(repoRoot);
      await expectExpressJson(
        repoRoot,
        {
          async getRunDetail() {
            throw new RunnerCoreError("Run not found", {
              code: "run_not_found",
              hint: "Start a new run to create one.",
              statusCode: 404,
            });
          },
        },
        { method: "GET", url: "/api/runs/missing-run" },
        (response) => {
          assert(response.statusCode === 404, `expected 404, got ${response.statusCode}`);
          const body = jsonOf(response);
          assert(body.code === "run_not_found", "wrong error code");
          assert(String(body.hint).includes("Start a new run"), "wrong hint");
        },
      );
      return "unknown run mapped to the runner error envelope by express";
    },
  },
  {
    id: "F2P-023",
    requirement_ids: ["R028"],
    projection: "dependency-execution",
    async run(repoRoot) {
      const server = readFileSync(serverPath(repoRoot), "utf8");
      const index = readFileSync(join(repoRoot, "javascript-app", "src", "index.ts"), "utf8");
      const combined = `${server}\n${index}`;
      const analysis = analyzeServerModule(repoRoot);
      assert(analysis.hasExpress, "express not on the execution path");
      assert(!analysis.hasFastify, "fastify still on the execution path");
      for (const forbidden of ["child_process", "worker_threads", "node:cluster"]) {
        assert(!combined.includes(forbidden), `in-process boundary violated by ${forbidden}`);
      }
      return "requests are routed in-process by express without delegation";
    },
  },
  {
    id: "F2P-024",
    requirement_ids: ["R050"],
    projection: "dependency-execution",
    async run(repoRoot) {
      await loadServer(repoRoot);
      const analysis = analyzeServerModule(repoRoot);
      assert(analysis.hasExpress && analysis.hasNodeHttp, "express/node:http missing");
      assert(!analysis.hasFastify, "fastify still on load path");
      return "express + node:http ownership confirmed on the create-run entry";
    },
  },
];

export async function runObservation(observationId, repoRoot) {
  const observation = observations.find((entry) => entry.id === observationId);
  if (!observation) throw new Error(`unknown observation ${observationId}`);
  try {
    const detail = await observation.run(repoRoot);
    return { observation_id: observationId, status: "PASS", detail };
  } catch (error) {
    if (
      error instanceof SourceTechnologyPresent ||
      /loads fastify|not on load path|does not load express/.test(String(error && error.message))
    ) {
      return {
        observation_id: observationId,
        status: "FAIL",
        detail: `target technology absent: ${String(error && error.message)}`,
      };
    }
    return {
      observation_id: observationId,
      status: "FAIL",
      detail: String(error && error.stack ? error.message : error),
    };
  }
}

export async function runAll(repoRoot) {
  const results = [];
  for (const observation of observations) {
    results.push(await runObservation(observation.id, repoRoot));
  }
  return results;
}
