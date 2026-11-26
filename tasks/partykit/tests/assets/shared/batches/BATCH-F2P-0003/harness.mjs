import fs from "node:fs";
import path from "node:path";
import cp from "node:child_process";
import { createRequire, register } from "node:module";
import { pathToFileURL } from "node:url";
import { satisfiesRange } from "../BATCH-F2P-0002/suite.mjs";
import { matchesPartyserverPath } from "../partyserver-prefix.mjs";

const NativeResponse = globalThis.Response;

class WorkersResponse extends NativeResponse {
  constructor(body, init) {
    if (init && (init.status === 101 || init.webSocket !== undefined)) {
      const { webSocket, status, ...rest } = init;
      super(body, { ...rest, status: status === 101 ? 200 : status });
      Object.defineProperty(this, "status", {
        value: status ?? 200,
        configurable: true
      });
      if (webSocket !== undefined) this.webSocket = webSocket;
      return;
    }
    super(body, init);
  }
}

globalThis.Response = WorkersResponse;

const MOCK_PARTYSERVER = `
const matchesPartyserverPath = ${matchesPartyserverPath.toString()};
const calls = [];
globalThis.__PS_CALLS__ = calls;

export function __reset() {
  calls.length = 0;
}

export async function routePartykitRequest(request, env, options) {
  const ctrl = globalThis.__PS_CONTROL__ || {};
  const entry = {
    request,
    env,
    options,
    path: new URL(request.url).pathname
  };
  calls.push(entry);

  if (!matchesPartyserverPath(entry.path, options?.prefix)) {
    entry.skippedByPrefix = true;
    return null;
  }

  const upgrade =
    (request.headers.get("upgrade") || "").toLowerCase() === "websocket";
  entry.upgrade = upgrade;
  entry.hookName = upgrade ? "onBeforeConnect" : "onBeforeRequest";

  const hook = upgrade ? options?.onBeforeConnect : options?.onBeforeRequest;
  let forwarded = request;

  if (hook) {
    entry.hookCalled = true;
    const result = await hook(request, ctrl.lobby || {});
    entry.hookResult = result;
    if (result instanceof Response) return result;
    if (result instanceof Request) forwarded = result;
  } else {
    entry.hookCalled = false;
  }

  entry.forwarded = forwarded;
  if (ctrl.respond) return await ctrl.respond(forwarded, env, options, entry);
  return ctrl.response === undefined ? null : ctrl.response;
}
`;

const MOCK_CLOUDFLARE = `export const env = globalThis.__CF_ENV__ || {};\n`;

function makeVirtualPlugin({ cloudflareURL, partyserverURL }) {
  return {
    name: "batch-f2p-0003-virtuals",
    setup(build) {
      build.onResolve({ filter: /^cloudflare:workers$/ }, () => ({
        path: cloudflareURL,
        external: true
      }));
      build.onResolve({ filter: /^partyserver$/ }, () => ({
        path: partyserverURL,
        external: true
      }));
    }
  };
}

function makeHonoFallbackPlugin() {
  return {
    name: "batch-f2p-0003-hono-fallback",
    setup(build) {
      build.onResolve({ filter: /^hono\/adapter$/ }, () => ({
        path: "hono-adapter",
        namespace: "batch-f2p-0003-hono"
      }));
      build.onResolve({ filter: /^hono\/factory$/ }, () => ({
        path: "hono-factory",
        namespace: "batch-f2p-0003-hono"
      }));
      build.onLoad(
        { filter: /.*/, namespace: "batch-f2p-0003-hono" },
        (args) => {
          if (args.path === "hono-adapter") {
            return {
              contents:
                "export function env(c) { return (c && c.env) || globalThis.__CF_ENV__ || {}; }\n",
              loader: "js"
            };
          }
          return {
            contents:
              "export function createMiddleware(fn) { const middleware = (c, next) => fn(c, next); middleware.__honoMiddleware = true; return middleware; }\n",
            loader: "js"
          };
        }
      );
    }
  };
}

function resolveDependencyRoots(repoRoot) {
  const candidates = [
    process.env.F2P_DEPENDENCIES_NODE_MODULES,
    process.env.F2P_EXTRA_NODE_MODULES,
    path.join(repoRoot, "node_modules"),
    "/assets/dependencies/node_modules",
    "/opt/migration/dependencies/node_modules"
  ].filter(Boolean);
  const seen = new Set();
  return candidates.filter((candidate) => {
    if (seen.has(candidate) || !fs.existsSync(candidate)) return false;
    seen.add(candidate);
    return true;
  });
}

function loadEsbuild(dependencyRoots) {
  for (const root of dependencyRoots) {
    try {
      return createRequire(path.join(root, "noop.js"))("esbuild");
    } catch {}
  }
  throw new Error("esbuild is not available in the dependency roots");
}

