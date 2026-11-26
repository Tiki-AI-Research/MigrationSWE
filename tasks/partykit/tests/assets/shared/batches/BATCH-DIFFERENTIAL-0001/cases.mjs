import { getState, reset } from "../../../fixtures/batches/BATCH-DIFFERENTIAL-0001/route-mock.mjs";
import { installResponse101 } from "../../../fixtures/batches/BATCH-DIFFERENTIAL-0001/response-101.mjs";
import { matchesPartyserverPath } from "../partyserver-prefix.mjs";

export const lobby = { party: "chat", className: "Chat", name: "room" };

function request(method, url, init = {}) {
  return new Request(url, { method, ...init });
}

async function responseInfo(response) {
  return {
    status: response.status,
    body: await response.text(),
    headers: Object.fromEntries(response.headers)
  };
}

function isUpgrade(request) {
  return (request.headers.get("upgrade") || "").toLowerCase() === "websocket";
}

async function routeWithHooks({ request, options }) {
  if (isUpgrade(request)) {
    const result = await options?.onBeforeConnect?.(request, lobby);
    if (result instanceof Response) return result;
    if (result instanceof Request) return null;
  } else {
    const result = await options?.onBeforeRequest?.(request, lobby);
    if (result instanceof Response) return result;
    if (result instanceof Request) return null;
  }
  return null;
}

function hookCase(upgrade) {
  return async (subject) => {
    const seen = [];
    reset(async ({ request, options }) => {
      if (isUpgrade(request) === upgrade) {
        const hook = upgrade ? options?.onBeforeConnect : options?.onBeforeRequest;
        if (hook) {
          seen.push(upgrade ? "connect" : "request");
          await hook(request, lobby);
        }
      }
      return null;
    });
    const options = {
      onBeforeConnect: () => {
        seen.push("connect-hook");
      },
      onBeforeRequest: () => {
        seen.push("request-hook");
      }
    };
    const app = subject.create({ options });
    await subject.handle(
      app,
      request("GET", "http://localhost/parties/chat/room", {
        headers: upgrade ? { Upgrade: "websocket" } : {}
      })
    );
    return {
      onBeforeRequest: seen.filter((entry) => entry === "request").length,
      onBeforeConnect: seen.filter((entry) => entry === "connect").length
    };
  };
}

function define(id, requirementIds, expected, run) {
  return { id, requirement_ids: requirementIds, expected, run };
}

