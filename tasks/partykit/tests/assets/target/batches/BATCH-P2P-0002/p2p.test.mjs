import { beforeEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const mock = vi.hoisted(() => {
  const state = {
    calls: [],
    routed: [],
    result: null,
    router: null
  };
  const lobby = { id: "p2p-test-lobby" };

  function reset() {
    state.calls.length = 0;
    state.routed.length = 0;
    state.result = null;
    state.router = null;
  }

  async function snapshot(request) {
    const clone = request.clone();
    return {
      url: clone.url,
      method: clone.method,
      headers: Object.fromEntries(clone.headers),
      body: await clone.text()
    };
  }

  function hasPrefix(pathname, prefix) {
    // Kept inline because this factory is hoisted by Vitest. Cross-checked
    // against shared/batches/partyserver-prefix.mjs by verifier regressions.
    const prefixParts = (prefix || "parties").split("/");
    const parts = pathname.split("/").filter(Boolean);
    return parts.length >= prefixParts.length + 2 &&
      prefixParts.every((part, index) => parts[index] === part);
  }

  async function routePartykitRequest(request, env, options) {
    state.calls.push({ ...(await snapshot(request)), env, options });

    if (!hasPrefix(new URL(request.url).pathname, options?.prefix)) {
      return null;
    }

    const upgrade =
      (request.headers.get("upgrade") || "").toLowerCase() === "websocket";
    const hook = upgrade ? options?.onBeforeConnect : options?.onBeforeRequest;
    let forwarded = request;

    if (hook) {
      const replacement = await hook(forwarded, lobby);
      if (replacement instanceof Response) return replacement;
      if (replacement instanceof Request) forwarded = replacement;
    }

    state.routed.push({ ...(await snapshot(forwarded)), env, options });

    if (state.router) return state.router(forwarded, env, options);
    return state.result;
  }

  return { state, lobby, reset, routePartykitRequest };
});

vi.mock("cloudflare:workers", () => ({
  DurableObject: class DurableObject {},
  env: {}
}));
vi.mock("partyserver", () => ({
  routePartykitRequest: mock.routePartykitRequest,
  Server: class Server {}
}));

const repo =
  process.env.P2P_REPO ?? process.env.REPO_ROOT ?? "/workspace/repo";
function adapterEntry() {
  const packageDir = path.join(repo, "packages/hono-party");
  const manifest = JSON.parse(
    fs.readFileSync(path.join(packageDir, "package.json"), "utf8")
  );
  const rootExport = manifest.exports?.["."];
  const built = path.join(
    packageDir,
    (typeof rootExport === "string" ? rootExport : rootExport?.import) ??
      manifest.module ??
      manifest.main
  );
  return fs.existsSync(built) ? built : path.join(packageDir, "src/index.ts");
}
const { partyserverMiddleware } = await import(
  pathToFileURL(adapterEntry()).href
);

const repoRequire = createRequire(path.join(repo, "package.json"));
function resolvePackage(name) {
  const linked = path.join(repo, "node_modules", name);
  let dir;
  if (fs.existsSync(linked)) {
    dir = fs.realpathSync(linked);
  } else {
    dir = path.dirname(repoRequire.resolve(name));
  }
  let manifest;
  while (!manifest) {
    const candidate = path.join(dir, "package.json");
    if (fs.existsSync(candidate)) {
      const parsed = JSON.parse(fs.readFileSync(candidate, "utf8"));
      if (parsed.name === name) manifest = parsed;
    }
    if (!manifest) {
      const parent = path.dirname(dir);
      if (parent === dir) throw new Error(`package root not found for ${name}`);
      dir = parent;
    }
  }
  const rootExport = manifest.exports?.["."];
  const entry =
    (typeof rootExport === "string" ? rootExport : rootExport?.import) ??
    manifest.module ??
    manifest.main;
  const built = path.join(dir, entry);
  if (fs.existsSync(built)) return pathToFileURL(built).href;
  const source = path.join(dir, "src/index.ts");
  return pathToFileURL(fs.existsSync(source) ? source : built).href;
}
const { Elysia } = await import(resolvePackage("elysia"));

const url = "http://localhost/parties/chat/room";
const text = (body, status = 200) => () => new Response(body, { status });

function appWith(options, routes = {}) {
  const app = new Elysia();
  if (options !== undefined) app.use(partyserverMiddleware(options));
  for (const [pathname, handler] of Object.entries(routes)) {
    app.get(pathname, handler);
  }
  return app;
}

function request(app, target = url, init) {
  return app.handle(new Request(target, init));
}

beforeEach(() => mock.reset());

describe("BATCH-P2P-0002 target", () => {
  it("P2P-025 error without onError does not reach downstream", async () => {
    let downstream = false;
    const thrown = new Error("boom");
    const app = appWith(
      { options: { onBeforeRequest: () => { throw thrown; } } },
      {
        "/parties/chat/room": () => {
          downstream = true;
          return new Response("downstream");
        }
      }
    );
    let propagated = false;
    try {
      const response = await request(app);
      propagated = response instanceof Response &&
        (response.status >= 400 || (await response.text()).includes("boom"));
    } catch (error) {
      expect(error).toBe(thrown);
      propagated = true;
    }
    // Stored P2P projections represent the successful error outcome.
    expect({ downstream, responded: propagated, hasBody: propagated })
      .toEqual({ downstream: false, responded: true, hasBody: true });
  });

  it("P2P-026 onBeforeConnect replacement and void", async () => {
    mock.state.result = new Response("ok");
    const replacement = "http://localhost/parties/other/room";
    const configured = appWith({
      options: {
        onBeforeConnect: () =>
          new Request(replacement, { headers: { Upgrade: "websocket" } })
      }
    });
    await request(configured, url, { headers: { Upgrade: "websocket" } });

    const voided = appWith({
      options: { onBeforeConnect: () => undefined }
    });
    await request(voided, url, { headers: { Upgrade: "websocket" } });

    expect({
      replacement: mock.state.routed[0].url,
      original: mock.state.routed[1].url
    }).toEqual({ replacement, original: url });
  });

  it("P2P-027 upgrade null falls through", async () => {
    const app = appWith({}, { "/parties/chat/room": text("next") });
    const response = await request(app, url, {
      headers: { Upgrade: "websocket" }
    });
    expect({ status: response.status, body: await response.text() }).toEqual({
      status: 200,
      body: "next"
    });
  });

  it("P2P-028 package does not export ./agent", async () => {
    const pkg = JSON.parse(
      fs.readFileSync(
        path.join(repo, "packages/hono-party/package.json"),
        "utf8"
      )
    );
    expect({
      agent: Object.prototype.hasOwnProperty.call(pkg.exports, "./agent")
    }).toEqual({ agent: false });
  });

  it("P2P-029 non-upgrade selects onBeforeRequest only", async () => {
    const hits = { request: 0, connect: 0 };
    mock.state.result = new Response("routed");
    const app = appWith({
      options: {
        onBeforeRequest: () => {
          hits.request++;
        },
        onBeforeConnect: () => {
          hits.connect++;
        }
      }
    });
    const response = await request(app);
    expect({
      status: response.status,
      body: await response.text(),
      ...hits
    }).toEqual({ status: 200, body: "routed", request: 1, connect: 0 });
  });

  it("P2P-031 upgrade selects onBeforeConnect only", async () => {
    const hits = { request: 0, connect: 0 };
    const app = appWith(
      {
        options: {
          onBeforeRequest: () => {
            hits.request++;
          },
          onBeforeConnect: () => {
            hits.connect++;
          }
        }
      },
      { "/parties/chat/room": text("next") }
    );
    const response = await request(app, url, {
      headers: { Upgrade: "websocket" }
    });
    expect({
      status: response.status,
      body: await response.text(),
      ...hits
    }).toEqual({ status: 200, body: "next", request: 0, connect: 1 });
  });

  it("P2P-032 onError receives the same error; absence propagates", async () => {
    const error = new Error("x");
    const seen = [];
    const handled = appWith(
      {
        options: {
          onBeforeRequest: () => {
            throw error;
          }
        },
        onError: (value) => seen.push(value)
      },
      { "/parties/chat/room": text("next") }
    );
    const handledResponse = await request(handled);

    let absentDownstream = false;
    const unhandledError = new Error("y");
    const unhandled = appWith(
      {
        options: {
          onBeforeRequest: () => {
            throw unhandledError;
          }
        }
      },
      {
        "/parties/chat/room": () => {
          absentDownstream = true;
          return new Response("downstream");
        }
      }
    );
    let errorSurfaced = false;
    try {
      const response = await request(unhandled);
      errorSurfaced = response.status >= 400 || (await response.text()).includes("y");
    } catch (error) {
      expect(error).toBe(unhandledError);
      errorSurfaced = true;
    }

    expect({
      called: seen.length,
      same: seen[0] === error,
      withStatus: handledResponse.status,
      withBody: await handledResponse.text(),
      absentDownstream,
      absentBody: errorSurfaced
    }).toEqual({
      called: 1,
      same: true,
      withStatus: 200,
      withBody: "next",
      absentDownstream: false,
      absentBody: true
    });
  });

  it("P2P-033 root export is callable and registerable", async () => {
    const plugin = partyserverMiddleware();
    const app = new Elysia().use(plugin).get("/health", text("ok"));
    const response = await request(app, "http://localhost/health");
    expect({
      callable: typeof partyserverMiddleware,
      registered: response instanceof Response,
      status: response.status,
      body: await response.text()
    }).toEqual({
      callable: "function",
      registered: true,
      status: 200,
      body: "ok"
    });
  });

  it("P2P-034 forwarded request keeps url, method, headers, body", async () => {
    mock.state.result = new Response("ok");
    const response = await request(
      appWith({}),
      "https://example.test/parties/chat/room?x=1",
      {
        method: "POST",
        headers: { "x-token": "t" },
        body: "payload"
      }
    );
    expect(response.status).toBe(200);
    const call = mock.state.calls[0];
    expect({
      url: call.url,
      method: call.method,
      token: call.headers["x-token"],
      body: call.body
    }).toEqual({
      url: "https://example.test/parties/chat/room?x=1",
      method: "POST",
      token: "t",
      body: "payload"
    });
  });

  it("P2P-035 prefix limits interception", async () => {
    const hits = [];
    mock.state.result = new Response("routed");
    const app = appWith(
      {
        options: {
          prefix: "party",
          onBeforeRequest: () => hits.push("hook")
        }
      },
      {
        "/other": text("other"),
        "/party/chat/room": text("downstream")
      }
    );
    const outside = await request(app, "http://localhost/other");
    const outsideHits = hits.length;
    const outsideRouted = mock.state.routed.length;
    const inside = await request(app, "http://localhost/party/chat/room");
    expect({
      outsideBody: await outside.text(),
      outsideHits,
      outsideRouted,
      insideBody: await inside.text(),
      insideHits: hits.length,
      insideRouted: mock.state.routed.length
    }).toEqual({
      outsideBody: "other",
      outsideHits: 0,
      outsideRouted: 0,
      insideBody: "routed",
      insideHits: 1,
      insideRouted: 1
    });
  });

  it("P2P-036 forwards options to routePartykitRequest", async () => {
    mock.state.result = new Response("ok");
    const options = {
      prefix: "party",
      cors: { origin: "*" },
      jurisdiction: "eu",
      locationHint: "weur",
      props: { room: "x" },
      routingRetry: 3
    };
    const response = await request(
      appWith({ options }),
      "http://localhost/party/chat/room"
    );
    expect(response.status).toBe(200);
    expect(mock.state.calls[0].options).toEqual(options);
  });

  it("P2P-037 accepts and forwards all documented options", async () => {
    mock.state.result = new Response("ok");
    const options = {
      prefix: "party",
      cors: { origin: "*" },
      jurisdiction: "eu",
      locationHint: "weur",
      props: { room: "x" },
      routingRetry: 3
    };
    const response = await request(
      appWith({ options }),
      "http://localhost/party/chat/room"
    );
    expect(response.status).toBe(200);
    const received = mock.state.calls[0].options;
    expect({
      prefix: received.prefix,
      cors: received.cors,
      jurisdiction: received.jurisdiction,
      locationHint: received.locationHint,
      props: received.props,
      routingRetry: received.routingRetry
    }).toEqual(options);
  });

  it("P2P-038 package exports the root entry", async () => {
    const pkg = JSON.parse(
      fs.readFileSync(
        path.join(repo, "packages/hono-party/package.json"),
        "utf8"
      )
    );
    expect({
      root: Object.prototype.hasOwnProperty.call(pkg.exports, ".")
    }).toEqual({ root: true });
  });

  it("P2P-039 package identity and named root export", async () => {
    const pkg = JSON.parse(
      fs.readFileSync(
        path.join(repo, "packages/hono-party/package.json"),
        "utf8"
      )
    );
    expect({ name: pkg.name, named: typeof partyserverMiddleware }).toEqual({
      name: "hono-party",
      named: "function"
    });
  });

  it("P2P-040 package does not export ./client", async () => {
    const pkg = JSON.parse(
      fs.readFileSync(
        path.join(repo, "packages/hono-party/package.json"),
        "utf8"
      )
    );
    expect({
      client: Object.prototype.hasOwnProperty.call(pkg.exports, "./client")
    }).toEqual({ client: false });
  });
  it("P2P-030 encodes non-ASCII props as an ASCII-safe header value", async () => {
    const { routePartykitRequest } = await vi.importActual(
      resolvePackage("partyserver")
    );
    const props = { secret: "Usuário 日本語 🎉" };
    const expectedHeader = "eyJzZWNyZXQiOiJVc3XDoXJpbyDml6XmnKzoqp4g8J+OiSJ9";
    const fakeEnv = {
      PropsServer: {
        idFromName: (name) => name,
        get: () => ({
          fetch: async (request) => {
            const rawPropsHeader = request.headers.get("x-partykit-props");
            const binary = atob(rawPropsHeader);
            const bytes = new Uint8Array(binary.length);
            for (let index = 0; index < binary.length; index++) {
              bytes[index] = binary.charCodeAt(index);
            }
            return Response.json({
              name: "room-unicode",
              props: JSON.parse(new TextDecoder().decode(bytes)),
              rawPropsHeader
            });
          }
        })
      }
    };
    const response = await routePartykitRequest(
      new Request(
        "http://example.com/unicode-props-parties/props-server/room-unicode"
      ),
      fakeEnv,
      { prefix: "unicode-props-parties", props }
    );
    const data = await response.json();
    const projection = {
      status: response.status,
      name: data.name,
      props: data.props,
      rawPropsHeader: data.rawPropsHeader
    };
    console.log(
      `P2P_PROJECTION ${JSON.stringify({
        observation_id: "P2P-030",
        value: projection
      })}`
    );
    expect(projection).toEqual({
      status: 200,
      name: "room-unicode",
      props,
      rawPropsHeader: expectedHeader
    });
    expect(data.rawPropsHeader).toMatch(/^[\x00-\x7f]*$/);
  });
});
