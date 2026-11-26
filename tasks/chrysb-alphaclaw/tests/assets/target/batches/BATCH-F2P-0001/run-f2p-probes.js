#!/usr/bin/env node
"use strict";

const http = require("http");
const fs = require("fs");
const net = require("net");
const path = require("path");
const os = require("os");

const kObservationPredicates = {
  "F2P-001": "static_hono",
  "F2P-002": "json_body_hono",
  "F2P-003": "json_body_hono",
  "F2P-004": "json_body_hono",
  "F2P-005": "json_body_hono",
  "F2P-006": "json_body_hono",
  "F2P-007": "raw_body_hono",
  "F2P-008": "static_hono",
  "F2P-009": "sse_hono",
  "F2P-010": "ws_hono",
  "F2P-011": "health_hono",
  "F2P-012": "raw_body_hono",
  "F2P-013": "proxy_hops_hono",
  "F2P-014": "health_hono",
  "F2P-015": "proxy_hops_hono",
  "F2P-016": "json_body_hono",
  "F2P-017": "health_hono",
  "F2P-018": "raw_body_hono",
  "F2P-019": "health_hono",
  "F2P-020": "health_hono",
  "F2P-021": "ws_hono",
  "F2P-022": "json_body_hono",
  "F2P-023": "health_hono",
  "F2P-024": "json_body_hono",
};

const repoRoot = path.resolve(
  process.argv[2] ||
    process.env.MIGRATION_REPO_ROOT ||
    process.env.REPO_ROOT ||
    process.env.MIGRATION_REPO ||
    process.env.REPO ||
    process.cwd(),
);
const outputPath = process.argv[3] || null;

process.env.SETUP_PASSWORD = "probe-setup-password";
process.env.ALPHACLAW_ROOT_DIR =
  process.env.ALPHACLAW_ROOT_DIR ||
  fs.mkdtempSync(path.join(os.tmpdir(), "f2p-probe-"));

const kBodyLimit = 5 * 1024 * 1024;

let honoFetchCalls = 0;

const countHonoFetch = (app) => {
  const originalFetch = app.fetch.bind(app);
  app.fetch = (...args) => {
    honoFetchCalls += 1;
    return originalFetch(...args);
  };
  return app;
};

process.on("uncaughtException", (error) => {
  if (error && (error.code === "EPIPE" || error.code === "ECONNRESET")) return;
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exit(2);
});

const resolveRepoPath = (relativePath) => path.join(repoRoot, relativePath);

const hasRepoPath = (relativePath) => fs.existsSync(resolveRepoPath(relativePath));

const requireRepo = (relativePath) => require(resolveRepoPath(relativePath));

const loadOptional = (relativePath) =>
  hasRepoPath(relativePath) ? requireRepo(relativePath) : null;

const requireExternal = (name) => require(name);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const startServer = async (server) => {
  server.on("connection", (socket) => socket.on("error", () => {}));
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve();
    });
  });
  return server.address().port;
};

const stopServer = async (server) => {
  if (!server || !server.listening) return;
  await new Promise((resolve) => {
    server.close(() => resolve());
    if (typeof server.closeAllConnections === "function") {
      server.closeAllConnections();
    }
    setTimeout(resolve, 150);
  });
};

const httpRequest = ({
  port,
  method = "GET",
  requestPath,
  headers = {},
  body = null,
}) =>
  new Promise((resolve, reject) => {
    const request = http.request(
      {
        host: "127.0.0.1",
        port,
        method,
        path: requestPath,
        headers,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
        response.on("end", () => {
          const buffer = Buffer.concat(chunks);
          resolve({
            status: response.statusCode,
            headers: response.headers,
            buffer,
            text: buffer.toString("utf8"),
          });
        });
      },
    );
    request.on("error", reject);
    if (body) request.write(body);
    request.end();
  });

const targetHonoModules = () =>
  hasRepoPath("lib/server/http/hono-app.js")
    ? requireRepo("lib/server/http/hono-app.js")
    : null;

const createExpressFallback = () => {
  try {
    return requireExternal("express");
  } catch {
    return null;
  }
};