export async function buildAdapter(repoRoot, dependencyRoots, side) {
  const esbuild = loadEsbuild(dependencyRoots);
  const entry = path.join(
    repoRoot,
    "packages",
    "hono-party",
    "src",
    "index.ts"
  );
  // Both static imports and computed runtime imports must resolve to the
  // same modules. Unique files also isolate caches between adapter builds.
  const directory = fs.mkdtempSync(path.join("/tmp", "batch-f2p-0003-"));
  const bundlePath = path.join(directory, `${side}-adapter.mjs`);
  const bundleURL = pathToFileURL(bundlePath).href;
  const cloudflarePath = path.join(directory, "cloudflare-workers.mjs");
  const partyserverPath = path.join(directory, "partyserver.mjs");
  fs.writeFileSync(cloudflarePath, MOCK_CLOUDFLARE);
  fs.writeFileSync(partyserverPath, MOCK_PARTYSERVER);
  const runtimeModules = {
    bundleURL,
    cloudflareURL: pathToFileURL(cloudflarePath).href,
    partyserverURL: pathToFileURL(partyserverPath).href
  };
  const virtualPlugin = makeVirtualPlugin(runtimeModules);
  const options = {
    stdin: {
      contents: `export * from ${JSON.stringify(entry)};\nexport { Elysia } from "elysia";\nexport { version as __elysiaVersion } from "elysia/package.json";\n`,
      resolveDir: repoRoot,
      sourcefile: "batch-f2p-0003-entry.ts",
      loader: "ts"
    },
    bundle: true,
    format: "esm",
    platform: "node",
    target: "node22",
    write: false,
    plugins: [virtualPlugin],
    nodePaths: dependencyRoots,
    logLevel: "silent"
  };

  let result;
  try {
    result = await esbuild.build(options);
  } catch (error) {
    if (side === "source" && /Could not resolve "hono/.test(String(error))) {
      options.plugins = [virtualPlugin, makeHonoFallbackPlugin()];
      result = await esbuild.build(options);
    } else {
      throw error;
    }
  }

  fs.writeFileSync(bundlePath, result.outputFiles[0].text);
  const loaderURL = new URL("./runtime-loader.mjs", import.meta.url);
  loaderURL.searchParams.set("bundle", bundleURL);
  register(loaderURL, { data: runtimeModules });
  return import(bundleURL);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function assertEqual(actual, expected, message) {
  const left = JSON.stringify(actual);
  const right = JSON.stringify(expected);
  if (left !== right) {
    throw new Error(`${message}: expected ${right}, got ${left}`);
  }
}

function installInstrumentation() {
  const spawnCalls = [];
  for (const name of [
    "spawn",
    "exec",
    "execFile",
    "spawnSync",
    "execSync",
    "execFileSync",
    "fork"
  ]) {
    if (typeof cp[name] === "function") {
      cp[name] = function (...args) {
        spawnCalls.push({ name, args });
        throw new Error(`subprocess blocked: ${name}`);
      };
    }
  }
  globalThis.__SPAWN_CALLS__ = spawnCalls;

  const fetchCalls = [];
  const nativeFetch = globalThis.fetch;
  globalThis.fetch = function (input, ...rest) {
    const url =
      typeof input === "string"
        ? input
        : input && input.url
          ? input.url
          : String(input);
    fetchCalls.push(url);
    if (/hono/i.test(url)) throw new Error("hono rpc blocked");
    return nativeFetch.call(this, input, ...rest);
  };
  globalThis.__FETCH_CALLS__ = fetchCalls;
}

export function createHarness(adapter) {
  const envObject = globalThis.__CF_ENV__ || {};
  globalThis.__CF_ENV__ = envObject;

  return {
    Elysia: adapter.Elysia,
    partyserverMiddleware: adapter.partyserverMiddleware,
    elysiaVersion: adapter.__elysiaVersion,
    env() {
      return envObject;
    },
    clearEnv() {
      for (const key of Object.keys(envObject)) delete envObject[key];
    },
    setControl(control) {
      globalThis.__PS_CONTROL__ = control || {};
      if (globalThis.__PS_CALLS__) globalThis.__PS_CALLS__.length = 0;
    },
    calls() {
      return globalThis.__PS_CALLS__ || [];
    },
    reset() {
      this.clearEnv();
      this.setControl({});
      if (globalThis.__SPAWN_CALLS__) globalThis.__SPAWN_CALLS__.length = 0;
      if (globalThis.__FETCH_CALLS__) globalThis.__FETCH_CALLS__.length = 0;
    }
  };
}

function setControl(h, control) {
  h.setControl(control);
}

async function expectUseAccepted(h) {
  assertEqual(h.elysiaVersion, "1.3.0", "R005 Elysia version");
  setControl(h, { response: null });
  const app = new h.Elysia()
    .use(h.partyserverMiddleware())
    .get("/health", () => "ok")
    .get("/parties/chat/room", () => "downstream");
  const response = await app.handle(new Request("http://localhost/health"));
  assertEqual(response.status, 200, "R005 status");
  assertEqual(await response.text(), "ok", "R005 body");
  setControl(h, { respond: () => new Response("registered", { status: 202 }) });
  const routed = await app.handle(new Request("http://localhost/parties/chat/room"));
  assertEqual(routed.status, 202, "R005 registered status");
  assertEqual(await routed.text(), "registered", "R005 registered body");
  assertEqual(h.calls().length, 1, "R005 matched delegation");
}

async function expectContext(h) {
  setControl(h, { response: null });
  h.env().JWT_SECRET = "s3cr3t";
  const seen = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeRequest: (request, lobby, context) => {
            seen.push({
              secret: context.env.JWT_SECRET,
              url: context.request && context.request.url,
              requestUrl: request.url
            });
          }
        }
      })
    )
    .get("/parties/chat/room", () => "downstream");
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );
  assertEqual(response.status, 200, "R012 status");
  assertEqual(seen.length, 1, "R012 hook calls");
  assertEqual(seen[0].secret, "s3cr3t", "R012 env binding");
  assertEqual(
    seen[0].url,
    "http://localhost/parties/chat/room",
    "R012 context request"
  );
  assertEqual(
    seen[0].requestUrl,
    "http://localhost/parties/chat/room",
    "R012 request argument"
  );
}

