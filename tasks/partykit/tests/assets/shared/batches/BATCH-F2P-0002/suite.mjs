import { createRequire } from "node:module";
import { matchesPartyserverPath } from "../partyserver-prefix.mjs";

const semver = createRequire("/opt/migration/dependencies/package.json")("semver");

export const OBSERVATIONS = [
  { id: "F2P-036", requirement_ids: ["R036"], checks: ["R036"] },
  { id: "F2P-046", requirement_ids: ["R037"], checks: ["R037"] },
  { id: "F2P-026", requirement_ids: ["R001"], checks: ["R029", "R001"] },
  { id: "F2P-031", requirement_ids: ["R012"], checks: ["R019", "R012"] },
  { id: "F2P-038", requirement_ids: ["R012"], checks: ["R026", "R012"] },
  { id: "F2P-025", requirement_ids: ["R006"], checks: ["R016", "R006"] },
  { id: "F2P-027", requirement_ids: ["R005"], checks: ["R024", "R005"] },
  { id: "F2P-028", requirement_ids: ["R022"], checks: ["R018", "R022"] },
  { id: "F2P-029", requirement_ids: ["R005"], checks: ["R030", "R005"] },
  { id: "F2P-030", requirement_ids: ["R025"], checks: ["R030", "R025"] },
  { id: "F2P-032", requirement_ids: ["R012"], checks: ["R003", "R012"] },
  { id: "F2P-033", requirement_ids: ["R006"], checks: ["R018", "R006"] },
  { id: "F2P-034", requirement_ids: ["R002"], checks: ["R018", "R002"] },
  { id: "F2P-035", requirement_ids: ["R012"], checks: ["R027", "R012"] },
  { id: "F2P-037", requirement_ids: ["R022"], checks: ["R014", "R022"] },
  { id: "F2P-039", requirement_ids: ["R025"], checks: ["R028", "R025"] },
  { id: "F2P-040", requirement_ids: ["R025"], checks: ["R029", "R025"] },
  { id: "F2P-041", requirement_ids: ["R002"], checks: ["R015", "R002"] },
  { id: "F2P-042", requirement_ids: ["R025"], checks: ["R018", "R025"] },
  { id: "F2P-043", requirement_ids: ["R025"], checks: ["R014", "R025"] },
  { id: "F2P-044", requirement_ids: ["R022"], checks: ["R003", "R022"] },
  { id: "F2P-045", requirement_ids: ["R001"], checks: ["R015", "R001"] },
  { id: "F2P-047", requirement_ids: ["R005"], checks: ["R017", "R005"] },
  { id: "F2P-048", requirement_ids: ["R022"], checks: ["R026", "R022"] }
];

function fail(message) {
  throw new Error(message);
}

function ok(value, message) {
  if (!value) fail(message);
}

function equal(actual, expected, message) {
  if (actual !== expected) {
    fail(`${message}: expected ${JSON.stringify(expected)}, received ${JSON.stringify(actual)}`);
  }
}

function deepEqual(actual, expected, message) {
  const left = JSON.stringify(actual);
  const right = JSON.stringify(expected);
  if (left !== right) fail(`${message}: expected ${right}, received ${left}`);
}

function bodyOf(response) {
  return response.text();
}

function partyRequest(path = "/parties/chat/room", init = {}) {
  return new Request(new URL(path, "http://localhost"), init);
}

function upgradeRequest(path = "/parties/chat/room", init = {}) {
  return partyRequest(path, { ...init, headers: { ...(init.headers ?? {}), Upgrade: "websocket" } });
}

export function makePartyserverRoute({ route, state }) {
  return async (request, env, options) => {
    state.calls.push({ request, env, options });
    const url = new URL(request.url);
    if (!matchesPartyserverPath(url.pathname, options?.prefix)) return null;
    const upgrade = (request.headers.get("upgrade") ?? "").toLowerCase() === "websocket";
    let forwarded = request;
    const hook = upgrade ? options?.onBeforeConnect : options?.onBeforeRequest;
    if (typeof hook === "function") {
      const result = await hook(forwarded, state.lobby);
      if (result instanceof Response) return result;
      if (result instanceof Request) forwarded = result;
    }
    return route ? route(forwarded, env, options, state) : null;
  };
}