const createThrottleMock = () => ({
  getClientKey: () => "probe-client",
  getOrCreateLoginAttemptState: () => ({ attempts: 0 }),
  evaluateLoginThrottle: () => ({ blocked: false, retryAfterSec: 0 }),
  recordLoginFailure: () => ({ lockMs: 0, locked: false }),
  recordLoginSuccess: () => {},
  cleanupLoginAttemptStates: () => {},
});

const runStaticProbe = async () => {
  const loginPath = resolveRepoPath("lib/public/login.html");
  const cssPath = resolveRepoPath("lib/public/css/theme.css");
  if (!fs.existsSync(loginPath) || !fs.existsSync(cssPath)) {
    return { pass: false, targetTech: false, detail: "public static fixture bytes missing" };
  }
  const loginBytes = fs.readFileSync(loginPath);
  const cssBytes = fs.readFileSync(cssPath);

  const modules = targetHonoModules();
  const staticModule = loadOptional("lib/server/http/static-files.js");
  const express = createExpressFallback();
  let server;
  honoFetchCalls = 0;
  try {
    let app;
    let targetTech = false;
    if (modules && staticModule) {
      app = countHonoFetch(modules.createHonoApp());
      app.use(staticModule.createPublicStaticMiddleware());
      server = modules.createHonoNodeServer(app);
      targetTech = true;
    } else if (express) {
      app = express();
      app.use(express.static(path.join(repoRoot, "lib", "public")));
      server = http.createServer(app);
    } else {
      return { pass: false, targetTech: false, detail: "no HTTP framework available" };
    }

    const port = await startServer(server);
    const loginResponse = await httpRequest({
      port,
      requestPath: "/login.html",
    });
    const cssResponse = await httpRequest({
      port,
      requestPath: "/css/theme.css",
    });

    const loginOk =
      loginResponse.status === 200 &&
      /text\/html/i.test(String(loginResponse.headers["content-type"] || "")) &&
      loginResponse.buffer.equals(loginBytes);
    const cssOk =
      cssResponse.status === 200 &&
      cssResponse.buffer.equals(cssBytes);
    const pass = Boolean(targetTech && honoFetchCalls > 0 && loginOk && cssOk);
    return {
      pass,
      targetTech,
      detail: {
        honoFetchCalls,
        loginStatus: loginResponse.status,
        loginContentType: loginResponse.headers["content-type"] || "",
        loginBytesMatch: loginResponse.buffer.equals(loginBytes),
        cssStatus: cssResponse.status,
        cssBytesMatch: cssResponse.buffer.equals(cssBytes),
      },
    };
  } finally {
    await stopServer(server);
  }
};

const runHealthProbe = async () => {
  const modules = targetHonoModules();
  const pages = loadOptional("lib/server/routes/pages.js");
  const express = createExpressFallback();
  let server;
  honoFetchCalls = 0;
  try {
    if (!pages) {
      return { pass: false, targetTech: false, detail: "page route module missing" };
    }
    let app;
    let targetTech = false;
    if (modules) {
      app = countHonoFetch(modules.createHonoApp());
      pages.registerPageRoutes({
        app,
        requireAuth: (_request, _response, next) => next(),
        isGatewayRunning: async () => true,
      });
      server = modules.createHonoNodeServer(app);
      targetTech = true;
    } else if (express) {
      app = express();
      pages.registerPageRoutes({
        app,
        requireAuth: (_request, _response, next) => next(),
        isGatewayRunning: async () => true,
      });
      server = http.createServer(app);
    } else {
      return { pass: false, targetTech: false, detail: "no HTTP framework available" };
    }

    const port = await startServer(server);
    const response = await httpRequest({ port, requestPath: "/health" });
    let body = null;
    try {
      body = JSON.parse(response.text);
    } catch {}
    const validBody =
      body &&
      ["healthy", "starting"].includes(body.status) &&
      ["running", "starting"].includes(body.gateway);
    const pass = Boolean(
      targetTech && honoFetchCalls > 0 && response.status === 200 && validBody,
    );
    return {
      pass,
      targetTech,
      detail: {
        honoFetchCalls,
        status: response.status,
        body,
      },
    };
  } finally {
    await stopServer(server);
  }
};