async function expectUpgrade401(h) {
  setControl(h, { response: null });
  const hits = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeConnect: () =>
            new Response("Unauthorized", { status: 401 })
        }
      })
    )
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "downstream";
    });
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    })
  );
  assertEqual(response.status, 401, "R001 status");
  assertEqual(await response.text(), "Unauthorized", "R001 body");
  assertEqual(hits.length, 0, "R001 downstream not called");
  assertEqual(h.calls().length, 1, "R001 routePartykitRequest count");
}

async function expectErrorFlow(h) {
  setControl(h, { response: null });
  const seen = [];
  const hits = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeRequest: () => {
            throw new Error("boom");
          }
        },
        onError: (error) => seen.push(error.message)
      })
    )
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "downstream";
    });
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );
  assertEqual(response.status, 200, "R002 status");
  assertEqual(await response.text(), "downstream", "R002 body");
  assertEqual(seen, ["boom"], "R002 onError");
  assertEqual(hits, ["route"], "R002 downstream");
}

async function expectRegistrationOrder(h) {
  setControl(h, { response: null });
  const hits = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeRequest: () => {
            hits.push("hook");
          }
        }
      })
    )
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "routed";
    });
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );
  assertEqual(hits, ["hook", "route"], "R006 order");
  assertEqual(response.status, 200, "R006 status");
  assertEqual(await response.text(), "routed", "R006 body");
}

async function expectUpgrade404(h, options = {}) {
  const routePath = options.path || "/parties/chat/room";
  const prefix = options.prefix || null;
  setControl(h, {
    response: new Response("not found", { status: 404 })
  });
  const hits = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware(prefix ? { options: { prefix } } : undefined)
    )
    .get(routePath, () => {
      hits.push("route");
      return "downstream";
    });
  const response = await app.handle(
    new Request(`http://localhost${routePath}`, {
      headers: { Upgrade: "websocket" }
    })
  );
  assertEqual(response.status, 404, "R022 status");
  assertEqual(await response.text(), "not found", "R022 body");
  assertEqual(hits.length, 0, "R022 downstream not called");
  assertEqual(h.calls().length, 1, "R022 routePartykitRequest count");
}

async function expectUpgrade403(h) {
  setControl(h, { response: null });
  const hits = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeConnect: () =>
            new Response("forbidden", {
              status: 403,
              headers: { "x-rejected": "yes" }
            })
        }
      })
    )
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "downstream";
    });
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    })
  );
  assertEqual(response.status, 403, "R025 status");
  assertEqual(response.headers.get("x-rejected"), "yes", "R025 header");
  assertEqual(await response.text(), "forbidden", "R025 body");
  assertEqual(hits.length, 0, "R025 downstream not called");
}

async function atomicPassthrough201(h) {
  setControl(h, {
    respond: () =>
      new Response("hello", {
        status: 201,
        headers: { "x-custom": "v" }
      })
  });
  const hits = [];
  const app = new h.Elysia()
    .use(h.partyserverMiddleware())
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "downstream";
    });
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );
  assertEqual(response.status, 201, "passthrough status");
  assertEqual(response.headers.get("x-custom"), "v", "passthrough header");
  assertEqual(await response.text(), "hello", "passthrough body");
  assertEqual(hits.length, 0, "passthrough downstream");
}