export const cases = [
  define(
    "DIFF-001",
    ["R003"],
    {
      status: 202,
      body: "routed-001",
      header: "yes",
      callCount: 1,
      url: "http://localhost/parties/chat/room",
      method: "GET",
      prefix: "parties",
      cors: "*",
      envObject: true
    },
    async (subject) => {
      reset(() => new Response("routed-001", { status: 202, headers: { "x-route": "yes" } }));
      const options = { prefix: "parties", cors: { origin: "*" } };
      const app = subject.create({ options });
      const response = await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      const call = getState().calls[0];
      const info = await responseInfo(response);
      return {
        status: info.status,
        body: info.body,
        header: info.headers["x-route"],
        callCount: getState().calls.length,
        url: call?.request.url,
        method: call?.request.method,
        prefix: call?.options?.prefix,
        cors: call?.options?.cors?.origin,
        envObject: !!call?.env && typeof call.env === "object"
      };
    }
  ),
  define(
    "DIFF-002",
    ["R004"],
    { callable: true, registered: true },
    async (subject) => {
      reset(() => null);
      const plugin = subject.mod.partyserverMiddleware();
      const registered = await subject.registerZeroArgumentPlugin(plugin);
      return {
        callable: typeof subject.mod.partyserverMiddleware === "function",
        registered
      };
    }
  ),
  define(
    "DIFF-003",
    ["R007"],
    { packageName: "hono-party", rootExportCallable: true },
    async (subject) => {
      const packageJson = await subject.packageJson();
      return {
        packageName: packageJson.name,
        rootExportCallable: typeof subject.mod.partyserverMiddleware === "function"
      };
    }
  ),
  define(
    "DIFF-004",
    ["R008"],
    {
      prefix: "party",
      cors: "*",
      jurisdiction: "eu",
      locationHint: "weur",
      props: "x",
      routingRetry: 3
    },
    async (subject) => {
      const options = {
        prefix: "party",
        cors: { origin: "*" },
        jurisdiction: "eu",
        locationHint: "weur",
        props: { room: "x" },
        routingRetry: 3
      };
      reset(() => null);
      const app = subject.create({ options });
      await subject.handle(app, request("GET", "http://localhost/party/chat/room"));
      const forwarded = getState().calls[0]?.options;
      return {
        prefix: forwarded?.prefix,
        cors: forwarded?.cors?.origin,
        jurisdiction: forwarded?.jurisdiction,
        locationHint: forwarded?.locationHint,
        props: forwarded?.props?.room,
        routingRetry: forwarded?.routingRetry
      };
    }
  ),
  define(
    "DIFF-005",
    ["R009"],
    {
      prefix: "party",
      cors: "*",
      jurisdiction: "eu",
      locationHint: "weur",
      props: "x",
      routingRetry: 3
    },
    async (subject) => {
      const options = {
        prefix: "party",
        cors: { origin: "*" },
        jurisdiction: "eu",
        locationHint: "weur",
        props: { room: "x" },
        routingRetry: 3
      };
      reset(() => null);
      const app = subject.create({ options });
      await subject.handle(app, request("GET", "http://localhost/party/chat/room"));
      const forwarded = getState().calls[0]?.options;
      return {
        prefix: forwarded?.prefix,
        cors: forwarded?.cors?.origin,
        jurisdiction: forwarded?.jurisdiction,
        locationHint: forwarded?.locationHint,
        props: forwarded?.props?.room,
        routingRetry: forwarded?.routingRetry
      };
    }
  ),
  define(
    "DIFF-006",
    ["R010"],
    { withCtxCalls: 1, message: "x", sameInstance: true, withoutCtxCalls: 0 },
    async (subject) => {
      const error = new Error("x");
      const seen = [];
      reset(() => {
        throw error;
      });
      const app = subject.create({ onError: (caught) => seen.push(caught) });
      await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      const withCtx = seen.slice();
      const appWithoutContext = subject.create();
      try {
        await subject.handle(appWithoutContext, request("GET", "http://localhost/parties/chat/room"));
      } catch (caught) {
        if (caught !== error) throw caught;
      }
      return {
        withCtxCalls: withCtx.length,
        message: withCtx[0]?.message,
        sameInstance: withCtx[0] === error,
        withoutCtxCalls: seen.length - withCtx.length
      };
    }
  ),
  define(
    "DIFF-007",
    ["R011"],
    {
      argCount: 3,
      requestUrl: "http://localhost/parties/chat/room",
      lobbyId: "chat",
      contextObject: true
    },
    async (subject) => {
      const seen = [];
      const options = {
        onBeforeRequest: (incoming, currentLobby, context) => {
          seen.push({
            requestUrl: incoming.url,
            lobbyId: currentLobby.party,
            contextType: typeof context,
            contextKeys: Object.keys(context)
          });
        }
      };
      reset(routeWithHooks);
      const app = subject.create({ options });
      await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      const call = seen[0] || {};
      return {
        argCount: call.contextType ? 3 : 0,
        requestUrl: call.requestUrl,
        lobbyId: call.lobbyId,
        contextObject: call.contextType === "object"
      };
    }
  ),
  define(
    "DIFF-008",
    ["R013"],
    { envPresent: true },
    async (subject) => {
      process.env.JWT_SECRET = "s3cr3t";
      reset(() => null);
      const app = subject.create({ options: {} });
      await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      const forwarded = getState().calls[0];
      return {
        envPresent: !!forwarded?.env && typeof forwarded.env === "object"
      };
    }
  ),
  define(
    "DIFF-009",
    ["R014"],
    {
      outsideStatus: 200,
      outsideBody: "other",
      outsideHookCalls: 0,
      insideRouteCalls: 1,
      insideStatus: 200,
      insideBody: "party-next"
    },
    async (subject) => {
      const seen = [];
      reset(async ({ request, options }) => {
        const path = new URL(request.url).pathname;
        const matched = matchesPartyserverPath(path, options?.prefix);
        if (matched && !isUpgrade(request) && options?.onBeforeRequest) {
          seen.push("hook");
          const result = await options.onBeforeRequest(request, lobby);
          if (result instanceof Response) return result;
        }
        return null;
      });
      const options = {
        prefix: "party",
        onBeforeRequest: () => {
          seen.push("hook");
        }
      };
      const app = subject.create({ options });
      const outside = await subject.handle(app, request("GET", "http://localhost/other"));
      const outsideRouteCalls = getState().calls.length;
      const outsideHookCalls = seen.length;
      const inside = await subject.handle(app, request("GET", "http://localhost/party/chat/room"));
      return {
        outsideStatus: outside.status,
        outsideBody: await outside.text(),
        outsideHookCalls,
        // An adapter may reject an out-of-prefix path before delegation.
        // Count only the matched request, irrespective of that choice.
        insideRouteCalls: getState().calls.length - outsideRouteCalls,
        insideStatus: inside.status,
        insideBody: await inside.text()
      };
    }
  ),
  define(
    "DIFF-010",
    ["R015"],
    { onBeforeRequest: 1, onBeforeConnect: 0 },
    hookCase(false)
  ),
  define(
    "DIFF-011",
    ["R016"],
    { onBeforeRequest: 0, onBeforeConnect: 1 },
    hookCase(true)
  ),
  define(
    "DIFF-012",
    ["R017"],
    {
      status: 200,
      body: "posted",
      url: "http://localhost/parties/chat/room",
      method: "POST",
      reqBody: "payload",
      envObject: true,
      prefix: "parties"
    },
    async (subject) => {
      reset(() => new Response("posted", { status: 200 }));
      const options = { prefix: "parties" };
      const app = subject.create({ options });
      const response = await subject.handle(
        app,
        request("POST", "http://localhost/parties/chat/room", { body: "payload" })
      );
      const call = getState().calls[0];
      return {
        status: response.status,
        body: await response.text(),
        url: call?.request.url,
        method: call?.request.method,
        reqBody: await call?.request.text(),
        envObject: !!call?.env && typeof call.env === "object",
        prefix: call?.options?.prefix
      };
    }
  ),
  define(
    "DIFF-013",
    ["R018"],
    {
      url: "https://example.test/parties/chat/room?x=1",
      method: "POST",
      token: "t",
      body: "payload"
    },
    async (subject) => {
      reset(() => new Response("ok", { status: 200 }));
      const app = subject.create({});
      await subject.handle(
        app,
        request("POST", "https://example.test/parties/chat/room?x=1", {
          headers: { "x-token": "t" },
          body: "payload"
        })
      );
      const call = getState().calls[0];
      return {
        url: call?.request.url,
        method: call?.request.method,
        token: call?.request.headers.get("x-token"),
        body: await call?.request.text()
      };
    }
  ),
  define(
    "DIFF-014",
    ["R019"],
    { status: 201, body: "hello", header: "v", callCount: 1 },
    async (subject) => {
      reset(() => new Response("hello", { status: 201, headers: { "x-custom": "v" } }));
      const app = subject.create({});
      const response = await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      const info = await responseInfo(response);
      return {
        status: info.status,
        body: info.body,
        header: info.headers["x-custom"],
        callCount: getState().calls.length
      };
    }
  ),
  define(
    "DIFF-015",
    ["R020"],
    {
      status: 200,
      body: "next",
      downstream: ["/parties/chat/room"],
      routeCalls: 1
    },
    async (subject) => {
      reset(() => null);
      const app = subject.create({});
      const response = await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      return {
        status: response.status,
        body: await response.text(),
        downstream: subject.downstream.slice(),
        routeCalls: getState().calls.length
      };
    }
  ),
  define(
    "DIFF-016",
    ["R021"],
    { status: 101, hasWebSocket: true, downstream: [] },
    async (subject) => {
      const handle = { accept() {} };
      reset(() => Object.assign(new Response(null, { status: 200 }), { webSocket: handle }));
      const app = subject.create({});
      const restore = installResponse101();
      try {
        const response = await subject.handle(
          app,
          request("GET", "http://localhost/parties/chat/room", {
            headers: { Upgrade: "websocket" }
          })
        );
        return {
          status: response.status,
          hasWebSocket: response.webSocket === handle,
          downstream: subject.downstream.slice()
        };
      } finally {
        restore();
      }
    }
  ),
  define(
    "DIFF-017",
    ["R023"],
    {
      status: 200,
      body: "next",
      downstream: ["/parties/chat/room"],
      routeCalls: 1
    },
    async (subject) => {
      reset(() => null);
      const app = subject.create({});
      const response = await subject.handle(
        app,
        request("GET", "http://localhost/parties/chat/room", {
          headers: { Upgrade: "websocket" }
        })
      );
      return {
        status: response.status,
        body: await response.text(),
        downstream: subject.downstream.slice(),
        routeCalls: getState().calls.length
      };
    }
  ),
  define(
    "DIFF-018",
    ["R024"],
    {
      first: "http://localhost/parties/other/room",
      second: "http://localhost/parties/chat/room"
    },
    async (subject) => {
      const forwarded = [];
      const options = {
        onBeforeConnect: (incoming) =>
          incoming.url.includes("other")
            ? new Request("http://localhost/parties/other/room", {
                headers: incoming.headers
              })
            : undefined
      };
      reset(async ({ request, options: currentOptions }) => {
        let nextRequest = request;
        if (isUpgrade(request) && currentOptions?.onBeforeConnect) {
          const replacement = await currentOptions.onBeforeConnect(request, lobby);
          if (replacement instanceof Request) nextRequest = replacement;
        }
        forwarded.push(nextRequest.url);
        return new Response("ok", { status: 200 });
      });
      const app = subject.create({ options });
      await subject.handle(
        app,
        request("GET", "http://localhost/parties/other/room", {
          headers: { Upgrade: "websocket" }
        })
      );
      await subject.handle(
        app,
        request("GET", "http://localhost/parties/chat/room", {
          headers: { Upgrade: "websocket" }
        })
      );
      return { first: forwarded[0], second: forwarded[1] };
    }
  ),
  define(
    "DIFF-019",
    ["R026"],
    { replaced: "yes", mutated: "yes" },
    async (subject) => {
      const seen = [];
      reset(async ({ request, options }) => {
        let nextRequest = request;
        if (options?.onBeforeRequest) {
          const replacement = await options.onBeforeRequest(request, lobby);
          if (replacement instanceof Request) nextRequest = replacement;
        }
        seen.push({
          replaced: nextRequest.headers.get("x-replaced"),
          mutated: nextRequest.headers.get("x-mutated")
        });
        return new Response("ok", { status: 200 });
      });
      const options = {
        onBeforeRequest: (incoming) => {
          if (incoming.url.includes("replace")) {
            return new Request("http://localhost/parties/chat/room", {
              headers: { ...Object.fromEntries(incoming.headers), "x-replaced": "yes" }
            });
          }
          incoming.headers.set("x-mutated", "yes");
        }
      };
      const app = subject.create({ options });
      await subject.handle(app, request("GET", "http://localhost/parties/replace/room"));
      await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      return { replaced: seen[0]?.replaced, mutated: seen[1]?.mutated };
    }
  ),
  define(
    "DIFF-020",
    ["R027"],
    { status: 401, body: "blocked", downstream: [] },
    async (subject) => {
      reset(routeWithHooks);
      const options = {
        onBeforeRequest: () => new Response("blocked", { status: 401 })
      };
      const app = subject.create({ options });
      const response = await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      return {
        status: response.status,
        body: await response.text(),
        downstream: subject.downstream.slice()
      };
    }
  ),
  define(
    "DIFF-021",
    ["R028"],
    {
      onErrorCalls: 1,
      message: "boom",
      status: 200,
      body: "next",
      downstream: ["/parties/chat/room"]
    },
    async (subject) => {
      const seen = [];
      reset(() => {
        throw new Error("boom");
      });
      const app = subject.create({ onError: (error) => seen.push(error.message) });
      const response = await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      return {
        onErrorCalls: seen.length,
        message: seen[0],
        status: response.status,
        body: await response.text(),
        downstream: subject.downstream.slice()
      };
    }
  ),
  define(
    "DIFF-022",
    ["R029"],
    { downstreamCalls: 0, errorSurfaced: true },
    async (subject) => {
      const thrown = new Error("boom");
      reset(() => { throw thrown; });
      const app = subject.create();
      let errorSurfaced = false;
      try {
        const response = await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
        errorSurfaced = response.status >= 500 || (await response.text()).includes("boom");
      } catch (caught) {
        if (caught !== thrown) throw caught;
        errorSurfaced = true;
      }
      return { downstreamCalls: subject.downstream.length, errorSurfaced };
    }
  ),
  define(
    "DIFF-023",
    ["R030"],
    {
      withError: ["connect"],
      withStatus: 200,
      withBody: "next",
      withoutError: ["request"],
      withoutStatus: 200
    },
    async (subject) => {
      const withSeen = [];
      reset(async ({ request, options }) => {
        if (isUpgrade(request)) {
          const result = await options?.onBeforeConnect?.(request, lobby);
          if (result instanceof Response) return result;
        }
        return null;
      });
      const withApp = subject.create({
        options: {
          onBeforeConnect: () => {
            throw new Error("connect");
          }
        },
        onError: (error) => withSeen.push(error.message)
      });
      const withResponse = await subject.handle(
        withApp,
        request("GET", "http://localhost/parties/chat/room", {
          headers: { Upgrade: "websocket" }
        })
      );

      const withoutSeen = [];
      reset(async ({ request, options }) => {
        if (!isUpgrade(request)) {
          const result = await options?.onBeforeRequest?.(request, lobby);
          if (result instanceof Response) return result;
        }
        return null;
      });
      const withoutApp = subject.create({
        options: {
          onBeforeRequest: () => {
            throw new Error("request");
          }
        },
        onError: (error) => withoutSeen.push(error.message)
      });
      const withoutResponse = await subject.handle(
        withoutApp,
        request("GET", "http://localhost/parties/chat/room")
      );

      return {
        withError: withSeen,
        withStatus: withResponse.status,
        withBody: await withResponse.text(),
        withoutError: withoutSeen,
        withoutStatus: withoutResponse.status
      };
    }
  ),
  define(
    "DIFF-024",
    ["R031"],
    {
      optionsKeys: [
        "cors",
        "jurisdiction",
        "locationHint",
        "onBeforeRequest",
        "prefix",
        "props",
        "routingRetry"
      ],
      hookCalls: 1,
      status: 200,
      body: "routed"
    },
    async (subject) => {
      const seen = [];
      const options = {
        prefix: "parties",
        cors: { origin: "*" },
        jurisdiction: "eu",
        locationHint: "weur",
        props: { room: "x" },
        routingRetry: 3,
        onBeforeRequest: () => {
          seen.push("hook");
        }
      };
      reset(async ({ options: forwarded }) => {
        if (forwarded?.onBeforeRequest) {
          await forwarded.onBeforeRequest(new Request("http://localhost/parties/chat/room"), lobby);
        }
        return new Response("routed", { status: 200 });
      });
      const app = subject.create({
        options,
        onError: (error) => seen.push(error.message)
      });
      const response = await subject.handle(app, request("GET", "http://localhost/parties/chat/room"));
      return {
        optionsKeys: Object.keys(getState().calls[0]?.options || {}).sort(),
        hookCalls: seen.length,
        status: response.status,
        body: await response.text()
      };
    }
  )
];

function stable(value) {
  return JSON.stringify(value);
}

export async function runAll(subject) {
  const observations = [];
  for (const testCase of cases) {
    try {
      const actual = await testCase.run(subject);
      observations.push({
        observation_id: testCase.id,
        requirement_ids: testCase.requirement_ids,
        status: stable(actual) === stable(testCase.expected) ? "PASS" : "FAIL",
        expected: testCase.expected,
        actual
      });
    } catch (error) {
      observations.push({
        observation_id: testCase.id,
        requirement_ids: testCase.requirement_ids,
        status: "FAIL",
        expected: testCase.expected,
        error: error instanceof Error ? error.message : String(error)
      });
    }
  }
  const failed = observations.filter((observation) => observation.status !== "PASS").length;
  return {
    schema_version: "migration-differential-report:v1",
    batch_id: "BATCH-DIFFERENTIAL-0001",
    side: subject.name,
    observations,
    summary: {
      total: observations.length,
      passed: observations.length - failed,
      failed
    }
  };
}