const runJsonBodyProbe = async () => {
  const modules = targetHonoModules();
  const bodyLimits = loadOptional("lib/server/http/body-limits.js");
  const authModule = loadOptional("lib/server/routes/auth.js");
  const express = createExpressFallback();
  let server;
  let restoreJsonFactory = null;
  honoFetchCalls = 0;
  try {
    if (!authModule) {
      return { pass: false, targetTech: false, detail: "auth route module missing" };
    }
    let app;
    let targetTech = false;
    let jsonMiddlewareRuns = 0;
    let handlerCalled = false;

    if (modules && bodyLimits) {
      const originalJsonFactory = bodyLimits.createJsonBodyMiddleware;
      bodyLimits.createJsonBodyMiddleware = (options) => {
        const middleware = originalJsonFactory(options);
        return async (context, next) => {
          jsonMiddlewareRuns += 1;
          return middleware(context, next);
        };
      };
      restoreJsonFactory = () => {
        bodyLimits.createJsonBodyMiddleware = originalJsonFactory;
      };
      app = countHonoFetch(modules.createHonoApp());
      app.hono.use(
        "*",
        bodyLimits.createJsonBodyMiddleware({ limitBytes: kBodyLimit }),
      );
      app.put("/api/env", (_request, response) => {
        handlerCalled = true;
        return response.json({ ok: true });
      });
      authModule.registerAuthRoutes({
        app,
        loginThrottle: createThrottleMock(),
      });
      server = modules.createHonoNodeServer(app);
      targetTech = true;
    } else if (express) {
      app = express();
      app.use(express.json({ limit: "5mb" }));
      app.put("/api/env", (_request, response) => {
        handlerCalled = true;
        response.json({ ok: true });
      });
      authModule.registerAuthRoutes({
        app,
        loginThrottle: createThrottleMock(),
      });
      app.use((error, _request, response, _next) => {
        response
          .status(error?.status || error?.statusCode || 500)
          .json({ error: error?.message || "probe error" });
      });
      server = http.createServer(app);
    } else {
      return { pass: false, targetTech: false, detail: "no HTTP framework available" };
    }

    const port = await startServer(server);
    const loginBody = Buffer.from(
      JSON.stringify({ password: process.env.SETUP_PASSWORD }),
      "utf8",
    );
    const loginResponse = await httpRequest({
      port,
      method: "POST",
      requestPath: "/api/auth/login",
      headers: {
        "content-type": "application/json",
        "content-length": String(loginBody.length),
      },
      body: loginBody,
    });

    handlerCalled = false;
    const oversized = Buffer.alloc(kBodyLimit + 1, 0x61);
    const oversizedResponse = await httpRequest({
      port,
      method: "PUT",
      requestPath: "/api/env",
      headers: {
        "content-type": "application/json",
        "content-length": String(oversized.length),
      },
      body: oversized,
    });

    let loginJson = null;
    try {
      loginJson = JSON.parse(loginResponse.text);
    } catch {}

    const pass = Boolean(
      targetTech &&
        honoFetchCalls > 0 &&
        jsonMiddlewareRuns > 0 &&
        loginResponse.status === 200 &&
        loginJson &&
        loginJson.ok === true &&
        oversizedResponse.status === 413 &&
        handlerCalled === false,
    );
    return {
      pass,
      targetTech,
      detail: {
        honoFetchCalls,
        jsonMiddlewareRuns,
        loginStatus: loginResponse.status,
        loginBody: loginJson,
        oversizedStatus: oversizedResponse.status,
        handlerCalled,
      },
    };
  } finally {
    if (restoreJsonFactory) restoreJsonFactory();
    await stopServer(server);
  }
};