async function atomicPrefixPassThrough(h, prefix = "party") {
  setControl(h, { response: null });
  const routeHits = [];
  const hookHits = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          prefix,
          onBeforeRequest: () => {
            hookHits.push("onBeforeRequest");
          },
          onBeforeConnect: () => {
            hookHits.push("onBeforeConnect");
          }
        }
      })
    )
    .get("/other", () => {
      routeHits.push("other");
      return "other-ok";
    })
    .get(`/${prefix}/chat/room`, () => {
      routeHits.push("party");
      return "party-ok";
    });

  let response = await app.handle(new Request("http://localhost/other"));
  assertEqual(response.status, 200, "prefix other status");
  assertEqual(await response.text(), "other-ok", "prefix other body");
  assertEqual(hookHits.length, 0, "prefix other hook count");

  response = await app.handle(
    new Request(`http://localhost/${prefix}/chat/room`)
  );
  assertEqual(response.status, 200, "prefix matched status");
  assertEqual(await response.text(), "party-ok", "prefix matched body");
  assertEqual(hookHits, ["onBeforeRequest"], "prefix matched hook");
}

async function atomicNonUpgradeHookSelection(h) {
  setControl(h, { response: null });
  const hooks = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeConnect: () => {
            hooks.push("onBeforeConnect");
          },
          onBeforeRequest: () => {
            hooks.push("onBeforeRequest");
          }
        }
      })
    )
    .get("/parties/chat/room", () => "downstream");
  await app.handle(new Request("http://localhost/parties/chat/room"));
  assertEqual(hooks, ["onBeforeRequest"], "non-upgrade hook selection");
}

async function atomicUpgradeHookSelection(h) {
  setControl(h, { response: null });
  const hooks = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeConnect: () => {
            hooks.push("onBeforeConnect");
          },
          onBeforeRequest: () => {
            hooks.push("onBeforeRequest");
          }
        }
      })
    )
    .get("/parties/chat/room", () => "downstream");
  await app.handle(
    new Request("http://localhost/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    })
  );
  assertEqual(hooks, ["onBeforeConnect"], "upgrade hook selection");
}

async function atomicNonUpgradeNullFallthrough(h) {
  setControl(h, { response: null });
  const app = new h.Elysia()
    .use(h.partyserverMiddleware())
    .get("/parties/chat/room", () => "next");
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );
  assertEqual(response.status, 200, "non-upgrade null status");
  assertEqual(await response.text(), "next", "non-upgrade null body");
}

async function atomicUpgradeNullFallthrough(h) {
  setControl(h, { response: null });
  const app = new h.Elysia()
    .use(h.partyserverMiddleware())
    .get("/parties/chat/room", () => "next");
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    })
  );
  assertEqual(response.status, 200, "upgrade null status");
  assertEqual(await response.text(), "next", "upgrade null body");
}

async function atomicUpgrade101(h) {
  const webSocket = { fake: true };
  setControl(h, {
    response: new Response(null, { webSocket })
  });
  const hits = [];
  const app = new h.Elysia()
    .use(h.partyserverMiddleware())
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "downstream";
    });
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    })
  );
  assertEqual(response.status, 101, "upgrade 101 status");
  assertEqual(response.webSocket, webSocket, "upgrade 101 handle");
  assertEqual(hits.length, 0, "upgrade 101 downstream");
  assertEqual(h.calls().length, 1, "upgrade 101 route count");
}

async function atomicForwardFidelity(h) {
  let forwarded;
  setControl(h, {
    respond: (request) => {
      forwarded = request;
      return new Response("ok", { status: 200 });
    }
  });
  const app = new h.Elysia()
    .use(h.partyserverMiddleware())
    .post("/parties/chat/room", () => "downstream");
  const response = await app.handle(
    new Request("https://example.test/parties/chat/room?x=1", {
      method: "POST",
      headers: { "x-token": "t" },
      body: "payload"
    })
  );
  assertEqual(response.status, 200, "forward fidelity status");
  assert(forwarded, "forward fidelity forwarded");
  assertEqual(
    forwarded.url,
    "https://example.test/parties/chat/room?x=1",
    "forward fidelity url"
  );
  assertEqual(forwarded.method, "POST", "forward fidelity method");
  assertEqual(
    forwarded.headers.get("x-token"),
    "t",
    "forward fidelity header"
  );
  assertEqual(await forwarded.text(), "payload", "forward fidelity body");
}

