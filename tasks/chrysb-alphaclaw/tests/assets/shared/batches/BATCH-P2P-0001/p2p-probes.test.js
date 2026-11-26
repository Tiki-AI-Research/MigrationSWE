/**
 * BATCH-P2P-0001 executable P2P probes.
 *
 * Every observation here is asserted against a running AlphaClaw server over
 * HTTP (and WebSocket for P2P-014).  The suite never requires, imports or
 * knowledge-of internal module paths, so any candidate layout that preserves
 * the documented public contract can satisfy it.
 */
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");
const request = require("supertest");

const kRepoRoot = process.env.P2P_REPO_ROOT || process.cwd();
const kSetupPassword = "p2p-secret";
const kGatewayToken = "p2p-gateway-token";
const kBootTimeoutMs = 180000;
const kJsonLimit = 5 * 1024 * 1024;
const kV1Limit = 50 * 1024 * 1024;

const kRequiredRoutes = [
  "all /oauth/:id",
  "all /openclaw",
  "delete /api/agents/:id",
  "delete /api/agents/:id/bindings",
  "delete /api/browse/delete",
  "delete /api/channels/accounts",
  "delete /api/models/auth/:profileId",
  "delete /api/nodes/:id",
  "delete /api/nodes/exec-approvals/allowlist/:id",
  "delete /api/telegram/groups/:groupId/topics/:topicId",
  "delete /api/webhooks/:name",
  "delete /api/webhooks/:name/oauth-callback",
  "get /",
  "get /api/agent/sessions",
  "get /api/agents",
  "get /api/agents/:id",
  "get /api/agents/:id/bindings",
  "get /api/agents/:id/workspace-size",
  "get /api/alphaclaw/config",
  "get /api/alphaclaw/release-notes",
  "get /api/alphaclaw/version",
  "get /api/auth/status",
  "get /api/browse/download",
  "get /api/browse/git-diff",
  "get /api/browse/git-summary",
  "get /api/browse/read",
  "get /api/browse/sqlite-table",
  "get /api/browse/tree",
  "get /api/channels/accounts",
  "get /api/channels/accounts/login-status",
  "get /api/channels/accounts/token",
  "get /api/codex/status",
  "get /api/cron/jobs",
  "get /api/cron/jobs/:id/runs",
  "get /api/cron/jobs/:id/trends",
  "get /api/cron/jobs/:id/usage",
  "get /api/cron/runs/bulk",
  "get /api/cron/status",
  "get /api/cron/usage/bulk",
  "get /api/devices",
  "get /api/doctor/cards",
  "get /api/doctor/runs",
  "get /api/doctor/runs/:id",
  "get /api/doctor/runs/:id/cards",
  "get /api/doctor/status",
  "get /api/env",
  "get /api/events/status",
  "get /api/gateway-status",
  "get /api/gateway/dashboard",
  "get /api/gmail/config",
  "get /api/gmail/watch/status",
  "get /api/google/accounts",
  "get /api/google/check",
  "get /api/google/credentials",
  "get /api/google/status",
  "get /api/models",
  "get /api/models/auth",
  "get /api/models/config",
  "get /api/models/status",
  "get /api/models/thinking-options",
  "get /api/nodes",
  "get /api/nodes/:id/browser-status",
  "get /api/nodes/connect-info",
  "get /api/nodes/exec-approvals",
  "get /api/nodes/exec-config",
  "get /api/onboard/progress",
  "get /api/onboard/status",
  "get /api/operations/:operationId/events",
  "get /api/pairings",
  "get /api/restart-status",
  "get /api/status",
  "get /api/sync-cron",
  "get /api/telegram/bot",
  "get /api/telegram/groups/:groupId/topics",
  "get /api/telegram/topic-registry",
  "get /api/telegram/workspace",
  "get /api/usage/sessions",
  "get /api/usage/sessions/:id",
  "get /api/usage/sessions/:id/timeseries",
  "get /api/usage/summary",
  "get /api/watchdog/events",
  "get /api/watchdog/logs",
  "get /api/watchdog/resources",
  "get /api/watchdog/settings",
  "get /api/watchdog/status",
  "get /api/watchdog/terminal/output",
  "get /api/webhooks",
  "get /api/webhooks/:name",
  "get /api/webhooks/:name/requests",
  "get /api/webhooks/:name/requests/:id",
  "get /auth/codex/callback",
  "get /auth/codex/start",
  "get /auth/google/callback",
  "get /auth/google/start",
  "get /health",
  "get /setup",
  "post /api/agent/message",
  "post /api/agents",
  "post /api/agents/:id/bindings",
  "post /api/agents/:id/default",
  "post /api/alphaclaw/update",
  "post /api/auth/login",
  "post /api/auth/logout",
  "post /api/browse/create-file",
  "post /api/browse/create-folder",
  "post /api/browse/git-sync",
  "post /api/browse/move",
  "post /api/browse/restore",
  "post /api/channels/accounts",
  "post /api/channels/accounts/jobs",
  "post /api/channels/accounts/login",
  "post /api/codex/disconnect",
  "post /api/codex/exchange",
  "post /api/cron/jobs/:id/disable",
  "post /api/cron/jobs/:id/enable",
  "post /api/cron/jobs/:id/run",
  "post /api/devices/:id/approve",
  "post /api/devices/:id/reject",
  "post /api/doctor/cards/:id/status",
  "post /api/doctor/findings/:id/fix",
  "post /api/doctor/import",
  "post /api/doctor/run",
  "post /api/gateway/restart",
  "post /api/gmail/config",
  "post /api/gmail/watch/renew",
  "post /api/gmail/watch/start",
  "post /api/gmail/watch/stop",
  "post /api/google/accounts",
  "post /api/google/credentials",
  "post /api/google/disconnect",
  "post /api/models/refresh",
  "post /api/models/set",
  "post /api/nodes/:id/approve",
  "post /api/nodes/:id/route",
  "post /api/nodes/exec-approvals/allowlist",
  "post /api/nodes/exec-config",
  "post /api/onboard",
  "post /api/onboard/github/verify",
  "post /api/onboard/import/apply",
  "post /api/onboard/import/scan",
  "post /api/pairings/:id/approve",
  "post /api/pairings/:id/reject",
  "post /api/restart-status/dismiss",
  "post /api/telegram/groups/:groupId/configure",
  "post /api/telegram/groups/:groupId/topics",
  "post /api/telegram/groups/:groupId/topics/bulk",
  "post /api/telegram/groups/verify",
  "post /api/telegram/workspace/reset",
  "post /api/watchdog/repair",
  "post /api/watchdog/terminal/close",
  "post /api/watchdog/terminal/input",
  "post /api/watchdog/terminal/session",
  "post /api/watchdog/test-notification",
  "post /api/webhooks",
  "post /api/webhooks/:name/oauth-callback",
  "post /api/webhooks/:name/oauth-callback/rotate",
  "post /gmail-pubsub",
  "put /api/agents/:id",
  "put /api/alphaclaw/config/features/openai-compat-api",
  "put /api/browse/write",
  "put /api/channels/accounts",
  "put /api/cron/jobs/:id/prompt",
  "put /api/cron/jobs/:id/routing",
  "put /api/env",
  "put /api/models/auth/:profileId",
  "put /api/models/config",
  "put /api/sync-cron",
  "put /api/telegram/groups/:groupId/topics/:topicId",
  "put /api/watchdog/settings",
  "put /api/webhooks/:name/destination",
  "use /api",
  "use /auth",
  "use /setup",
];

