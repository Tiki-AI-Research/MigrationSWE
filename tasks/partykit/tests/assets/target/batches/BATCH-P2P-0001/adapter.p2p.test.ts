import { beforeEach, describe, expect, it, vi } from "vitest";

const { routePartykitRequest } = vi.hoisted(() => ({
  routePartykitRequest: vi.fn()
}));

vi.mock("partyserver", () => ({ routePartykitRequest }));
vi.mock("cloudflare:workers", () => ({
  env: { P2P_TEST: "yes", JWT_SECRET: "secret" }
}));

import { Elysia } from "elysia";

import { partyserverMiddleware } from "../../../packages/hono-party/src/index";

const LOBBY = { name: "test-lobby" } as never;

const NativeResponse = globalThis.Response;
class WebSocketResponse extends NativeResponse {
  declare webSocket: WebSocket | null;

  constructor(
    body?: BodyInit | null,
    init?: ResponseInit & { webSocket?: unknown }
  ) {
    const requestedStatus = init?.status ?? 200;
    const nativeInit = requestedStatus === 101 ? { ...init, status: 200 } : init;
    super(body, nativeInit);
    if (requestedStatus === 101) {
      Object.defineProperty(this, "status", { value: 101 });
    }
    this.webSocket = (init?.webSocket as WebSocket | null | undefined) ?? null;
  }
}
(globalThis as { Response: typeof Response }).Response =
  WebSocketResponse as unknown as typeof Response;

type AnyOptions = {
  onBeforeConnect?: (
    request: Request,
    lobby: unknown,
    context: unknown
  ) => unknown;
  onBeforeRequest?: (
    request: Request,
    lobby: unknown,
    context: unknown
  ) => unknown;
};

function appWith(
  ctx?: Parameters<typeof partyserverMiddleware>[0],
  downstream = "next"
) {
  const app = new Elysia();
  app.use(partyserverMiddleware(ctx));
  app.all("/parties/chat/room", () => downstream);
  return app;
}

function request(path = "/parties/chat/room", init: RequestInit = {}) {
  return new Request(`http://localhost${path}`, init);
}

async function handle(app: Elysia, req: Request) {
  return app.handle(req);
}

beforeEach(() => {
  routePartykitRequest.mockReset();
});