const runRawBodyProbe = async () => {
  const modules = targetHonoModules();
  const bodyLimits = loadOptional("lib/server/http/body-limits.js");
  const express = createExpressFallback();
  let server;
  honoFetchCalls = 0;
  try {
    let app;
    let targetTech = false;
    const rawHandler = (_request, response) =>
      response.json({
        bytes: _request.body ? Buffer.from(_request.body).toString("base64") : null,
        length: _request.body ? Buffer.from(_request.body).length : 0,
      });

    if (modules && bodyLimits) {
      app = countHonoFetch(modules.createHonoApp());
      app.hono.use(
        "*",
        bodyLimits.createRawBodyMiddleware({ limitBytes: kBodyLimit }),
      );
      app.post("/webhook/foo", rawHandler);
      server = modules.createHonoNodeServer(app);
      targetTech = true;
    } else if (express) {
      app = express();
      app.use(express.raw({ type: () => true, limit: "5mb" }));
      app.post("/webhook/foo", rawHandler);
      app.use((error, _request, response, _next) => {
        response
          .status(error?.status || error?.statusCode || 500)
          .json({ error: error?.message || "probe error" });
      });
      server = http.createServer(app);
    } else {
      return { pass: false, targetTech: false, detail: "no HTTP framework available" };
    }

    const port = await startServer(server);
    const rawBody = Buffer.from('{"a":1}', "utf8");
    const rawResponse = await httpRequest({
      port,
      method: "POST",
      requestPath: "/webhook/foo",
      headers: {
        "content-type": "application/json",
        "content-length": String(rawBody.length),
      },
      body: rawBody,
    });
    let rawJson = null;
    try {
      rawJson = JSON.parse(rawResponse.text);
    } catch {}

    const oversized = Buffer.alloc(kBodyLimit + 1, 0x61);
    const oversizedResponse = await httpRequest({
      port,
      method: "POST",
      requestPath: "/webhook/foo",
      headers: {
        "content-type": "application/json",
        "content-length": String(oversized.length),
      },
      body: oversized,
    });

    const bytesPreserved = Boolean(
      rawJson && rawJson.bytes === rawBody.toString("base64"),
    );
    const pass = Boolean(
      targetTech &&
        honoFetchCalls > 0 &&
        rawResponse.status === 200 &&
        bytesPreserved &&
        oversizedResponse.status === 413,
    );
    return {
      pass,
      targetTech,
      detail: {
        honoFetchCalls,
        rawStatus: rawResponse.status,
        bytesPreserved,
        oversizedStatus: oversizedResponse.status,
      },
    };
  } finally {
    await stopServer(server);
  }
};

const collectSseFrames = ({ port, timeoutMs = 3500 }) =>
  new Promise((resolve) => {
    let buffer = "";
    let headers = {};
    let finished = false;
    let timer = null;
    let request = null;
    const finish = () => {
      if (finished) return;
      finished = true;
      if (timer) clearTimeout(timer);
      try {
        request?.destroy();
      } catch {}
      resolve({ headers, buffer });
    };
    request = http.get(
      {
        host: "127.0.0.1",
        port,
        path: "/api/events/status",
      },
      (response) => {
        headers = response.headers;
        response.on("data", (chunk) => {
          buffer += String(chunk);
          const events = (buffer.match(/event: status/g) || []).length;
          if (events >= 2) finish();
        });
        response.on("end", finish);
        response.on("error", finish);
      },
    );
    request.on("error", finish);
    timer = setTimeout(finish, timeoutMs);
  });

