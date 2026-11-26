import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import test from "node:test";
import {
  buildAdapter,
  createHarness,
  expectAmbientBindingsWithUnrelatedContext,
  expectOriginalErrorPropagation,
  expectVerbatimPrefix
} from "../assets/shared/batches/BATCH-F2P-0003/harness.mjs";

function dependencyRoot() {
  for (const root of [process.env.VERIFIER_NODE_MODULES, "/opt/migration/dependencies/node_modules"]) {
    if (!root) continue;
    const require = createRequire(path.join(root, "noop.js"));
    try {
      require.resolve("esbuild");
      require.resolve("elysia");
      return root;
    } catch {}
  }
  throw new Error("esbuild and Elysia are required for the runtime regressions");
}

async function buildFixture(source, env) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "party-runtime-regression-"));
  try {
    const entry = path.join(directory, "packages/hono-party/src/index.ts");
    fs.mkdirSync(path.dirname(entry), { recursive: true });
    fs.writeFileSync(entry, source);
    globalThis.__CF_ENV__ = env;
    globalThis.__PS_CONTROL__ = {};
    return await buildAdapter(directory, [dependencyRoot()], "target");
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

const imports = `
import { env } from "cloudflare:workers";
import { routePartykitRequest } from "partyserver";
export { env, routePartykitRequest };
export async function read(specifier) { return import(specifier); }
export async function literal() { return import("cloudflare:workers"); }
export async function computed() {
  const name = ["cloudflare", "workers"].join(":");
  return import(name);
}
`;

test("static, literal, variable and computed imports share mocks and binding updates", async () => {
  const env = { JWT_SECRET: "initial" };
  const adapter = await buildFixture(imports, env);
  const modules = await Promise.all([
    adapter.literal(), adapter.read("cloudflare:workers"), adapter.computed()
  ]);
  assert.equal(adapter.env, env);
  for (const module of modules) assert.equal(module.env, adapter.env);
  env.JWT_SECRET = "updated";
  for (const module of modules) assert.equal(module.env.JWT_SECRET, "updated");
  const dynamicRouter = await adapter.read("partyserver");
  assert.equal(dynamicRouter.routePartykitRequest, adapter.routePartykitRequest);
  assert.equal("mock" in adapter.routePartykitRequest, false);
  await adapter.routePartykitRequest(new Request("http://localhost/parties/chat/room"), env);
  await dynamicRouter.routePartykitRequest(new Request("http://localhost/parties/chat/room"), env);
  assert.equal(globalThis.__PS_CALLS__.length, 2);
});

test("adapter caches are isolated and runtime resolution does not leak to other modules", async () => {
  const firstEnv = { JWT_SECRET: "first" };
  const first = await buildFixture(imports, firstEnv);
  const secondEnv = { JWT_SECRET: "second" };
  const second = await buildFixture(imports, secondEnv);
  assert.equal((await first.read("cloudflare:workers")).env, firstEnv);
  assert.equal((await second.read("cloudflare:workers")).env, secondEnv);
  assert.notEqual(first.routePartykitRequest, second.routePartykitRequest);
  assert.equal((await first.read("partyserver")).routePartykitRequest, first.routePartykitRequest);
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "party-unrelated-module-"));
  try {
    const entry = path.join(directory, "unrelated.mjs");
    fs.writeFileSync(entry, "export async function read(specifier) { return import(specifier); }\n");
    const unrelated = await import(pathToFileURL(entry));
    await assert.rejects(unrelated.read("cloudflare:workers"), { code: "ERR_UNSUPPORTED_ESM_URL_SCHEME" });
    await assert.rejects(unrelated.read("partyserver"), { code: "ERR_MODULE_NOT_FOUND" });
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

function middlewareSource(bug) {
  const fallback = bug === "store" ? "c.store.counter ? c.store : env"
    : bug === "decorator" ? 'c.serviceName ? { serviceName: c.serviceName } : env' : "env";
  return `
import { env } from "cloudflare:workers";
import { routePartykitRequest } from "partyserver";
export function partyserverMiddleware(ctx) {
  return app => app.onRequest(async c => {
    c.env ??= ${fallback};
    const { onBeforeRequest, onBeforeConnect, ...rest } = ctx.options;
    return (await routePartykitRequest(c.request.clone(), c.env, {
      ...rest,
      onBeforeRequest: (request, lobby) => onBeforeRequest?.(request, lobby, c),
      onBeforeConnect: (request, lobby) => onBeforeConnect?.(request, lobby, c)
    })) ?? undefined;
  });
}
`;
}

test("ambient bindings survive unrelated Elysia state and decorators for HTTP and WebSocket", async () => {
  const adapter = await buildFixture(middlewareSource(), {});
  const harness = createHarness(adapter);
  await expectAmbientBindingsWithUnrelatedContext(harness);
});

test("the ambient binding check rejects both store and decorator shadowing", async () => {
  for (const bug of ["store", "decorator"]) {
    const adapter = await buildFixture(middlewareSource(bug), {});
    const harness = createHarness(adapter);
    await assert.rejects(expectAmbientBindingsWithUnrelatedContext(harness), /forwarded secret/);
  }
});

function prefixMiddlewareSource(mode) {
  return `
import { env } from "cloudflare:workers";
import { routePartykitRequest } from "partyserver";
export function partyserverMiddleware(ctx) {
  return app => app.onRequest(async c => {
    const options = ctx.options;
    const normalized = { ...options, prefix: options.prefix.split("/").filter(Boolean).join("/") };
    let response = await routePartykitRequest(c.request.clone(), env,
      ${mode === "normalize" ? "normalized" : "options"});
    ${mode === "retry" ? "if (response === null) response = await routePartykitRequest(c.request.clone(), env, normalized);" : ""}
    return response ?? undefined;
  });
}
`;
}

test("verbatim prefix check accepts unchanged delegation", async () => {
  const adapter = await buildFixture(prefixMiddlewareSource("valid"), {});
  await expectVerbatimPrefix(createHarness(adapter));
});

test("verbatim prefix check rejects normalization and retry after unmatched null", async () => {
  for (const mode of ["normalize", "retry"]) {
    const adapter = await buildFixture(prefixMiddlewareSource(mode), {});
    await assert.rejects(expectVerbatimPrefix(createHarness(adapter)), /downstream status/);
  }
});

function errorMiddlewareSource(swallow) {
  return `
import { env } from "cloudflare:workers";
import { routePartykitRequest } from "partyserver";
export function partyserverMiddleware(ctx) {
  return app => app.onRequest(async c => {
    try {
      return (await routePartykitRequest(c.request.clone(), env, ctx.options)) ?? undefined;
    } catch (error) {
      ${swallow ? 'return new Response("hidden error", { status: 500 });' : 'throw error;'}
    }
  });
}
`;
}

test("error propagation accepts original errors and rejects synthesized 500 responses", async () => {
  const valid = await buildFixture(errorMiddlewareSource(false), {});
  await expectOriginalErrorPropagation(createHarness(valid));
  const invalid = await buildFixture(errorMiddlewareSource(true), {});
  await assert.rejects(expectOriginalErrorPropagation(createHarness(invalid)),
    /Elysia receives original error/);
});
