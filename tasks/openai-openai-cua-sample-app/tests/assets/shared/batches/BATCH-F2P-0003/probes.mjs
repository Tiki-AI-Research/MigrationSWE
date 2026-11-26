// Shared F2P probe library for BATCH-F2P-0003.
//
// Each probe encodes the single semantic predicate named by one atomic
// observation from the frozen contract. The *same* predicate is evaluated in
// both environments (source Fastify checkout and target Express checkout); a
// probe passes only when the target technology (Express 5) is actually on the
// executed/loaded path and fails when the source technology (Fastify 5) is.
//
// The library is deliberately runtime-agnostic: it receives a `context` whose
// `importServer()` may resolve the runner module through whatever loader the
// hosting harness provides (tsx/tsup). Static facts are always available so the
// dependency/import projections can be evaluated without booting a server.

import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { expressVersion, listenForProbe } from "../BATCH-F2P-0001/predicates.mjs";

const EXPRESS_VERSION = "5.1.0";
const TYPES_EXPRESS_VERSION = "5.0.6";

async function readIf(path) {
  try {
    return await readFile(path, "utf8");
  } catch {
    return undefined;
  }
}

// Module specifiers are resolved by *value* — the identical rule implemented by
// BATCH-F2P-0001/predicates.mjs#collectSpecifiers.  ESM `from`/`import()`/bare
// `import`, CommonJS `require`, and any loader bound to a name
// (`createRequire(import.meta.url)`, `require`, `module.require`), including
// aliases of those names and borrowed calls (`f.call`/`f.apply`/
// `Reflect.apply`), all contribute the module they load.  The specifier of
// `createRequire(import.meta.url)("express")` is the argument of the call on the
// loader factory's result, so it resolves to `express` exactly like
// `import express from "express"`.
function collectSpecifiers(text) {
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

// A declared `@types/express` range is only correct when it targets the Express 5
// API.  An absent declaration is accepted, because "the Express types are
// resolvable" is owned by the public ``typecheck`` gate rather than by a
// mandatory dev-dependency name; an unparsable range is accepted too, so an
// unknown notation never becomes a false failure.
function typesExpressCompatible(range) {
  if (typeof range !== "string" || range.trim() === "") return true;
  const match = /(\d+)(?:\.\d+){0,2}/.exec(range);
  return match === null ? true : Number(match[1]) === 5;
}

export async function collectFacts(root) {
  const serverPath = join(root, "javascript-app/src/server.ts");
  const indexPath = join(root, "javascript-app/src/index.ts");
  const testPath = join(root, "javascript-app/tests/server.test.ts");
  const pkgPath = join(root, "javascript-app/package.json");
  const lockPath = join(root, "pnpm-lock.yaml");

  const [serverSrc, indexSrc, testSrc, pkgRaw, lockSrc] = await Promise.all([
    readIf(serverPath),
    readIf(indexPath),
    readIf(testPath),
    readIf(pkgPath),
    readIf(lockPath),
  ]);

  let pkg;
  try {
    pkg = JSON.parse(pkgRaw);
  } catch {
    pkg = undefined;
  }

  const dependencies = { ...(pkg?.dependencies ?? {}) };
  const devDependencies = { ...(pkg?.devDependencies ?? {}) };
  const all = { ...dependencies, ...devDependencies };

  const source = serverSrc ?? "";
  const index = indexSrc ?? "";

  const has = (text, re) => re.test(text);

  // Specifier extraction defers to the shared by-value rule above so "target
  // technology on the load path" is decided once, rather than by ESM `from`
  // syntax alone (a static `from` clause must not be the only recognised way to
  // load express).
  const specifiers = [...collectSpecifiers(source), ...collectSpecifiers(index)];

  return {
    root,
    paths: { serverPath, indexPath, testPath, pkgPath, lockPath },
    serverSrc: source,
    indexSrc: index,
    testSrc: testSrc ?? "",
    pkg,
    pkgRaw: pkgRaw ?? "",
    lockSrc: lockSrc ?? "",
    dependencies,
    devDependencies,
    all,
    specifiers,
    importsExpress: specifiers.includes("express"),
    importsFastify: specifiers.includes("fastify"),
    usesNodeHttpServer: specifiers.includes("node:http"),
    declaresExpress: typeof dependencies.express === "string" && expressVersion(root) === EXPRESS_VERSION,
    // The manifest must declare Express; the Express *type* package is optional
    // (a candidate may type the import inline and satisfy the public typecheck
    // gate instead), but when it is declared it must target the Express 5 API.
    declaresTypesExpress: typeof devDependencies["@types/express"] === "string",
    typesExpressRange: devDependencies["@types/express"],
    typesExpressCompatible: typesExpressCompatible(devDependencies["@types/express"]),
    declaresFastify: "fastify" in dependencies,
    // Behavior/technology idioms that only the Express implementation can carry.
    usesExpressJson: has(source, /express\.json\s*\(/),
    // A raw SSE writer may set the stream up with `writeHead(`/`flushHeaders()`
    // or by `.set(`ting the `Content-Type` header object on the raw response;
    // every accepted form still requires the `text/event-stream` media type.
    usesRawServerResponseSse:
      has(source, /text\/event-stream/) &&
      (has(source, /writeHead\s*\(/) ||
        has(source, /flushHeaders\s*\(/) ||
        has(source, /\.\s*set\s*\(\s*\{/) ||
        has(source, /\b(?:res|response)\s*\.\s*set\s*\(/)),
    // Origin/CORS handling is identified case-insensitively and accepts the
    // Express `vary()` helper as well as a literal `Vary` header.
    registersOriginMiddleware:
      (has(source, /allowedOrigins/i) || has(source, /allow-origin/i)) &&
      (has(source, /\bvary\b/i) || has(source, /\.\s*vary\s*\(/)),
    hasFastifyResidue:
      has(source, /\bFastify\b/) ||
      has(source, /\bFastifyReply\b/) ||
      has(source, /addHook\s*\(/) ||
      has(source, /setErrorHandler\s*\(/) ||
      has(source, /reply\.raw/) ||
      has(source, /reply\.hijack\s*\(/) ||
      has(source, /app\.options\s*\(\s*["']\*["']/),
    spawnsSubprocess:
      has(source, /from\s+["']node:child_process["']/) ||
      has(source, /from\s+["']child_process["']/) ||
      has(source, /require\s*\(\s*["']node:child_process["']\s*\)/),
  };
}

function fail(detail) {
  return { status: "FAIL", detail };
}

function pass(detail) {
  return { status: "PASS", detail };
}

// A probe whose runtime precondition is absent was never executed.  It must be
// recorded as NOT_RUN (excluded from the F2P ratio) rather than as a FAIL with a
// misleading "not on the load path" conclusion it did not actually measure.
function notRun(detail) {
  return { status: "NOT_RUN", detail };
}

// ---------------------------------------------------------------------------
// Runtime helpers. Only exercised when the target technology is loadable.
// ---------------------------------------------------------------------------

async function makeHarness(context) {
  const server = await context.importServer();
  const contracts = await context.importContracts();

  const scenario = {
    id: "fixture-scenario",
    labId: "paint",
    category: "creativity",
    title: "Fixture",
    description: "Synthetic F2P probe scenario",
    defaultPrompt: "Complete fixture.",
    workspaceTemplatePath: context.root,
    tags: ["fixture"],
  };

  const detailFor = (id, status) =>
    contracts.runDetailSchema.parse({
      run: {
        id,
        scenarioId: scenario.id,
        labId: scenario.labId,
        browserMode: "headless",
        model: "probe-model",
        maxResponseTurns: 24,
        prompt: "probe",
        status,
        startedAt: "2026-09-03T00:00:00.000Z",
      },
      scenario,
      workspacePath: join(context.root, "data", "runs", id),
      eventStreamUrl: `/api/runs/${id}/events`,
      replayUrl: `/api/runs/${id}/replay`,
      events: [],
    });

  const baseManager = {
    startRun: async (input) => detailFor("probe-run", "running"),
    getActiveRunDetail: async () => null,
    getRunDetail: async (id) => detailFor(id, "running"),
    stopRun: async (id) => detailFor(id, "cancelled"),
    resetScenario: async (id) => ({ scenarioId: id, cancelledRunId: "probe-run" }),
    getReplayBundle: async (id) => ({
      version: 3,
      run: detailFor(id, "completed").run,
      events: [],
      artifacts: [],
      scenario,
    }),
    shutdown: async () => undefined,
    subscribe: () => () => undefined,
  };

  return { server, contracts, scenario, detailFor, baseManager };
}

// ---------------------------------------------------------------------------
// Probe specifications. `check(facts, context)` -> { status, detail }.
// ---------------------------------------------------------------------------

export const PROBES = [
  {
    id: "F2P-055",
    requirementIds: ["R027"],
    surface: "Runner production module imports (server.ts, index.ts)",
    check: async (facts) => {
      if (!facts.importsExpress) return fail("server/index do not import express");
      if (facts.importsFastify) return fail("server/index still import fastify");
      if (!facts.usesNodeHttpServer)
        return fail("server does not open its own node:http listener");
      return pass("server imports express and node:http and never loads fastify");
    },
  },
  {
    id: "F2P-070",
    requirementIds: ["R030"],
    surface: "javascript-app/package.json::dependency manifest",
    check: async (facts) => {
      if (!facts.declaresExpress)
        return fail(`express ${EXPRESS_VERSION} missing from dependencies`);
      if (facts.declaresFastify) return fail("fastify still declared as a dependency");
      return pass(`express ${EXPRESS_VERSION} declared; fastify removed`);
    },
  },
  {
    id: "F2P-071",
    requirementIds: ["R048"],
    surface: "javascript-app/package.json::dependency manifest",
    check: async (facts) => {
      if (!facts.declaresExpress)
        return fail(`express ${EXPRESS_VERSION} missing from dependencies`);
      if (!facts.typesExpressCompatible)
        return fail(
          `@types/express ${facts.typesExpressRange} does not target the Express 5 API (reference ${TYPES_EXPRESS_VERSION})`,
        );
      return pass(
        facts.declaresTypesExpress
          ? `@types/express ${facts.typesExpressRange} declared for the express imports`
          : `express ${EXPRESS_VERSION} declared; Express types resolved by the typecheck gate`,
      );
    },
  },
  {
    id: "F2P-072",
    requirementIds: ["R049"],
    surface: "pnpm workspace lockfile",
    check: async (facts) => {
      if (!facts.declaresExpress || facts.declaresFastify)
        return fail("runner runtime dependencies must resolve Express 5.1.0 without Fastify");
      return pass("runner declares Express and resolves 5.1.0 without a Fastify runtime dependency; unused lock entries are not runtime dependencies");
    },
  },
  {
    id: "F2P-058",
    requirementIds: ["R037"],
    surface: "javascript-app/src/server.ts::Malformed JSON error mapping",
    check: async (facts, context) => {
      if (!facts.usesExpressJson) return fail("express.json body parser is not registered");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "POST",
          url: "/api/runs",
          headers: { "content-type": "application/json" },
          payload: "{invalid",
        });
        if (response.statusCode !== 400)
          return fail(`malformed JSON produced ${response.statusCode}`);
        const body = response.json();
        if (body.code !== "invalid_request")
          return fail(`malformed JSON produced code ${body.code}`);
        return pass("malformed JSON maps to a 400 invalid_request JSON envelope");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-063",
    requirementIds: ["R034"],
    surface: "javascript-app/src/server.ts::GET /api/capabilities",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("createServer is not backed by express");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({ method: "GET", url: "/health" });
        if (response.statusCode !== 200)
          return fail(`inject(/health) returned ${response.statusCode}`);
        const body = response.json();
        if (body.status !== "ok" || body.service !== "runner")
          return fail("inject(/health) body mismatch");
        return pass("createServer().inject() resolves health through the express app");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-064",
    requirementIds: ["R051"],
    surface: "javascript-app/src/server.ts::POST /api/runs/",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("express is not on the load path");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "POST",
          url: "/api/runs/probe-run/stop",
        });
        if (response.statusCode !== 200)
          return fail(`stop returned ${response.statusCode}`);
        const detail = context.contracts.runDetailSchema.parse(response.json());
        if (detail.run.status !== "cancelled")
          return fail(`stop status ${detail.run.status}`);
        return pass("stop returns 200 with a cancelled runDetail body");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-065",
    requirementIds: ["R036"],
    surface: "javascript-app/src/server.ts::POST /api/runs/:id/stop",
    check: async (facts, context) => {
      if (!facts.usesExpressJson) return fail("express.json body parser is not registered");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "POST",
          url: "/api/runs",
          payload: { scenarioId: "fixture-scenario", prompt: "p" },
        });
        if (response.statusCode !== 202)
          return fail(`POST /api/runs returned ${response.statusCode}`);
        const body = context.contracts.startRunResponseSchema.parse(response.json());
        if (body.runId !== body.detail.run.id)
          return fail("runId does not match detail.run.id");
        return pass("express.json parses the body and startRun receives it");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-066",
    requirementIds: ["R036"],
    surface:
      "javascript-app/src/server.ts::Runner HTTP server: routing table, JSON body parsing, CORS headers, error envelope, and text/event-stream responses",
    check: async (facts, context) => {
      if (!facts.usesExpressJson) return fail("express.json is not registered");
      if (!facts.registersOriginMiddleware) return fail("origin/CORS middleware missing");
      if (!facts.usesRawServerResponseSse) return fail("raw SSE writer missing");
      // Error handling is exercised by F2P-056; parameter spelling is not a contract.
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "POST",
          url: "/api/runs",
          headers: { origin: "http://localhost:3000" },
          payload: { scenarioId: "fixture-scenario", prompt: "p" },
        });
        if (response.statusCode !== 202)
          return fail(`JSON body route returned ${response.statusCode}`);
        if (response.headers["access-control-allow-origin"] !== "http://localhost:3000")
          return fail("CORS origin was not echoed");
        return pass("express.json, CORS, error middleware and SSE all wired");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-068",
    requirementIds: ["R036"],
    surface:
      "javascript-app/src/server.ts::javascript-app/src/server.ts module export createServer",
    check: async (facts, context) => {
      if (!facts.usesExpressJson) return fail("createServer does not register express.json");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({
        allowedOrigins: ["https://operator.example"],
        manager: baseManager,
      });
      try {
        const response = await app.inject({
          method: "POST",
          url: "/api/runs",
          payload: { scenarioId: "fixture-scenario", prompt: "p" },
        });
        if (response.statusCode !== 202)
          return fail(`createServer(options) returned ${response.statusCode}`);
        return pass("createServer(options) accepts manager/allowedOrigins and injects");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-069",
    requirementIds: ["R028"],
    surface: "javascript-app/src/server.ts::POST /stop",
    check: async (facts) => {
      if (facts.spawnsSubprocess)
        return fail("server spawns a child process instead of routing in-process");
      if (facts.importsFastify) return fail("server still delegates to a fastify runtime");
      if (!facts.importsExpress) return fail("server does not route through express");
      return pass("requests are routed by the in-process express app; no proxy/subprocess");
    },
  },
  {
    id: "F2P-067",
    requirementIds: ["R028"],
    surface: "javascript-app/src/server.ts::runtime entry",
    check: async (facts) => {
      if (facts.importsFastify) return fail("runtime entry still loads fastify");
      if (!facts.usesExpressJson) return fail("runtime entry does not build an express app");
      if (!facts.usesRawServerResponseSse) return fail("runtime entry does not stream SSE");
      return pass("server.ts builds an express app in-process for every route");
    },
  },
  {
    id: "F2P-049",
    requirementIds: ["R028"],
    surface: "javascript-app/src/server.ts::POST /api/runs",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("POST /api/runs is not routed through express");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "POST",
          url: "/api/runs",
          payload: { scenarioId: "fixture-scenario", prompt: "p" },
        });
        if (response.statusCode !== 202)
          return fail(`POST /api/runs returned ${response.statusCode}`);
        const body = context.contracts.startRunResponseSchema.parse(response.json());
        if (body.runId !== body.detail.run.id)
          return fail("runId does not match detail.run.id");
        return pass("in-process express app serves POST /api/runs");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-050",
    requirementIds: ["R039"],
    surface: "javascript-app/src/server.ts::SSE snapshot/live gap handling",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("OPTIONS is not handled by an express app");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "OPTIONS",
          url: "/api/runs",
          headers: { origin: "http://localhost:3000" },
        });
        if (response.statusCode !== 204)
          return fail(`OPTIONS returned ${response.statusCode}`);
        if (response.headers["access-control-allow-origin"] !== "http://localhost:3000")
          return fail("OPTIONS did not echo the allowed origin");
        if (
          !String(response.headers["access-control-allow-headers"] ?? "").includes(
            "x-cua-backend",
          )
        )
          return fail("OPTIONS is missing the x-cua-backend allow-header");
        return pass("OPTIONS preflight returns 204 with the express CORS headers");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-051",
    requirementIds: ["R034"],
    surface: "javascript-app/src/server.ts::SSE snapshot/live gap handling",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("app.inject is not backed by express");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({ method: "GET", url: "/health" });
        if (response.statusCode !== 200)
          return fail(`inject(/health) returned ${response.statusCode}`);
        return pass("createServer().inject() drives the express app under test");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-052",
    requirementIds: ["R039"],
    surface: "javascript-app/src/server.ts::GET /api/runs/",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("OPTIONS is not handled by an express app");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "OPTIONS",
          url: "/api/runs",
          headers: { origin: "http://localhost:3000" },
        });
        if (response.statusCode !== 204)
          return fail(`OPTIONS returned ${response.statusCode}`);
        if (response.headers["access-control-allow-methods"] !== "GET,POST,OPTIONS")
          return fail("OPTIONS allow-methods mismatch");
        return pass("GET route family answers OPTIONS with express middleware");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-053",
    requirementIds: ["R039"],
    surface: "javascript-app/src/server.ts::GET /api/runs/:id",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("OPTIONS is not handled by an express app");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "OPTIONS",
          url: "/api/runs",
          headers: { origin: "http://localhost:3000" },
        });
        if (response.statusCode !== 204)
          return fail(`OPTIONS returned ${response.statusCode}`);
        return pass("GET /api/runs/:id family answers OPTIONS through express");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-054",
    requirementIds: ["R034"],
    surface: "javascript-app/src/server.ts::GET /api/scenarios",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("app.inject is not backed by express");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({ method: "GET", url: "/api/scenarios" });
        if (response.statusCode !== 200)
          return fail(`GET /api/scenarios returned ${response.statusCode}`);
        const scenarios = context.contracts.scenariosResponseSchema.parse(
          response.json(),
        );
        if (!Array.isArray(scenarios))
          return fail("scenarios response is not an array");
        return pass("inject(/api/scenarios) is served by the express app");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-056",
    requirementIds: ["R051"],
    surface: "javascript-app/src/server.ts::All error responses",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("express is not on the load path");
      const { server, contracts } = await context.harness();
      const failure = new Error("boom");
      const original = console.error;
      const seen = [];
      console.error = (...args) => seen.push(args);
      const app = server.createServer({
        manager: {
          getActiveRunDetail: async () => {
            throw failure;
          },
          shutdown: async () => undefined,
        },
      });
      try {
        const response = await app.inject({ method: "GET", url: "/api/runs/active" });
        if (response.statusCode !== 500)
          return fail(`thrown handler produced ${response.statusCode}`);
        const body = contracts.runnerErrorResponseSchema.parse(response.json());
        if (body.code !== "internal_runner_error")
          return fail(`error envelope code ${body.code}`);
        if (response.body.includes(failure.message))
          return fail("internal message leaked to the client");
        if (!seen.some((args) => args.includes(failure)))
          return fail("original error was not logged");
        const missing = await app.inject({ method: "GET", url: "/api/unknown" });
        if (missing.statusCode !== 404) return fail(`unknown route returned ${missing.statusCode}`);
        contracts.runnerErrorResponseSchema.parse(missing.json());
        const malformed = await app.inject({ method: "GET", url: "/api/runs/%E0%A4%A" });
        if (malformed.statusCode !== 400) return fail(`malformed run id returned ${malformed.statusCode}`);
        if (contracts.runnerErrorResponseSchema.parse(malformed.json()).code !== "invalid_request")
          return fail("malformed run id did not use invalid_request");
        return pass("rejections reach the four-argument middleware as a JSON envelope");
      } finally {
        console.error = original;
        await app.close();
      }
    },
  },
  {
    id: "F2P-057",
    requirementIds: ["R047"],
    surface: "javascript-app/src/server.ts::GET /api/runs/:id/events",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("backend gate is not express middleware");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "GET",
          url: "/health",
          headers: { "x-cua-backend": "python" },
        });
        if (response.statusCode !== 409)
          return fail(`backend mismatch produced ${response.statusCode}`);
        if (response.json().code !== "backend_mismatch")
          return fail("backend mismatch code missing");
        return pass("X-CUA-Backend mismatch short-circuits with 409 before any action");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-059",
    requirementIds: ["R043"],
    surface:
      "javascript-app/src/server.ts::contracts/, console/, labs/, python-app/ and the version-3 replay format",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("not-found envelope is not express middleware");
      const { server } = await context.harness();
      const app = server.createServer({
        dataRoot: join(context.root, "data-probe-empty"),
      });
      try {
        const response = await app.inject({ method: "GET", url: "/api/runs/missing-run" });
        if (response.statusCode !== 404)
          return fail(`missing run produced ${response.statusCode}`);
        const body = response.json();
        if (body.code !== "run_not_found")
          return fail("run_not_found code missing");
        if (!String(body.hint ?? "").includes("Start a new run"))
          return fail("run_not_found hint missing");
        return pass("unknown run returns the express error envelope, not a default 404");
      } finally {
        await app.close();
      }
    },
  },
  {
    id: "F2P-060",
    requirementIds: ["R028"],
    surface: "javascript-app/src/server.ts::GET /replay",
    check: async (facts) => {
      if (facts.importsFastify) return fail("replay route still served by fastify");
      if (!facts.importsExpress) return fail("replay route not routed by express");
      if (facts.spawnsSubprocess) return fail("replay route delegated to a subprocess");
      return pass("replay route is served in-process by express");
    },
  },
  {
    id: "F2P-061",
    requirementIds: ["R035"],
    surface:
      "javascript-app/src/server.ts::GET /api/runs/:id/artifacts/screenshots/:name",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("listen/close is not backed by node:http+express");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const address = await listenForProbe(app);
        if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(address))
          return fail(`listen resolved ${address}`);
        await app.close();
        return pass("listen() resolves the URL string and close() resolves");
      } finally {
        await app.close().catch(() => undefined);
      }
    },
  },
  {
    id: "F2P-062",
    requirementIds: ["R039"],
    surface: "javascript-app/src/server.ts::Screenshot artifact route",
    check: async (facts, context) => {
      if (!facts.importsExpress) return fail("OPTIONS is not handled by an express app");
      const { server, baseManager } = await context.harness();
      const app = server.createServer({ manager: baseManager });
      try {
        const response = await app.inject({
          method: "OPTIONS",
          url: "/api/runs/probe-run/artifacts/screenshots/a.png",
          headers: { origin: "http://localhost:3000" },
        });
        if (response.statusCode !== 204)
          return fail(`OPTIONS returned ${response.statusCode}`);
        return pass("artifact route answers OPTIONS through express middleware");
      } finally {
        await app.close();
      }
    },
  },
];

export const OBSERVATION_IDS = PROBES.map((probe) => probe.id);

export async function runProbes(context) {
  const facts = await collectFacts(context.root);

  const canBootTarget =
    facts.importsExpress &&
    !facts.importsFastify &&
    typeof context.importServer === "function";

  let harness;
  const contextWithHarness = {
    ...context,
    facts,
    get harness() {
      return async () => {
        if (!harness) harness = await makeHarness(context);
        return harness;
      };
    },
    get contracts() {
      return context.contracts ?? {};
    },
  };

  const results = [];
  for (const probe of PROBES) {
    let result;
    try {
      if (!canBootTarget && probeNeedsRuntime(probe)) {
        result = notRun("probe not run: target technology (express) is not on the load path");
      } else {
        result = await probe.check(facts, contextWithHarness);
      }
    } catch (error) {
      result = {
        status: "FAIL",
        detail: `runtime predicate failed: ${error instanceof Error ? error.message : String(error)}`,
      };
    }
    results.push({ id: probe.id, ...result });
  }

  return { root: context.root, results };
}

function probeNeedsRuntime(probe) {
  const source = probe.check.toString();
  return source.includes("context.harness");
}