const runSseProbe = async () => {
  const modules = targetHonoModules();
  const sse = loadOptional("lib/server/http/sse.js");
  const express = createExpressFallback();
  let server;
  honoFetchCalls = 0;
  try {
    let app;
    let targetTech = false;
    const handler = async (request, response) => {
      if (targetTech) {
        const stream = sse.openSseStream(response);
        const writeStatusEvent = () =>
          stream.writeEvent("status", {
            status: { ok: true },
            watchdogStatus: null,
            doctorStatus: null,
            timestamp: new Date().toISOString(),
          });
        await writeStatusEvent();
        const statusIntervalId = setInterval(writeStatusEvent, 2000);
        const keepAliveIntervalId = setInterval(
          () => stream.writeComment("keepalive"),
          15000,
        );
        sse.onStreamAbort(request, () => {
          clearInterval(statusIntervalId);
          clearInterval(keepAliveIntervalId);
          stream.close();
        });
        return;
      }

      response.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        Connection: "keep-alive",
      });
      const writeStatusEvent = () => {
        response.write("event: status\n");
        response.write(
          `data: ${JSON.stringify({
            status: { ok: true },
            watchdogStatus: null,
            doctorStatus: null,
            timestamp: new Date().toISOString(),
          })}\n\n`,
        );
      };
      writeStatusEvent();
      const statusIntervalId = setInterval(writeStatusEvent, 2000);
      const keepAliveIntervalId = setInterval(
        () => response.write(": keepalive\n\n"),
        15000,
      );
      request.on("close", () => {
        clearInterval(statusIntervalId);
        clearInterval(keepAliveIntervalId);
        response.end();
      });
    };

    if (modules && sse) {
      app = countHonoFetch(modules.createHonoApp());
      app.get("/api/events/status", handler);
      server = modules.createHonoNodeServer(app);
      targetTech = true;
    } else if (express) {
      app = express();
      app.get("/api/events/status", handler);
      server = http.createServer(app);
    } else {
      return { pass: false, targetTech: false, detail: "no HTTP framework available" };
    }

    const port = await startServer(server);
    const frames = await collectSseFrames({ port });
    const buffer = frames.buffer;
    const eventCount = (buffer.match(/event: status/g) || []).length;
    const firstEventOk =
      /event: status/.test(buffer) &&
      /"timestamp"/.test(buffer) &&
      /"watchdogStatus"/.test(buffer) &&
      /"doctorStatus"/.test(buffer);
    const headersOk =
      /text\/event-stream/i.test(String(frames.headers["content-type"] || "")) &&
      /no-cache/.test(String(frames.headers["cache-control"] || "")) &&
      String(frames.headers["x-accel-buffering"] || "") === "no";
    const pass = Boolean(
      targetTech &&
        honoFetchCalls > 0 &&
        eventCount >= 2 &&
        firstEventOk &&
        headersOk,
    );
    return {
      pass,
      targetTech,
      detail: {
        honoFetchCalls,
        eventCount,
        firstEventOk,
        headersOk,
        contentType: frames.headers["content-type"] || "",
        cacheControl: frames.headers["cache-control"] || "",
        xAccelBuffering: frames.headers["x-accel-buffering"] || "",
      },
    };
  } finally {
    await stopServer(server);
  }
};

const openWebSocket = ({ port, headers = {} }) =>
  new Promise((resolve) => {
    const { WebSocket } = requireExternal("ws");
    const socket = new WebSocket(`ws://127.0.0.1:${port}/api/ws/chat`, {
      headers,
    });
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        socket.terminate();
      } catch {}
      resolve({ opened: false, status: null, timeout: true });
    }, 2500);
    socket.on("open", () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ opened: true, status: 101, socket });
    });
    socket.on("unexpected-response", (_request, response) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const status = response.statusCode;
      response.resume();
      resolve({ opened: false, status });
    });
    socket.on("error", () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ opened: false, status: null });
    });
  });