async function atomicForwardBindingsOptions(h) {
  let capturedEnv;
  let capturedOptions;
  setControl(h, {
    respond: (request, env, options) => {
      capturedEnv = env;
      capturedOptions = options;
      return new Response("ok", { status: 200 });
    }
  });
  h.env().JWT_SECRET = "s3cr3t";
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          prefix: "parties",
          cors: { origin: "*" },
          jurisdiction: "eu",
          locationHint: "weur",
          props: { room: "x" },
          routingRetry: 3
        }
      })
    )
    .post("/parties/chat/room", () => "downstream");
  await app.handle(
    new Request("http://localhost/parties/chat/room", {
      method: "POST",
      body: "payload"
    })
  );
  assert(capturedEnv, "forward bindings captured");
  assertEqual(capturedEnv.JWT_SECRET, "s3cr3t", "forward bindings secret");
  assert(capturedOptions, "forward options captured");
  assertEqual(capturedOptions.prefix, "parties", "forward options prefix");
  assertEqual(capturedOptions.cors, { origin: "*" }, "forward options cors");
  assertEqual(capturedOptions.jurisdiction, "eu", "forward options jurisdiction");
  assertEqual(capturedOptions.locationHint, "weur", "forward options hint");
  assertEqual(capturedOptions.props, { room: "x" }, "forward options props");
  assertEqual(capturedOptions.routingRetry, 3, "forward options retry");
}

export async function expectVerbatimPrefix(h) {
  // PartyServer splits the configured prefix verbatim. Leading, trailing,
  // or repeated separators must not become a different, matching prefix.
  // This batch uses a plain routing function, so mock-specific branches in
  // an adapter exercise the same path they would with the real router.
  for (const prefix of ["/party", "party/", "party//nested"]) {
    for (const upgrade of [false, true]) {
      const pathname = prefix.includes("nested")
        ? "/party/nested/chat/room" : "/party/chat/room";
      const label = `verbatim prefix ${prefix}, ${upgrade ? "WS" : "HTTP"}`;
      setControl(h, { response: new Response("intercepted", { status: 202 }) });
      const hooks = [];
      let downstream = 0;
      const app = new h.Elysia().use(h.partyserverMiddleware({
        options: {
          prefix,
          onBeforeRequest: () => { hooks.push("request"); },
          onBeforeConnect: () => { hooks.push("connect"); }
        }
      })).get(pathname, () => {
        downstream += 1;
        return "next";
      });
      const response = await app.handle(new Request(`http://localhost${pathname}`, {
        headers: upgrade ? { Upgrade: "WebSocket" } : {}
      }));
      assertEqual(response.status, 200, `${label}: downstream status`);
      assertEqual(await response.text(), "next", `${label}: unmatched null falls through`);
      assertEqual(downstream, 1, `${label}: downstream called once`);
      assertEqual(hooks, [], `${label}: no matching PartyServer hook`);
      // Early prefix filtering is valid. Every actual delegation must still
      // carry the original option and keep the router's unmatched result.
      for (const call of h.calls()) {
        assertEqual(call.options?.prefix, prefix, `${label}: forwarded option`);
        assertEqual(call.skippedByPrefix, true, `${label}: unmatched route`);
      }
    }
  }
}

export async function expectAmbientBindingsWithUnrelatedContext(h) {
  for (const decoration of ["store", "decorator"]) {
    for (const upgrade of [false, true]) {
      const label = `ambient env with unrelated ${decoration}, ${upgrade ? "WS" : "HTTP"}`;
      const secret = `worker-secret-${decoration}-${upgrade}`;
      const namespace = {
        idFromName: (name) => name,
        get: () => ({ fetch: async () => new Response("DO") })
      };
      h.clearEnv();
      h.env().JWT_SECRET = secret;
      h.env().Chat = namespace;
      setControl(h, { response: new Response("routed", { status: 207 }) });
      const hooks = [];
      const hasNamespace = (binding) =>
        typeof binding?.idFromName === "function" && typeof binding?.get === "function";
      const capture = (name) => (_request, _lobby, context) => {
        hooks.push({
          name,
          secret: context.env?.JWT_SECRET,
          namespaceAvailable: hasNamespace(context.env?.Chat)
        });
      };
      let downstreamCalls = 0;
      const app = new h.Elysia();
      if (decoration === "store") app.state("counter", 1);
      else app.decorate("serviceName", "example");
      app.use(h.partyserverMiddleware({
        options: {
          onBeforeConnect: capture("connect"),
          onBeforeRequest: capture("request")
        }
      })).get("/parties/chat/room", () => {
        downstreamCalls += 1;
        return "downstream";
      });
      const response = await app.handle(new Request("http://localhost/parties/chat/room", {
        headers: upgrade ? { Upgrade: "WebSocket" } : {}
      }));
      assertEqual(response.status, 207, `${label}: status`);
      assertEqual(await response.text(), "routed", `${label}: body`);
      assertEqual(downstreamCalls, 0, `${label}: no downstream`);
      assertEqual(h.calls().length, 1, `${label}: route count`);
      assertEqual(h.calls()[0].env?.JWT_SECRET, secret, `${label}: forwarded secret`);
      assert(hasNamespace(h.calls()[0].env?.Chat), `${label}: forwarded namespace`);
      assertEqual(hooks, [{
        name: upgrade ? "connect" : "request",
        secret,
        namespaceAvailable: true
      }], `${label}: callback bindings and hook selection`);
    }
  }
}