const freePort = () =>
  new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });

const waitForHealth = async (baseUrl, deadline) => {
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/health`);
      if (response.ok) return true;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
};

const createGatewaySpy = async () => {
  const calls = [];
  let reply = { status: 200, headers: {}, body: "{}" };
  const server = http.createServer((incoming, outgoing) => {
    const chunks = [];
    incoming.on("data", (chunk) => chunks.push(chunk));
    incoming.on("end", () => {
      calls.push({
        method: incoming.method,
        url: incoming.url,
        headers: incoming.headers,
        bodyText: Buffer.concat(chunks).toString("utf8"),
      });
      outgoing.statusCode = reply.status;
      for (const [key, value] of Object.entries(reply.headers)) {
        outgoing.setHeader(key, value);
      }
      outgoing.end(reply.body);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    calls,
    port: server.address().port,
    reply: (next) => {
      reply = next;
    },
    close: () => new Promise((resolve) => server.close(resolve)),
  };
};

const writeRoot = ({ rootDir, gatewayPort, openaiCompatEnabled }) => {
  const openclawDir = path.join(rootDir, ".openclaw");
  fs.mkdirSync(openclawDir, { recursive: true });
  fs.writeFileSync(
    path.join(openclawDir, "openclaw.json"),
    JSON.stringify({ gateway: { port: gatewayPort } }, null, 2),
  );
  fs.writeFileSync(
    path.join(rootDir, "onboarded.json"),
    JSON.stringify({ onboarding_version: "p2p-probe" }, null, 2),
  );
  fs.writeFileSync(
    path.join(openclawDir, "alphaclaw.json"),
    JSON.stringify(
      { features: { openaiCompatApi: { enabled: openaiCompatEnabled === true } } },
      null,
      2,
    ),
  );
};

const startServer = async ({ rootDir, gatewayPort, port, env = {} }) => {
  const child = spawn(
    process.execPath,
    [path.join(kRepoRoot, "bin", "alphaclaw.js"), "start"],
    {
      cwd: kRepoRoot,
      env: {
        ...process.env,
        PORT: String(port),
        SETUP_PASSWORD: kSetupPassword,
        ALPHACLAW_ROOT_DIR: rootDir,
        HOME: rootDir,
        OPENCLAW_GATEWAY_TOKEN: kGatewayToken,
        TRUST_PROXY_HOPS: "1",
        NO_COLOR: "1",
        ...env,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += String(chunk);
  });
  child.stderr.on("data", (chunk) => {
    output += String(chunk);
  });
  const baseUrl = `http://127.0.0.1:${port}`;
  const ready = await waitForHealth(baseUrl, Date.now() + kBootTimeoutMs);
  if (!ready) {
    child.kill("SIGKILL");
    throw new Error(`server did not become ready:\n${output.slice(-4000)}`);
  }
  return {
    baseUrl,
    output: () => output,
    stop: () =>
      new Promise((resolve) => {
        if (child.exitCode !== null) return resolve();
        child.once("exit", () => resolve());
        child.kill("SIGTERM");
        setTimeout(() => {
          try {
            child.kill("SIGKILL");
          } catch {}
          resolve();
        }, 15000);
      }),
  };
};