export function satisfiesRange(version, range) {
  return typeof range === "string" &&
    semver.validRange(range) !== null &&
    semver.satisfies(version, range);
}

const CHECKS = {
  async R001(side) {
    const downstream = [];
    const scenario = side.createApp({
      ctx: { options: { onBeforeConnect: () => new Response("Unauthorized", { status: 401 }) } },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => { downstream.push("downstream"); return "downstream"; } }]
    });
    const response = await side.handle(scenario.app, upgradeRequest(), scenario.env);
    equal(response.status, 401, "R001 status");
    equal(await bodyOf(response), "Unauthorized", "R001 body");
    equal(downstream.length, 0, "R001 downstream calls");
  },

  async R002(side) {
    const seen = [];
    const downstream = [];
    const scenario = side.createApp({
      ctx: {
        options: { onBeforeRequest: () => { throw new Error("boom"); } },
        onError: (error) => seen.push(error.message)
      },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => { downstream.push("downstream"); return "downstream"; } }]
    });
    const response = await side.handle(scenario.app, partyRequest(), scenario.env);
    equal(response.status, 200, "R002 status");
    equal(await bodyOf(response), "downstream", "R002 body");
    deepEqual(seen, ["boom"], "R002 onError calls");
    equal(downstream.length, 1, "R002 downstream calls");
  },

  async R003(side) {
    const env = { JWT_SECRET: "s3cr3t" };
    let observed;
    const scenario = side.createApp({
      env,
      ctx: { options: { prefix: "parties", props: { room: "x" } } },
      route: (request, receivedEnv, options) => {
        observed = { request, env: receivedEnv, options };
        return new Response("delegated", { status: 200 });
      }
    });
    const response = await side.handle(scenario.app, partyRequest(), scenario.env);
    const calls = scenario.state.calls.filter((call) => new URL(call.request.url).pathname === "/parties/chat/room");
    equal(calls.length, 1, "R003 routePartykitRequest calls");
    ok(observed, "R003 routePartykitRequest was not called");
    equal(observed.request.url, "http://localhost/parties/chat/room", "R003 forwarded URL");
    deepEqual(observed.env, env, "R003 forwarded env");
    equal(observed.options?.props?.room, "x", "R003 forwarded props");
    equal(await bodyOf(response), "delegated", "R003 response body");
  },

  async R005(side) {
    const scenario = side.createApp({
      route: () => null,
      routes: [{ method: "get", path: "/health", handler: () => "ok" }]
    });
    const response = await side.handle(scenario.app, new Request("http://localhost/health"), scenario.env);
    equal(response.status, 200, "R005 status");
    equal(await bodyOf(response), "ok", "R005 body");
  },

  async R006(side) {
    const hits = [];
    const scenario = side.createApp({
      ctx: { options: { onBeforeRequest: () => { hits.push("hook"); } } },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => { hits.push("route"); return "routed"; } }]
    });
    const response = await side.handle(scenario.app, partyRequest(), scenario.env);
    deepEqual(hits, ["hook", "route"], "R006 ordering");
    equal(await bodyOf(response), "routed", "R006 body");
  },

  async R012(side) {
    const seen = [];
    const env = { JWT_SECRET: "s3cr3t" };
    const scenario = side.createApp({
      env,
      ctx: {
        options: {
          onBeforeRequest: (_request, _lobby, context) => {
            seen.push({ secret: context.env.JWT_SECRET, url: context.request.url });
          }
        }
      },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => "downstream" }]
    });
    await side.handle(scenario.app, partyRequest(), scenario.env);
    equal(seen.length, 1, "R012 hook calls");
    equal(seen[0].secret, "s3cr3t", "R012 env binding");
    equal(seen[0].url, "http://localhost/parties/chat/room", "R012 context request URL");
  },

  async R014(side) {
    const hooks = [];
    const routed = [];
    const scenario = side.createApp({
      ctx: {
        options: {
          prefix: "party",
          onBeforeRequest: () => { hooks.push("request"); },
          onBeforeConnect: () => { hooks.push("connect"); }
        }
      },
      route: (request) => { routed.push(request.url); return new Response("party", { status: 200 }); },
      routes: [{ method: "get", path: "/other", handler: () => "other" }]
    });
    const passthrough = await side.handle(scenario.app, partyRequest("/other"), scenario.env);
    equal(passthrough.status, 200, "R014 passthrough status");
    equal(await bodyOf(passthrough), "other", "R014 passthrough body");
    equal(hooks.length, 0, "R014 passthrough hooks");
    equal(routed.length, 0, "R014 passthrough routing");
    const intercepted = await side.handle(scenario.app, partyRequest("/party/chat/room"), scenario.env);
    equal(intercepted.status, 200, "R014 intercepted status");
    equal(await bodyOf(intercepted), "party", "R014 intercepted body");
    deepEqual(hooks, ["request"], "R014 intercepted hooks");
    equal(routed.length, 1, "R014 intercepted routing");
  },

  async R015(side) {
    const hooks = { request: 0, connect: 0 };
    const scenario = side.createApp({
      ctx: {
        options: {
          onBeforeRequest: () => { hooks.request += 1; },
          onBeforeConnect: () => { hooks.connect += 1; }
        }
      },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => "downstream" }]
    });
    const response = await side.handle(scenario.app, partyRequest(), scenario.env);
    equal(hooks.request, 1, "R015 onBeforeRequest calls");
    equal(hooks.connect, 0, "R015 onBeforeConnect calls");
    equal(await bodyOf(response), "downstream", "R015 body");
  },

  async R016(side) {
    const hooks = { request: 0, connect: 0 };
    const scenario = side.createApp({
      ctx: {
        options: {
          onBeforeRequest: () => { hooks.request += 1; },
          onBeforeConnect: () => { hooks.connect += 1; }
        }
      },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => "downstream" }]
    });
    const response = await side.handle(scenario.app, upgradeRequest(), scenario.env);
    equal(hooks.request, 0, "R016 onBeforeRequest calls");
    equal(hooks.connect, 1, "R016 onBeforeConnect calls");
    equal(await bodyOf(response), "downstream", "R016 body");
  },

  async R017(side) {
    const env = { JWT_SECRET: "s3cr3t" };
    let observed;
    const scenario = side.createApp({
      env,
      ctx: { options: { prefix: "parties", props: { room: "x" }, routingRetry: 3 } },
      route: async (request, receivedEnv, options) => {
        observed = { request, env: receivedEnv, options, body: await request.text() };
        return new Response("ok", { status: 200 });
      }
    });
    await side.handle(scenario.app, partyRequest("/parties/chat/room", { method: "POST", body: "payload" }), scenario.env);
    ok(observed, "R017 routePartykitRequest was not called");
    equal(observed.request.method, "POST", "R017 method");
    equal(observed.body, "payload", "R017 body");
    deepEqual(observed.env, env, "R017 forwarded env");
    equal(observed.options?.props?.room, "x", "R017 forwarded props");
    equal(observed.options?.routingRetry, 3, "R017 forwarded routingRetry");
  },

  async R018(side) {
    let observed;
    const scenario = side.createApp({
      route: async (request) => {
        observed = {
          url: request.url,
          method: request.method,
          token: request.headers.get("x-token"),
          body: await request.text()
        };
        return new Response("ok", { status: 200 });
      }
    });
    await side.handle(
      scenario.app,
      new Request("https://example.test/parties/chat/room?x=1", {
        method: "POST",
        headers: { "x-token": "t" },
        body: "payload"
      }),
      scenario.env
    );
    ok(observed, "R018 routePartykitRequest was not called");
    equal(observed.url, "https://example.test/parties/chat/room?x=1", "R018 URL");
    equal(observed.method, "POST", "R018 method");
    equal(observed.token, "t", "R018 header");
    equal(observed.body, "payload", "R018 body");
  },

  async R019(side) {
    const scenario = side.createApp({
      route: () => new Response("hello", { status: 201, headers: { "x-custom": "v" } })
    });
    const response = await side.handle(scenario.app, partyRequest(), scenario.env);
    equal(response.status, 201, "R019 status");
    equal(response.headers.get("x-custom"), "v", "R019 header");
    equal(await bodyOf(response), "hello", "R019 body");
  },

  async R022(side) {
    const downstream = [];
    const scenario = side.createApp({
      route: () => new Response("not found", { status: 404 }),
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => { downstream.push("downstream"); return "downstream"; } }]
    });
    const response = await side.handle(scenario.app, upgradeRequest(), scenario.env);
    equal(response.status, 404, "R022 status");
    equal(await bodyOf(response), "not found", "R022 body");
    equal(downstream.length, 0, "R022 downstream calls");
  },

  async R024(side) {
    let replaced;
    const first = side.createApp({
      ctx: {
        options: {
          onBeforeConnect: () => new Request("http://localhost/parties/other/room", { headers: { Upgrade: "websocket" } })
        }
      },
      route: (request) => { replaced = request.url; return new Response("ok", { status: 200 }); }
    });
    await side.handle(first.app, upgradeRequest(), first.env);
    equal(replaced, "http://localhost/parties/other/room", "R024 replacement request");

    let original;
    const second = side.createApp({
      ctx: { options: { onBeforeConnect: () => undefined } },
      route: (request) => { original = request.url; return new Response("ok", { status: 200 }); }
    });
    await side.handle(second.app, upgradeRequest(), second.env);
    equal(original, "http://localhost/parties/chat/room", "R024 original request");
  },

  async R025(side) {
    const downstream = [];
    const scenario = side.createApp({
      ctx: {
        options: {
          onBeforeConnect: () => new Response("forbidden", { status: 403, headers: { "x-rejected": "yes" } })
        }
      },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => { downstream.push("downstream"); return "downstream"; } }]
    });
    const response = await side.handle(scenario.app, upgradeRequest(), scenario.env);
    equal(response.status, 403, "R025 status");
    equal(response.headers.get("x-rejected"), "yes", "R025 header");
    equal(await bodyOf(response), "forbidden", "R025 body");
    equal(downstream.length, 0, "R025 downstream calls");
  },

  async R026(side) {
    let replaced;
    const first = side.createApp({
      ctx: {
        options: {
          onBeforeRequest: () => new Request("http://localhost/parties/chat/room", { headers: { "x-replaced": "yes" } })
        }
      },
      route: (request) => { replaced = request.headers.get("x-replaced"); return new Response("ok", { status: 200 }); }
    });
    await side.handle(first.app, partyRequest(), first.env);
    equal(replaced, "yes", "R026 replacement header");

    let mutated;
    const second = side.createApp({
      ctx: {
        options: {
          onBeforeRequest: (request) => { request.headers.set("x-mutated", "yes"); }
        }
      },
      route: (request) => { mutated = request.headers.get("x-mutated"); return new Response("ok", { status: 200 }); }
    });
    await side.handle(second.app, partyRequest(), second.env);
    equal(mutated, "yes", "R026 mutation header");
  },

  async R027(side) {
    const downstream = [];
    const scenario = side.createApp({
      ctx: { options: { onBeforeRequest: () => new Response("blocked", { status: 401 }) } },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => { downstream.push("downstream"); return "downstream"; } }]
    });
    const response = await side.handle(scenario.app, partyRequest(), scenario.env);
    equal(response.status, 401, "R027 status");
    equal(await bodyOf(response), "blocked", "R027 body");
    equal(downstream.length, 0, "R027 downstream calls");
  },

  async R028(side) {
    const seen = [];
    const scenario = side.createApp({
      ctx: {
        options: { onBeforeConnect: () => { throw new Error("boom"); } },
        onError: (error) => seen.push(error.message)
      },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => "next" }]
    });
    const response = await side.handle(scenario.app, upgradeRequest(), scenario.env);
    equal(response.status, 200, "R028 status");
    equal(await bodyOf(response), "next", "R028 body");
    deepEqual(seen, ["boom"], "R028 onError calls");
  },

  async R029(side) {
    const downstream = [];
    const thrown = new Error("boom");
    const scenario = side.createApp({
      ctx: { options: { onBeforeRequest: () => { throw thrown; } } },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => { downstream.push("downstream"); return "downstream"; } }]
    });
    let propagated = false;
    try {
      const response = await side.handle(scenario.app, partyRequest(), scenario.env);
      propagated = response.status >= 500 || (await bodyOf(response)).includes("boom");
    } catch (error) {
      equal(error, thrown, "R029 original error identity");
      propagated = true;
    }
    equal(downstream.length, 0, "R029 downstream calls");
    ok(propagated, "R029 error must propagate");
  },

  async R030(side) {
    const seen = [];
    const first = side.createApp({
      ctx: {
        options: { onBeforeConnect: () => { throw new Error("connect"); } },
        onError: (error) => seen.push(error.message)
      },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => "next" }]
    });
    const firstResponse = await side.handle(first.app, upgradeRequest(), first.env);
    equal(firstResponse.status, 200, "R030 onError status");
    equal(await bodyOf(firstResponse), "next", "R030 onError body");
    deepEqual(seen, ["connect"], "R030 onError calls");

    const downstream = [];
    const thrown = new Error("request");
    const second = side.createApp({
      ctx: { options: { onBeforeRequest: () => { throw thrown; } } },
      route: () => null,
      routes: [{ method: "get", path: "/parties/chat/room", handler: () => { downstream.push("downstream"); return "downstream"; } }]
    });
    let propagated = false;
    try {
      const response = await side.handle(second.app, partyRequest(), second.env);
      propagated = response.status >= 500 || (await bodyOf(response)).includes("request");
    } catch (error) {
      equal(error, thrown, "R030 original error identity");
      propagated = true;
    }
    equal(downstream.length, 0, "R030 propagation downstream calls");
    ok(propagated, "R030 error must propagate");
  },

  async R036(side) {
    const manifest = side.packageJson;
    const peer = manifest?.peerDependencies?.elysia;
    const dev = manifest?.devDependencies?.elysia;
    ok(Boolean(peer), "R036 missing peerDependencies.elysia");
    ok(Boolean(dev), "R036 missing devDependencies.elysia");
    ok(satisfiesRange("1.3.0", peer), `R036 peer range does not include 1.3.0: ${peer}`);
    ok(satisfiesRange("1.3.0", dev), `R036 dev range does not include 1.3.0: ${dev}`);
    equal(side.installedVersions?.elysia, "1.3.0", "R036 installed elysia version");
  },

  async R037(side) {
    equal(side.packageJson?.peerDependencies?.partyserver, ">=0.2.0 <1.0.0", "R037 partyserver peer range");
    equal(side.installedVersions?.partyserver, "0.5.10", "R037 installed partyserver version");
    ok(side.partyserverImportable, "R037 partyserver is not importable");
  }
};

export async function runAll(side) {
  const results = [];
  for (const observation of OBSERVATIONS) {
    const checkResults = [];
    for (const name of observation.checks) {
      try {
        await CHECKS[name](side);
        checkResults.push({ name, status: "PASS" });
      } catch (error) {
        checkResults.push({ name, status: "FAIL", error: error instanceof Error ? error.message : String(error) });
      }
    }
    const targetTechnology = side.targetTechnology;
    const passed = targetTechnology.ok && checkResults.every((result) => result.status === "PASS");
    results.push({
      observation_id: observation.id,
      requirement_ids: observation.requirement_ids,
      status: passed ? "PASS" : "FAIL",
      target_technology: targetTechnology,
      checks: checkResults
    });
  }
  return {
    side: side.name,
    target_technology: side.targetTechnology,
    observations: results,
    passed: results.filter((result) => result.status === "PASS").length,
    failed: results.filter((result) => result.status !== "PASS").length
  };
}