async function atomicRouteCalledOnce(h) {
  setControl(h, {
    respond: () => new Response("routed", { status: 200 })
  });
  h.env().JWT_SECRET = "s3cr3t";
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          prefix: "parties",
          cors: { origin: "*" },
          jurisdiction: "eu",
          locationHint: "weur",
          props: { room: "x" },
          routingRetry: 3
        }
      })
    )
    .get("/parties/chat/room", () => "downstream");
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );
  assertEqual(response.status, 200, "route once status");
  assertEqual(await response.text(), "routed", "route once body");
  assertEqual(h.calls().length, 1, "route once count");
  const entry = h.calls()[0];
  assertEqual(
    entry.request.url,
    "http://localhost/parties/chat/room",
    "route once request"
  );
  assertEqual(entry.env.JWT_SECRET, "s3cr3t", "route once env");
  assertEqual(entry.options.prefix, "parties", "route once options");
}

async function atomicOnBeforeRequestTakeover(h) {
  setControl(h, { response: null });
  const hits = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeRequest: () =>
            new Response("blocked", { status: 401 })
        }
      })
    )
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "downstream";
    });
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );
  assertEqual(response.status, 401, "request takeover status");
  assertEqual(await response.text(), "blocked", "request takeover body");
  assertEqual(hits.length, 0, "request takeover downstream");
}

async function atomicReplacementMutation(h) {
  let forwarded;
  setControl(h, {
    respond: (request) => {
      forwarded = request;
      return new Response("ok", { status: 200 });
    }
  });
  let app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeRequest: (request) => {
            request.headers.set("x-mutated", "yes");
          }
        }
      })
    )
    .get("/parties/chat/room", () => "downstream");
  await app.handle(new Request("http://localhost/parties/chat/room"));
  assertEqual(
    forwarded.headers.get("x-mutated"),
    "yes",
    "mutation forwarding"
  );

  let replacement;
  setControl(h, {
    respond: (request) => {
      replacement = request;
      return new Response("ok", { status: 200 });
    }
  });
  app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeRequest: () =>
            new Request("http://localhost/parties/other/room", {
              headers: { "x-replaced": "yes" }
            })
        }
      })
    )
    .get("/parties/chat/room", () => "downstream");
  await app.handle(new Request("http://localhost/parties/chat/room"));
  assert(replacement, "replacement forwarded");
  assertEqual(
    replacement.url,
    "http://localhost/parties/other/room",
    "replacement url"
  );
  assertEqual(
    replacement.headers.get("x-replaced"),
    "yes",
    "replacement header"
  );
}

async function atomicErrorWithoutOnError(h) {
  setControl(h, { response: null });
  const hits = [];
  const app = new h.Elysia()
    .use(
      h.partyserverMiddleware({
        options: {
          onBeforeRequest: () => {
            throw new Error("boom");
          }
        }
      })
    )
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "downstream";
    });
  try {
    await app.handle(new Request("http://localhost/parties/chat/room"));
  } catch {}
  assertEqual(hits.length, 0, "error propagation downstream");
  await expectOriginalErrorPropagation(h);
}

export async function expectOriginalErrorPropagation(h) {
  // PartyServer must work without an application route too. A synthesized
  // 500 response hides the original error from both Elysia and the caller.
  for (const matched of [false, true]) {
    for (const upgrade of [false, true]) {
      for (const source of ["hook", "router"]) {
        const label = `original error: ${matched ? "matched" : "unmatched"}, ${upgrade ? "WS" : "HTTP"}, ${source}`;
        const thrown = new Error(label);
        setControl(h, source === "router"
          ? { respond: () => { throw thrown; } }
          : { response: null });
        const seen = [];
        let downstreamCalls = 0;
        const hook = upgrade ? "onBeforeConnect" : "onBeforeRequest";
        const app = new h.Elysia()
          .use(h.partyserverMiddleware({ options: {
            [hook]: () => { if (source === "hook") throw thrown; }
          } }))
          .onError(({ error }) => {
            if (error === thrown) {
              seen.push(error);
              return new Response("application handled original error", { status: 502 });
            }
          });
        if (matched) app.get("/parties/chat/room", () => {
          downstreamCalls += 1;
          return "downstream";
        });
        let response;
        let rejected;
        try {
          response = await app.handle(new Request("http://localhost/parties/chat/room", {
            headers: upgrade ? { Upgrade: "WebSocket" } : {}
          }));
        } catch (error) {
          rejected = error;
        }
        assertEqual(downstreamCalls, 0, `${label}: no route handler`);
        if (rejected !== undefined) {
          assert(rejected === thrown, `${label}: caller receives original error`);
        } else {
          assert(seen.length === 1 && seen[0] === thrown,
            `${label}: Elysia receives original error`);
          assert(response instanceof Response,
            `${label}: Elysia completes its application error handling`);
        }
      }
    }
  }
}