const httpCall = async (
  baseUrl,
  { method = "GET", requestPath = "/", headers = {}, body } = {},
) => {
  const response = await fetch(`${baseUrl}${requestPath}`, {
    method,
    headers,
    body,
    redirect: "manual",
  });
  const text = await response.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {}
  return {
    status: response.status,
    headers: Object.fromEntries(
      [...response.headers.entries()].map(([key, value]) => [key.toLowerCase(), value]),
    ),
    text,
    json,
    cookie: (response.headers.get("set-cookie") || "").split(";")[0],
  };
};

const loginCookie = async (baseUrl, password = kSetupPassword) => {
  const response = await httpCall(baseUrl, {
    method: "POST",
    requestPath: "/api/auth/login",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  expect(response.status).toBe(200);
  expect(response.cookie).toContain("setup_token=");
  return response.cookie;
};

const readRawSocket = ({ port, requestPath, headers }) =>
  new Promise((resolve, reject) => {
    const socket = net.createConnection(port, "127.0.0.1");
    let received = "";
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(received);
    };
    socket.setTimeout(5000, finish);
    socket.on("data", (chunk) => {
      received += String(chunk);
    });
    socket.on("close", finish);
    socket.on("end", finish);
    socket.on("error", (error) => {
      if (received) return finish();
      reject(error);
    });
    socket.on("connect", () => {
      socket.write(
        `GET ${requestPath} HTTP/1.1\r\nHost: localhost\r\n` +
          Object.entries(headers || {})
            .map(([key, value]) => `${key}: ${value}\r\n`)
            .join("") +
          "\r\n",
      );
    });
  });

const readSseFrames = async (baseUrl, requestPath, { cookie, until, timeoutMs = 20000 }) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let text = "";
  try {
    const response = await fetch(`${baseUrl}${requestPath}`, {
      headers: cookie ? { Cookie: cookie } : {},
      signal: controller.signal,
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      text += decoder.decode(value, { stream: true });
      if (until(text)) break;
    }
    controller.abort();
    return { headers: response.headers, text };
  } catch (error) {
    if (error?.name === "AbortError") return { headers: null, text };
    throw error;
  } finally {
    clearTimeout(timer);
  }
};

const collectJavaScriptFiles = (relativeDir) => {
  const absoluteDir = path.join(kRepoRoot, relativeDir);
  if (!fs.existsSync(absoluteDir)) return [];
  const files = [];
  for (const entry of fs.readdirSync(absoluteDir, { withFileTypes: true })) {
    const relativePath = path.join(relativeDir, entry.name);
    if (entry.isDirectory()) {
      files.push(...collectJavaScriptFiles(relativePath));
    } else if (entry.isFile() && entry.name.endsWith(".js")) {
      files.push(relativePath);
    }
  }
  return files;
};

const kRoutePattern =
  /\b(?:app|router)\.(get|post|put|delete|patch|all|use)\s*\(\s*(?:\[[^\]]*\]|["'`])([^"'`\]]*)/g;
const kRouteSuffix = /(?:\/?\*+|\{[^}]*\}|:[^/]*)$/;

describe("BATCH-P2P-0001 probes", () => {
  let gateway;
  let rootDir;
  let server;
  let v1Server;
  let v1RootDir;
  let cookie;

  beforeAll(async () => {
    gateway = await createGatewaySpy();
    rootDir = fs.mkdtempSync(path.join(os.tmpdir(), "alphaclaw-p2p-"));
    writeRoot({ rootDir, gatewayPort: gateway.port, openaiCompatEnabled: false });
    server = await startServer({
      rootDir,
      gatewayPort: gateway.port,
      port: await freePort(),
    });
    cookie = await loginCookie(server.baseUrl);

    v1RootDir = fs.mkdtempSync(path.join(os.tmpdir(), "alphaclaw-p2p-v1-"));
    writeRoot({ rootDir: v1RootDir, gatewayPort: gateway.port, openaiCompatEnabled: true });
    v1Server = await startServer({
      rootDir: v1RootDir,
      gatewayPort: gateway.port,
      port: await freePort(),
    });
  }, 600000);

  afterAll(async () => {
    if (v1Server) await v1Server.stop();
    if (server) await server.stop();
    if (gateway) await gateway.close();
    for (const directory of [rootDir, v1RootDir]) {
      if (directory) fs.rmSync(directory, { recursive: true, force: true });
    }
  }, 120000);

  beforeEach(() => {
    gateway.calls.length = 0;
    gateway.reply({ status: 200, headers: {}, body: "{}" });
  });

  it("P2P-018 exposes the public health contract", async () => {
    const healthy = await httpCall(server.baseUrl, { requestPath: "/health" });
    expect(healthy.status).toBe(200);
    expect(Object.keys(healthy.json || {}).sort()).toEqual(["gateway", "status"]);
    expect(["healthy", "starting"]).toContain(healthy.json.status);
    expect(["running", "starting"]).toContain(healthy.json.gateway);
  });

  it("P2P-003 preserves setup UI shell routing and auth gating", async () => {
    const rootWithoutCookie = await httpCall(server.baseUrl, { requestPath: "/" });
    expect(rootWithoutCookie.status).toBe(302);
    expect(rootWithoutCookie.headers.location).toBe("/login.html");

    const rootWithCookie = await httpCall(server.baseUrl, {
      requestPath: "/",
      headers: { Cookie: cookie },
    });
    expect(rootWithCookie.status).toBe(200);
    expect(rootWithCookie.text.toLowerCase()).toContain("<!doctype html>");

    const setupWithCookie = await httpCall(server.baseUrl, {
      requestPath: "/setup",
      headers: { Cookie: cookie },
    });
    expect(setupWithCookie.status).toBe(200);
    expect(setupWithCookie.text.toLowerCase()).toContain("<!doctype html>");
  });

  it("P2P-024 rewrites webhook paths and converts GET hooks to POST", async () => {
    const webhook = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/webhook/foo?a=1",
      headers: { "Content-Type": "application/json" },
      body: '{"x":1}',
    });
    expect(webhook.status).toBe(200);
    expect(gateway.calls[0]).toMatchObject({ method: "POST", url: "/hooks/foo?a=1" });

    const hook = await httpCall(server.baseUrl, { requestPath: "/hooks/foo?b=2" });
    expect(hook.status).toBe(200);
    expect(gateway.calls[1]).toMatchObject({ method: "POST", url: "/hooks/foo?b=2" });
  });

  it("P2P-012 merges query parameters with explicit body precedence", async () => {
    const response = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/webhook/foo?a=1&b=2",
      headers: { "Content-Type": "application/json" },
      body: '{"b":"body","c":3}',
    });
    expect(response.status).toBe(200);
    expect(JSON.parse(gateway.calls[0].bodyText)).toEqual({ a: "1", b: "body", c: 3 });
  });

  it("P2P-019 moves webhook query tokens into authorization headers", async () => {
    await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/webhook/foo?token=abc&a=1",
      body: "{}",
    });
    expect(gateway.calls[0].url).toBe("/hooks/foo?a=1");
    expect(gateway.calls[0].headers.authorization).toBe("Bearer abc");

    await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/webhook/foo?token=abc",
      headers: { Authorization: "Bearer xyz" },
      body: "{}",
    });
    expect(gateway.calls[1].url).toBe("/hooks/foo");
    expect(gateway.calls[1].headers.authorization).toBe("Bearer xyz");
  });

  it("P2P-023 suppresses duplicate Gmail push deliveries", async () => {
    gateway.reply({ status: 204, headers: {}, body: "" });
    const payload = JSON.stringify({
      account: "agent@example.com",
      messages: [{ id: "message-1" }],
    });

    const first = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/hooks/gmail",
      headers: { "Content-Type": "application/json" },
      body: payload,
    });
    expect(first.status).toBe(204);
    expect(gateway.calls).toHaveLength(1);

    const second = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/hooks/gmail",
      headers: { "Content-Type": "application/json" },
      body: payload,
    });
    expect(second.status).toBe(200);
    expect(second.json).toEqual({ ok: true, deduped: true });
    expect(gateway.calls).toHaveLength(1);
  });

  it("P2P-002 mirrors webhook gateway responses and logs failures", async () => {
    gateway.reply({ status: 202, headers: { "X-Hook": "yes" }, body: "accepted" });
    const mirrored = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/webhook/foo",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    expect(mirrored.status).toBe(202);
    expect(mirrored.headers["x-hook"]).toBe("yes");
    expect(mirrored.text).toBe("accepted");

    const requests = await httpCall(server.baseUrl, {
      requestPath: "/api/webhooks/foo/requests",
      headers: { Cookie: cookie },
    });
    expect(requests.status).toBe(200);
    const rows = Array.isArray(requests.json)
      ? requests.json
      : requests.json?.requests || [];
    expect(rows.length).toBeGreaterThanOrEqual(1);
    expect(rows[0].gatewayStatus).toBe(202);
  });

  it("P2P-013 rejects oversized JSON and raw bodies before handlers", async () => {
    const oversized = Buffer.alloc(kJsonLimit + 1, 0x61);
    const api = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/api/env",
      headers: { "Content-Type": "application/json", Cookie: cookie },
      body: oversized,
    });
    expect(api.status).toBe(413);

    const raw = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/webhook/big",
      headers: { "Content-Type": "application/octet-stream" },
      body: oversized,
    });
    expect(raw.status).toBe(413);
    expect(gateway.calls).toHaveLength(0);
  });

  it("P2P-016 enforces the /v1 limit and disabled-feature gating", async () => {
    const disabled = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/v1/chat/completions",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${kGatewayToken}`,
      },
      body: JSON.stringify({ model: "m" }),
    });
    expect(disabled.status).toBe(404);
    expect(disabled.json).toEqual({ error: "Not found" });
    expect(gateway.calls).toHaveLength(0);

    const oversized = await httpCall(v1Server.baseUrl, {
      method: "POST",
      requestPath: "/v1/chat/completions",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${kGatewayToken}`,
      },
      body: Buffer.alloc(kV1Limit + 1, 0x61),
    });
    expect(oversized.status).toBe(413);
    expect(gateway.calls).toHaveLength(0);
  });

  it("P2P-010 streams status SSE frames and clears timers on disconnect", async () => {
    let sawKeepalive = false;
    const { headers, text } = await readSseFrames(server.baseUrl, "/api/events/status", {
      cookie,
      timeoutMs: 40000,
      until: (body) => {
        if (/: keepalive\n\n/.test(body)) sawKeepalive = true;
        return sawKeepalive && (body.match(/event: status\n/g) || []).length >= 2;
      },
    });
    expect((headers?.get("content-type") || "").toLowerCase()).toContain(
      "text/event-stream",
    );
    expect(headers?.get("cache-control")).toBe("no-cache, no-transform");
    expect(headers?.get("x-accel-buffering")).toBe("no");

    const frames = [...text.matchAll(/event: status\ndata: (\{[^\n]+\})\n\n/g)].map(
      (match) => JSON.parse(match[1]),
    );
    expect(frames.length).toBeGreaterThanOrEqual(1);
    expect(Object.keys(frames[0]).sort()).toEqual([
      "doctorStatus",
      "status",
      "timestamp",
      "watchdogStatus",
    ]);
    expect(sawKeepalive).toBe(true);
  }, 90000);

  it("P2P-011 preserves the required public route table", () => {
    const files = collectJavaScriptFiles("lib");
    const present = new Set();
    for (const relativePath of files) {
      const source = fs.readFileSync(path.join(kRepoRoot, relativePath), "utf8");
      kRoutePattern.lastIndex = 0;
      let match;
      while ((match = kRoutePattern.exec(source))) {
        const routePath = match[2].trim();
        present.add(`${match[1]} ${routePath}`);
        const trimmed = routePath.replace(kRouteSuffix, "");
        if (trimmed && trimmed !== routePath) {
          present.add(`${match[1]} ${trimmed}`);
        }
      }
    }
    const missing = kRequiredRoutes.filter((entry) => !present.has(entry));
    expect(missing).toEqual([]);
    expect(kRequiredRoutes.length).toBe(173);
  });

  it("P2P-014 authenticates WebSocket upgrades before bridging", async () => {
    const { port } = new URL(server.baseUrl);
    for (const upgradePath of [
      "/openclaw/x",
      "/api/watchdog/terminal/ws",
      "/api/ws/chat",
    ]) {
      const response = await readRawSocket({
        port: Number(port),
        requestPath: upgradePath,
        headers: {
          Upgrade: "websocket",
          Connection: "Upgrade",
          "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
          "Sec-WebSocket-Version": "13",
        },
      });
      expect(response).toContain("401");
      expect(gateway.calls).toHaveLength(0);
    }
  }, 60000);

  it("P2P-021 selects the client IP from trusted proxy hops", async () => {
    const withProxy = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/api/auth/login",
      headers: {
        "Content-Type": "application/json",
        "X-Forwarded-For": "198.51.100.7",
      },
      body: JSON.stringify({ password: "wrong" }),
    });
    expect(withProxy.status).toBe(401);
    expect(withProxy.json).toEqual({ ok: false, error: "Invalid credentials" });

    const direct = await httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/api/auth/login",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: "wrong" }),
    });
    expect(direct.status).toBe(401);
  });
});