describe("hono-party P2P adapter contract", () => {
  it("P2P-005 forwards onBeforeRequest replacements and in-place mutations", async () => {
    let replacementHeader: string | null = null;
    routePartykitRequest.mockImplementation(
      async (request: Request, _env: unknown, routeOptions: AnyOptions) => {
        const replacement = await routeOptions.onBeforeRequest?.(
          request,
          LOBBY,
          null
        );
        const forwarded =
          replacement instanceof Request ? replacement : request;
        replacementHeader = forwarded.headers.get("x-replaced");
        return null;
      }
    );

    await handle(
      appWith({
        options: {
          onBeforeRequest: () =>
            new Request("http://localhost/parties/chat/room", {
              headers: { "x-replaced": "yes" }
            })
        }
      }),
      request()
    );

    let mutationHeader: string | null = null;
    routePartykitRequest.mockImplementation(
      async (request: Request, _env: unknown, routeOptions: AnyOptions) => {
        const result = await routeOptions.onBeforeRequest?.(
          request,
          LOBBY,
          null
        );
        const forwarded = result instanceof Request ? result : request;
        mutationHeader = forwarded.headers.get("x-mutated");
        return null;
      }
    );

    await handle(
      appWith({
        options: {
          onBeforeRequest: (request) => {
            request.headers.set("x-mutated", "yes");
            return undefined;
          }
        }
      }),
      request()
    );

    expect({
      replacementHeader,
      mutationHeader
    }).toEqual({
      replacementHeader: "yes",
      mutationHeader: "yes"
    });
  });

  it("P2P-008 forwards a 101 response with the PartyServer web socket handle", async () => {
    const webSocket = { accept: () => undefined };
    routePartykitRequest.mockResolvedValue(
      new Response(null, { status: 200, webSocket } as ResponseInit)
    );
    let downstreamCalls = 0;
    const app = new Elysia();
    app.use(partyserverMiddleware());
    app.all("/parties/chat/room", () => {
      downstreamCalls += 1;
      return "next";
    });

    const response = await handle(
      app,
      request("/parties/chat/room", { headers: { Upgrade: "websocket" } })
    );

    expect({
      status: response.status,
      downstreamCalls,
      sameWebSocket: response.webSocket === webSocket
    }).toEqual({
      status: 101,
      downstreamCalls: 0,
      sameWebSocket: true
    });
  });

  it("P2P-009 invokes both hooks with request, lobby and context", async () => {
    const calls: Array<{ kind: string; length: number; context: unknown }> = [];
    const options: AnyOptions = {
      onBeforeConnect: (...args: unknown[]) => {
        calls.push({
          kind: "connect",
          length: args.length,
          context: args[2]
        });
        return undefined;
      },
      onBeforeRequest: (...args: unknown[]) => {
        calls.push({
          kind: "request",
          length: args.length,
          context: args[2]
        });
        return undefined;
      }
    };
    routePartykitRequest.mockImplementation(
      async (req: Request, _env: unknown, routeOptions: AnyOptions) => {
        const hook = req.headers.get("upgrade")?.toLowerCase() === "websocket"
          ? routeOptions.onBeforeConnect
          : routeOptions.onBeforeRequest;
        await hook?.(req, LOBBY, null);
        return null;
      }
    );

    const app = appWith({ options: options as never });
    await handle(app, request());
    await handle(app, request("/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    }));

    expect(
      calls.map((call) => ({
        kind: call.kind,
        length: call.length,
        contextIsObject:
          typeof call.context === "object" && call.context !== null
      }))
    ).toEqual([
      { kind: "request", length: 3, contextIsObject: true },
      { kind: "connect", length: 3, contextIsObject: true }
    ]);
  });

  it("P2P-011 forwards url, method, body, bindings and options", async () => {
    let forwardedBody = "";
    routePartykitRequest.mockImplementation(async (req: Request) => {
      forwardedBody = await req.text();
      return null;
    });
    const options = { prefix: "parties" } as never;
    const app = appWith({ options });

    const response = await handle(
      app,
      request("/parties/chat/room", {
        method: "POST",
        headers: { "content-type": "text/plain" },
        body: "payload"
      })
    );

    const [forwarded, bindings, forwardedOptions] =
      routePartykitRequest.mock.calls[0] as [
        Request,
        Record<string, unknown>,
        Record<string, unknown>
      ];

    expect({
      status: response.status,
      method: forwarded.method,
      path: new URL(forwarded.url).pathname,
      body: forwardedBody,
      binding: bindings.P2P_TEST,
      optionKeys: Object.keys(forwardedOptions ?? {})
    }).toEqual({
      status: 200,
      method: "POST",
      path: "/parties/chat/room",
      body: "payload",
      binding: "yes",
      optionKeys: ["prefix"]
    });
  });

  it("P2P-013 passes a non-upgrade response through unchanged", async () => {
    routePartykitRequest.mockResolvedValue(
      new Response("hello", {
        status: 201,
        headers: { "x-custom": "v" }
      })
    );

    const response = await handle(appWith(), request());

    expect({
      status: response.status,
      header: response.headers.get("x-custom"),
      body: await response.text()
    }).toEqual({ status: 201, header: "v", body: "hello" });
  });

  it("P2P-014 lets onBeforeRequest take over the response", async () => {
    routePartykitRequest.mockImplementation(
      async (_request: Request, _env: unknown, routeOptions: AnyOptions) =>
        routeOptions.onBeforeRequest?.(request(), LOBBY, null) ?? null
    );
    let downstreamCalls = 0;
    const app = new Elysia();
    app.use(
      partyserverMiddleware({
        options: {
          onBeforeRequest: () => new Response("blocked", { status: 401 })
        }
      })
    );
    app.all("/parties/chat/room", () => {
      downstreamCalls += 1;
      return "next";
    });

    const response = await handle(app, request());

    expect({
      status: response.status,
      body: await response.text(),
      downstreamCalls
    }).toEqual({ status: 401, body: "blocked", downstreamCalls: 0 });
  });

  it("P2P-016 falls through when PartyServer returns null", async () => {
    routePartykitRequest.mockResolvedValue(null);

    const response = await handle(appWith(), request());

    expect({
      status: response.status,
      body: await response.text()
    }).toEqual({ status: 200, body: "next" });
  });

  it("P2P-018 delegates to routePartykitRequest exactly once", async () => {
    routePartykitRequest.mockResolvedValue(
      new Response("routed", { status: 202 })
    );

    const response = await handle(appWith(undefined, "not-used"), request());

    expect({
      calls: routePartykitRequest.mock.calls.length,
      status: response.status,
      body: await response.text()
    }).toEqual({ calls: 1, status: 202, body: "routed" });
  });

  it("P2P-021 invokes onError and then continues downstream", async () => {
    const seen: string[] = [];
    routePartykitRequest.mockRejectedValue(new Error("boom"));

    const response = await handle(
      appWith({ onError: (error) => seen.push(error.message) }),
      request()
    );

    expect({
      errors: seen,
      status: response.status,
      body: await response.text()
    }).toEqual({ errors: ["boom"], status: 200, body: "next" });
  });

  it("P2P-023 preserves the public middleware capability", async () => {
    routePartykitRequest.mockResolvedValue(null);
    const seen: string[] = [];
    const app = appWith({
      options: {
        prefix: "parties",
        cors: true,
        jurisdiction: "eu",
        locationHint: "weur",
        props: { role: "tester" },
        routingRetry: { maxAttempts: 2 }
      },
      onError: (error) => seen.push(error.message)
    });

    const response = await handle(app, request());
    const options = routePartykitRequest.mock.calls[0]?.[2] as
      | Record<string, unknown>
      | undefined;

    expect({
      exported: typeof partyserverMiddleware,
      status: response.status,
      body: await response.text(),
      optionKeys: Object.keys(options ?? {}).sort(),
      errorWasRecorded: Array.isArray(seen)
    }).toEqual({
      exported: "function",
      status: 200,
      body: "next",
      optionKeys: [
        "cors",
        "jurisdiction",
        "locationHint",
        "prefix",
        "props",
        "routingRetry"
      ],
      errorWasRecorded: true
    });
  });

  it("P2P-024 handles hook errors with and without onError", async () => {
    const seen: string[] = [];
    routePartykitRequest.mockImplementation(
      async (_request: Request, _env: unknown, routeOptions: AnyOptions) =>
        routeOptions.onBeforeConnect?.(_request, LOBBY, null) ?? null
    );

    const withHandler = await handle(
      appWith({
        options: {
          onBeforeConnect: () => {
            throw new Error("connect");
          }
        },
        onError: (error) => seen.push(error.message)
      }),
      request("/parties/chat/room", { headers: { Upgrade: "websocket" } })
    );

    routePartykitRequest.mockImplementation(
      async (_request: Request, _env: unknown, routeOptions: AnyOptions) =>
        routeOptions.onBeforeRequest?.(_request, LOBBY, null) ?? null
    );
    let downstreamCalls = 0;
    const withoutHandler = new Elysia();
    const requestError = new Error("request");
    withoutHandler.use(
      partyserverMiddleware({
        options: {
          onBeforeRequest: () => {
            throw requestError;
          }
        }
      })
    );
    withoutHandler.all("/parties/chat/room", () => {
      downstreamCalls += 1;
      return "next";
    });
    let errorSurfaced = false;
    try {
      const response = await handle(withoutHandler, request());
      errorSurfaced = response.status >= 400 || (await response.text()) === "request";
    } catch (error) {
      expect(error).toBe(requestError);
      errorSurfaced = true;
    }

    expect({
      errors: seen,
      withHandler: {
        status: withHandler.status,
        body: await withHandler.text()
      },
      withoutHandler: {
        downstreamCalls,
        errorSurfaced
      }
    }).toEqual({
      errors: ["connect"],
      withHandler: { status: 200, body: "next" },
      withoutHandler: { downstreamCalls: 0, errorSurfaced: true }
    });
  });
});