async function atomicErrorPreservesRequestBody(h) {
  const thrown = new Error("forwarding failed after reading body");
  setControl(h, {
    respond: async (request) => {
      // PartyServer transfers the body to its own Request before invoking
      // hooks. A later routing or hook failure must leave the caller's body
      // available to Elysia when ctx.onError permits fallthrough.
      const ownedRequest = new Request(request);
      assertEqual(await ownedRequest.text(), "payload", "forwarded error body");
      throw thrown;
    }
  });
  const errors = [];
  let downstreamCalls = 0;
  const app = new h.Elysia()
    .use(h.partyserverMiddleware({ onError: (error) => errors.push(error) }))
    .post("/parties/chat/room", ({ body }) => {
      downstreamCalls += 1;
      return new Response(`next:${body}`, {
        status: 207,
        headers: { "x-downstream": "yes" }
      });
    });
  const response = await app.handle(new Request("http://localhost/parties/chat/room", {
    method: "POST",
    headers: { "content-type": "text/plain" },
    body: "payload"
  }));
  assertEqual(errors.length, 1, "body error callback count");
  assert(errors[0] === thrown, "body error callback identity");
  assertEqual(downstreamCalls, 1, "body error downstream count");
  assertEqual(response.status, 207, "body error downstream status");
  assertEqual(response.headers.get("x-downstream"), "yes", "body error downstream header");
  assertEqual(await response.text(), "next:payload", "body error downstream body");
}