const runWsProbe = async () => {
  const modules = targetHonoModules();
  const bridgeModule = loadOptional("lib/server/watchdog-terminal-ws.js");
  const express = createExpressFallback();
  let server;
  let wss = null;
  honoFetchCalls = 0;
  try {
    let targetTech = false;
    const isAuthorizedRequest = (request) =>
      String(request.headers?.cookie || "").includes("setup_token=probe-valid");

    if (modules && bridgeModule) {
      const app = countHonoFetch(modules.createHonoApp());
      app.get("/probe/ping", (_request, response) =>
        response.json({ ok: true }),
      );
      server = modules.createHonoNodeServer(app);
      const { WebSocketServer } = requireExternal("ws");
      wss = new WebSocketServer({ noServer: true });
      wss.on("connection", (socket) => {
        socket.on("message", (message) => socket.send(`echo:${String(message)}`));
      });
      const chatWsService = {
        handleUpgrade(request, socket, head) {
          wss.handleUpgrade(request, socket, head, (client) => {
            wss.emit("connection", client, request);
          });
        },
      };
      bridgeModule.createWatchdogTerminalWsBridge({
        server,
        proxy: { ws() {} },
        getGatewayUrl: () => "http://127.0.0.1:1",
        isAuthorizedRequest,
        watchdogTerminal: {
          createOrReuseSession: () => ({ id: "probe-session" }),
          subscribe: () => ({ ok: true, unsubscribe() {} }),
          writeInput() {},
        },
        chatWsService,
      });
      targetTech = true;
    } else if (express) {
      const app = express();
      server = http.createServer(app);
      server.on("upgrade", (_request, socket) => {
        socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
      });
    } else {
      return { pass: false, targetTech: false, detail: "no HTTP framework available" };
    }

    const port = await startServer(server);
    await httpRequest({ port, requestPath: "/probe/ping" });
    const unauthorized = await openWebSocket({ port });
    if (unauthorized.socket) {
      try {
        unauthorized.socket.close();
      } catch {}
    }
    const authorized = await openWebSocket({
      port,
      headers: { cookie: "setup_token=probe-valid" },
    });
    let echoed = false;
    if (authorized.opened && authorized.socket) {
      echoed = await new Promise((resolve) => {
        const timer = setTimeout(() => resolve(false), 2000);
        authorized.socket.on("message", (message) => {
          clearTimeout(timer);
          resolve(String(message) === "echo:probe-message");
        });
        authorized.socket.send("probe-message");
      });
      try {
        authorized.socket.close();
      } catch {}
    }

    const pass = Boolean(
      targetTech &&
        honoFetchCalls > 0 &&
        unauthorized.status === 401 &&
        authorized.opened &&
        echoed,
    );
    return {
      pass,
      targetTech,
      detail: {
        honoFetchCalls,
        unauthorizedStatus: unauthorized.status,
        authorizedOpened: authorized.opened,
        echoed,
      },
    };
  } finally {
    if (wss) {
      await new Promise((resolve) => wss.close(() => resolve()));
    }
    await stopServer(server);
  }
};

const runProxyHopsProbe = async () => {
  const modules = targetHonoModules();
  const clientIp = loadOptional("lib/server/http/client-ip.js");
  const express = createExpressFallback();
  let server;
  honoFetchCalls = 0;
  try {
    let targetTech = false;
    let port;
    let resultOne;
    let resultZero;

    if (modules && clientIp) {
      const app = countHonoFetch(modules.createHonoApp());
      app.get("/probe/ip1", (request, response) =>
        response.json({
          ip: clientIp.resolveClientIpFromContext(request.__context, 1),
        }),
      );
      app.get("/probe/ip0", (request, response) =>
        response.json({
          ip: clientIp.resolveClientIpFromContext(request.__context, 0),
        }),
      );
      server = modules.createHonoNodeServer(app);
      port = await startServer(server);
      resultOne = JSON.parse(
        (await httpRequest({
          port,
          requestPath: "/probe/ip1",
          headers: { "x-forwarded-for": "198.51.100.7" },
        })).text,
      );
      resultZero = JSON.parse(
        (await httpRequest({
          port,
          requestPath: "/probe/ip0",
          headers: { "x-forwarded-for": "198.51.100.7" },
        })).text,
      );
      targetTech = true;
    } else if (express) {
      const app = express();
      app.get("/probe/ip", (request, response) =>
        response.json({ ip: request.ip }),
      );
      server = http.createServer(app);
      port = await startServer(server);
      app.set("trust proxy", 1);
      resultOne = JSON.parse(
        (await httpRequest({
          port,
          requestPath: "/probe/ip",
          headers: { "x-forwarded-for": "198.51.100.7" },
        })).text,
      );
      app.set("trust proxy", 0);
      resultZero = JSON.parse(
        (await httpRequest({
          port,
          requestPath: "/probe/ip",
          headers: { "x-forwarded-for": "198.51.100.7" },
        })).text,
      );
    } else {
      return { pass: false, targetTech: false, detail: "no HTTP framework available" };
    }

    const pass = Boolean(
      targetTech &&
        honoFetchCalls > 0 &&
        resultOne?.ip === "198.51.100.7" &&
        resultZero?.ip === "127.0.0.1",
    );
    return {
      pass,
      targetTech,
      detail: {
        honoFetchCalls,
        trustProxyOne: resultOne?.ip || null,
        trustProxyZero: resultZero?.ip || null,
      },
    };
  } finally {
    await stopServer(server);
  }
};