async function expectDependencyPolicy(h, repoRoot) {
  const packagePath = path.join(
    repoRoot,
    "packages",
    "hono-party",
    "package.json"
  );
  const sourcePath = path.join(
    repoRoot,
    "packages",
    "hono-party",
    "src",
    "index.ts"
  );
  const pkg = JSON.parse(fs.readFileSync(packagePath, "utf8"));
  const source = fs.readFileSync(sourcePath, "utf8");

  assertEqual(pkg.name, "hono-party", "R033 package name");
  assert(
    !Object.prototype.hasOwnProperty.call(pkg.peerDependencies || {}, "hono"),
    "R033 peer hono removed"
  );
  assert(
    !Object.prototype.hasOwnProperty.call(pkg.devDependencies || {}, "hono"),
    "R033 dev hono removed"
  );
  assert(
    satisfiesRange("1.3.0", pkg.peerDependencies?.elysia),
    "R033 peer elysia 1.3.0"
  );
  assert(
    satisfiesRange("1.3.0", pkg.devDependencies?.elysia),
    "R033 dev elysia 1.3.0"
  );
  assertEqual(
    pkg.peerDependencies?.partyserver,
    ">=0.2.0 <1.0.0",
    "R033 partyserver peer range"
  );

  const specifiers = [...source.matchAll(/\bfrom\s+["']([^"']+)["']/g)].map(
    (match) => match[1]
  );
  assert(
    !specifiers.some(
      (specifier) => specifier === "hono" || specifier.startsWith("hono/")
    ),
    "R033 no hono imports"
  );
  assert(
    source.includes("routePartykitRequest"),
    "R033 direct routePartykitRequest"
  );
  assert(
    !/import\s*\(\s*["']hono/.test(source),
    "R033 no dynamic hono import"
  );

  setControl(h, {
    respond: () => new Response("routed", { status: 200 })
  });
  h.env().JWT_SECRET = "s3cr3t";
  const app = new h.Elysia()
    .use(h.partyserverMiddleware())
    .get("/parties/chat/room", () => "downstream");
  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );
  assertEqual(response.status, 200, "R033 runtime status");
  assertEqual(await response.text(), "routed", "R033 runtime body");
  assertEqual(h.calls().length, 1, "R033 direct delegation");
  assertEqual(h.calls()[0].env.JWT_SECRET, "s3cr3t", "R033 runtime env");
  assertEqual(globalThis.__SPAWN_CALLS__.length, 0, "R033 no subprocess");
  assertEqual(globalThis.__FETCH_CALLS__.length, 0, "R033 no rpc/fetch");
}

export const CASES = [
  {
    id: "F2P-059",
    run: (h, repoRoot) => expectDependencyPolicy(h, repoRoot)
  },
  {
    id: "F2P-049",
    run: async (h) => {
      await atomicPassthrough201(h);
      await expectUseAccepted(h);
    }
  },
  {
    id: "F2P-050",
    run: async (h) => {
      await atomicPrefixPassThrough(h);
      await expectContext(h);
    }
  },
  {
    id: "F2P-051",
    run: async (h) => {
      await atomicErrorWithoutOnError(h);
      await expectContext(h);
    }
  },
  {
    id: "F2P-052",
    run: async (h) => {
      await atomicPrefixPassThrough(h);
      await expectUseAccepted(h);
    }
  },
  {
    id: "F2P-053",
    run: async (h) => {
      await atomicPrefixPassThrough(h);
      await expectErrorFlow(h);
    }
  },
  {
    id: "F2P-054",
    run: async (h) => {
      await atomicForwardFidelity(h);
      await expectContext(h);
    }
  },
  {
    id: "F2P-055",
    run: async (h) => {
      await atomicNonUpgradeNullFallthrough(h);
      await expectRegistrationOrder(h);
    }
  },
  {
    id: "F2P-056",
    run: async (h) => {
      await atomicNonUpgradeHookSelection(h);
      await expectUpgrade404(h);
    }
  },
  {
    id: "F2P-057",
    run: async (h) => {
      await atomicPrefixPassThrough(h);
      await expectUpgrade404(h, { prefix: "party", path: "/party/chat/room" });
    }
  },
  {
    id: "F2P-058",
    run: async (h) => {
      await atomicUpgradeNullFallthrough(h);
      await expectUpgrade404(h);
    }
  },
  {
    id: "F2P-060",
    run: async (h) => {
      await atomicUpgrade101(h);
      await expectUpgrade401(h);
    }
  },
  {
    id: "F2P-061",
    run: async (h) => {
      await atomicPrefixPassThrough(h);
      await expectUpgrade401(h);
    }
  },
  {
    id: "F2P-062",
    run: async (h) => {
      await atomicUpgradeHookSelection(h);
      await expectUseAccepted(h);
    }
  },
  {
    id: "F2P-063",
    run: async (h) => {
      await atomicForwardBindingsOptions(h);
      await expectVerbatimPrefix(h);
      await expectUpgrade404(h);
      await expectAmbientBindingsWithUnrelatedContext(h);
    }
  },
  {
    id: "F2P-064",
    run: async (h) => {
      await atomicErrorWithoutOnError(h);
      await expectUpgrade404(h);
    }
  },
  {
    id: "F2P-065",
    run: async (h) => {
      await atomicNonUpgradeHookSelection(h);
      await expectUpgrade403(h);
    }
  },
  {
    id: "F2P-066",
    run: async (h) => {
      await atomicRouteCalledOnce(h);
      await expectUpgrade401(h);
    }
  },
  {
    id: "F2P-067",
    run: async (h) => {
      await atomicOnBeforeRequestTakeover(h);
      await expectErrorFlow(h);
      await atomicErrorPreservesRequestBody(h);
    }
  },
  {
    id: "F2P-068",
    run: async (h) => {
      await atomicUpgrade101(h);
      await expectErrorFlow(h);
    }
  },
  {
    id: "F2P-069",
    run: async (h) => {
      await atomicReplacementMutation(h);
      await expectRegistrationOrder(h);
    }
  },
  {
    id: "F2P-070",
    run: async (h) => {
      await atomicNonUpgradeNullFallthrough(h);
      await expectUseAccepted(h);
    }
  },
  {
    id: "F2P-071",
    run: async (h) => {
      await atomicPrefixPassThrough(h);
      await expectRegistrationOrder(h);
    }
  },
  {
    id: "F2P-072",
    run: async (h) => {
      await atomicNonUpgradeNullFallthrough(h);
      await expectUpgrade401(h);
    }
  }
];

export async function run({ side, repoRoot, reportPath }) {
  const dependencyRoots = resolveDependencyRoots(repoRoot);
  if (!globalThis.__CF_ENV__ || typeof globalThis.__CF_ENV__ !== "object") {
    globalThis.__CF_ENV__ = {};
  }
  const adapter = await buildAdapter(repoRoot, dependencyRoots, side);
  installInstrumentation();
  const h = createHarness(adapter);
  const results = [];

  for (const testCase of CASES) {
    h.reset();
    try {
      await testCase.run(h, repoRoot);
      results.push({ id: testCase.id, status: "PASS" });
    } catch (error) {
      results.push({
        id: testCase.id,
        status: "FAIL",
        error: String((error && error.message) || error)
      });
    }
  }

  const summary = {
    side,
    total: results.length,
    passed: results.filter((result) => result.status === "PASS").length,
    failed: results.filter((result) => result.status === "FAIL").length,
    results
  };

  if (reportPath) {
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, JSON.stringify(summary, null, 2));
  }

  console.log(JSON.stringify(summary, null, 2));
  return summary;
}

export function parseArgs(defaultRepoRoot) {
  const args = process.argv.slice(2);
  let repoRoot = process.env.F2P_REPO_ROOT || defaultRepoRoot;
  let reportPath = null;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--repo") {
      repoRoot = args[index + 1];
      index += 1;
    } else if (arg === "--report") {
      reportPath = args[index + 1];
      index += 1;
    } else if (!arg.startsWith("-") && !reportPath) {
      reportPath = arg;
    }
  }
  return { repoRoot, reportPath };
}