const liveSessionFor = async () => {
  const live = require(path.resolve(__dirname, "../../../shared/integration/live-server.js"));
  if (!live.usesTargetFramework(repoRoot)) return null;
  const session = await live.sharedLiveSession(repoRoot, {
    setupPassword: "probe-setup-password",
    openaiCompatEnabled: true,
  });
  return { live, ...session };
};

const runLivePredicate = async (name) => {
  const session = await liveSessionFor();
  if (!session) return null;
  const { live, gateway, server } = session;
  const call = (options) => live.httpCall(server.baseUrl, options);
  try {
    const login = await call({
      method: "POST",
      requestPath: "/api/auth/login",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: "probe-setup-password" }),
    });
    const cookie = login.cookie;
    const checks = {
      static_hono: async () => {
        const page = await call({ requestPath: "/login.html" });
        const stylesheet = await call({ requestPath: "/css/theme.css" });
        return (
          page.status === 200 &&
          /text\/html/i.test(page.headers["content-type"] || "") &&
          stylesheet.status === 200
        );
      },
      health_hono: async () => {
        const response = await call({ requestPath: "/health" });
        return (
          response.status === 200 &&
          ["healthy", "starting"].includes(response.json?.status) &&
          ["running", "starting"].includes(response.json?.gateway)
        );
      },
      json_body_hono: async () => {
        const setCookie = login.headers["set-cookie"] || "";
        const oversized = "a".repeat(kBodyLimit + 1);
        const rejected = await call({
          method: "PUT",
          requestPath: "/api/env",
          headers: { "content-type": "application/json", cookie },
          body: oversized,
        });
        return (
          login.status === 200 &&
          login.json?.ok === true &&
          /httponly/i.test(setCookie) &&
          /samesite=lax/i.test(setCookie) &&
          rejected.status === 413
        );
      },
      raw_body_hono: async () => {
        const seen = gateway.calls.length;
        const body = JSON.stringify({ token: "body-token", from: "body" });
        const response = await call({
          method: "POST",
          requestPath: "/webhook/foo?token=query-token&from=query",
          headers: { "content-type": "application/json" },
          body,
        });
        const forwarded = gateway.calls[seen];
        let forwardedBody = null;
        try {
          forwardedBody = JSON.parse(forwarded?.bodyText || "");
        } catch {}
        return (
          response.status === 200 &&
          forwarded?.method === "POST" &&
          String(forwarded?.url || "").startsWith("/hooks/foo") &&
          !String(forwarded?.url || "").includes("token=") &&
          forwarded?.headers.authorization === "Bearer query-token" &&
          forwardedBody?.from === "body" &&
          forwardedBody?.token === "body-token"
        );
      },
      sse_hono: async () => {
        const response = await call({
          requestPath: "/api/events/status",
          headers: { cookie },
        });
        return (
          response.status === 200 &&
          /text\/event-stream/i.test(response.headers["content-type"] || "") &&
          /no-cache/.test(response.headers["cache-control"] || "") &&
          response.headers["x-accel-buffering"] === "no" &&
          /event:\s*status/.test(response.text) &&
          /"watchdogStatus"/.test(response.text) &&
          /"doctorStatus"/.test(response.text)
        );
      },
      ws_hono: async () => {
        const { WebSocket } = require(path.join(repoRoot, "node_modules", "ws"));
        const upgrade = (requestPath, headers = {}) =>
          new Promise((resolve) => {
            const socket = new WebSocket(
              `ws://127.0.0.1:${server.port}${requestPath}`,
              { headers },
            );
            const timer = setTimeout(() => {
              socket.terminate();
              resolve(0);
            }, 4000);
            socket.on("open", () => {
              clearTimeout(timer);
              socket.close();
              resolve(101);
            });
            socket.on("unexpected-response", (_request, response) => {
              clearTimeout(timer);
              response.resume();
              resolve(response.statusCode);
            });
            socket.on("error", () => {
              clearTimeout(timer);
              resolve(0);
            });
          });
        const paths = [
          "/api/ws/chat",
          "/api/watchdog/terminal/ws",
          "/openclaw/x",
        ];
        const rejected = [];
        for (const requestPath of paths) {
          rejected.push((await upgrade(requestPath)) === 401);
        }
        const accepted = (await upgrade("/api/ws/chat", { cookie })) === 101;
        return rejected.every(Boolean) && accepted;
      },
      proxy_hops_hono: async () => {
        const throttled = [];
        for (let attempt = 0; attempt < 12 && throttled.length === 0; attempt += 1) {
          const response = await call({
            method: "POST",
            requestPath: "/api/auth/login",
            headers: {
              "content-type": "application/json",
              "x-forwarded-for": "198.51.100.7, 203.0.113.9",
            },
            body: JSON.stringify({ password: "wrong-password" }),
          });
          if (response.status === 429) throttled.push(response);
        }
        const blocked = throttled[0];
        return (
          blocked?.status === 429 &&
          Number(blocked.headers["retry-after"]) > 0 &&
          Number(blocked.json?.retryAfterSec) > 0
        );
      },
    };
    const pass = checks[name] ? await checks[name]() : false;
    return { pass, targetTech: true, detail: { live: true, predicate: name } };
  } catch (error) {
    return { pass: false, targetTech: true, error: String(error && error.message ? error.message : error) };
  }
};

const runProbe = async (name) => {
  try {
    const liveResult = await runLivePredicate(name);
    if (liveResult) return liveResult;
  } catch (error) {
    return { pass: false, targetTech: true, error: String(error && error.stack ? error.stack : error) };
  }
  const probes = {
    static_hono: runStaticProbe,
    health_hono: runHealthProbe,
    json_body_hono: runJsonBodyProbe,
    raw_body_hono: runRawBodyProbe,
    sse_hono: runSseProbe,
    ws_hono: runWsProbe,
    proxy_hops_hono: runProxyHopsProbe,
  };
  const probe = probes[name];
  if (!probe) {
    return { pass: false, targetTech: false, error: `unknown predicate ${name}` };
  }
  try {
    return await probe();
  } catch (error) {
    return {
      pass: false,
      targetTech: false,
      error: String(error && error.stack ? error.stack : error),
    };
  }
};

const main = async () => {
  const predicateNames = Array.from(new Set(Object.values(kObservationPredicates)));
  const liveSession = await liveSessionFor().catch(() => null);
  const settled = await Promise.all(predicateNames.map(async (name) => [name, await runProbe(name)]));
  const predicates = Object.fromEntries(settled);
  const observations = Object.entries(kObservationPredicates).map(
    ([observationId, predicate]) => {
      const result = predicates[predicate] || {
        pass: false,
        targetTech: false,
        error: "predicate result missing",
      };
      return {
        observation_id: observationId,
        predicate,
        status: result.pass ? "PASS" : "FAIL",
        target_technology_used: Boolean(result.targetTech),
        detail: result.detail || null,
        error: result.error || null,
      };
    },
  );
  const output = {
    schema_version: "migration-asset-f2p-probe-results:v1",
    batch_id: "BATCH-F2P-0001",
    repo_root: repoRoot,
    target_technology: {
      name: "Hono",
      version: "4.13.7",
    },
    source_technology: {
      name: "Express",
      version: "5.2.1",
    },
    generated_at: new Date().toISOString(),
    predicates,
    observations,
  };
  const text = `${JSON.stringify(output, null, 2)}\n`;
  if (outputPath) {
    fs.mkdirSync(path.dirname(outputPath), { recursive: true });
    fs.writeFileSync(outputPath, text);
  }
  if (liveSession) {
    await liveSession.server.stop();
    await liveSession.gateway.close();
  }
  process.stdout.write(text, () => process.exit(0));
};

main();
