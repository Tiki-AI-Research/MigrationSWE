const fs = require("fs");
const path = require("path");
const http = require("http");
const net = require("net");
const os = require("os");
const zlib = require("zlib");
const crypto = require("crypto");
const { createRequire } = require("module");
const { spawnSync } = require("child_process");

const kTargetTechnology = { name: "Hono", version: "4.13.7" };
const kSourceTechnology = { name: "Express", version: "5.2.1" };
const kDefaultJsonLimit = 5 * 1024 * 1024;
const kDefaultV1Limit = 50 * 1024 * 1024;

const OBSERVATIONS = {
  "F2P-025": {
    requirement_ids: ["R045"],
    entry: {
      file: "tests/server/gateway.test.js",
      name: "removes the prior managed entry when REMOTE_MCP_NAME changes",
    },
    probe: "sse",
  },
  "F2P-026": {
    requirement_ids: ["R043"],
    entry: {
      file: "tests/server/onboarding-github.test.js",
      name: "clones without embedding the github token in the command line",
    },
    probe: "raw-webhook",
  },
  "F2P-027": {
    requirement_ids: ["R048"],
    entry: null,
    probe: "trust-proxy",
  },
  "F2P-028": {
    requirement_ids: ["R043"],
    entry: {
      file: "tests/server/routes-usage.test.js",
      name: "server/routes/usage",
    },
    probe: "raw-webhook",
  },
  "F2P-029": {
    requirement_ids: ["R045"],
    entry: {
      file: "tests/server/openclaw-channel-plugin-preflight.test.js",
      name: "does not update current, disabled, or non-official plugins",
    },
    probe: "sse",
  },
  "F2P-030": {
    requirement_ids: ["R001"],
    entry: {
      file: "tests/server/secret-detector.test.js",
      name: "skips values that are already env var references",
    },
    probe: "health",
  },
  "F2P-031": {
    requirement_ids: ["R001"],
    entry: {
      file: "tests/server/watchdog-notify.test.js",
      name: "reports partial Slack delivery failure when one account is missing a bot token",
    },
    probe: "health",
  },
  "F2P-032": {
    requirement_ids: ["R045"],
    entry: {
      file: "tests/server/routes-telegram.test.js",
      name: "quotes git-sync commit messages as a single shell arg",
    },
    probe: "sse",
  },
  "F2P-033": {
    requirement_ids: ["R001"],
    entry: {
      file: "tests/server/cron-service.test.js",
      name: "uses --system-event when editing main systemEvent job prompts",
    },
    probe: "health",
  },
  "F2P-034": {
    requirement_ids: ["R045"],
    entry: {
      file: "tests/server/git-shim.test.js",
      name: "server git shim scripts",
    },
    probe: "sse",
  },
  "F2P-035": {
    requirement_ids: ["R043"],
    entry: {
      file: "tests/server/gateway.test.js",
      name: "uses REMOTE_MCP_NAME as the server key when set",
    },
    probe: "raw-webhook",
  },
  "F2P-036": {
    requirement_ids: ["R044"],
    entry: {
      file: "tests/server/routes-agents.test.js",
      name: "updates an agent on PUT /api/agents/:id",
    },
    probe: "static-public",
  },
  "F2P-037": {
    requirement_ids: ["R049"],
    entry: null,
    probe: "gzip-proxy",
  },
  "F2P-038": {
    requirement_ids: ["R044"],
    entry: {
      file: "tests/server/openclaw-channel-plugin-preflight.test.js",
      name: "server/openclaw-channel-plugin-preflight",
    },
    probe: "static-public",
  },
  "F2P-039": {
    requirement_ids: ["R042"],
    entry: {
      file: "tests/server/routes-pairings.test.js",
      name: "rejects invalid pairing approval input before running command",
    },
    probe: "json-body",
  },
  "F2P-040": {
    requirement_ids: ["R046"],
    entry: {
      file: "tests/server/agents-service.test.js",
      name: "migrates single-account channel config before adding another account",
    },
    probe: "websocket",
  },
  "F2P-041": {
    requirement_ids: ["R043"],
    entry: {
      file: "tests/server/import-temp.test.js",
      name: "server/onboarding/import-temp",
    },
    probe: "raw-webhook",
  },
  "F2P-042": {
    requirement_ids: ["R044"],
    entry: {
      file: "tests/server/agents-service.test.js",
      name: "creates a whatsapp channel account with allowlist defaults",
    },
    probe: "static-public",
  },
  "F2P-043": {
    requirement_ids: ["R045"],
    entry: {
      file: "tests/server/routes-onboarding.test.js",
      name: "does not treat nested openclaw config as an imported config during completion",
    },
    probe: "sse",
  },
  "F2P-044": {
    requirement_ids: ["R042"],
    entry: {
      file: "tests/server/onboarding-validation.test.js",
      name: "accepts canonical GPT-5.5 with an OpenAI API key and no Codex OAuth",
    },
    probe: "json-body",
  },
  "F2P-045": {
    requirement_ids: ["R001"],
    entry: {
      file: "tests/server/routes-proxy.test.js",
      name: "rejects /v1 requests when no gateway token is configured",
    },
    probe: "health",
  },
  "F2P-046": {
    requirement_ids: ["R042"],
    entry: {
      file: "tests/server/webhooks-db.test.js",
      name: "server/webhooks-db",
    },
    probe: "json-body",
  },
  "F2P-047": {
    requirement_ids: ["R046"],
    entry: {
      file: "tests/server/routes-webhooks.test.js",
      name: "deletes oauth callback alias when deleting webhook",
    },
    probe: "websocket",
  },
  "F2P-048": {
    requirement_ids: ["R044"],
    entry: {
      file: "tests/server/onboarding-validation.test.js",
      name: "accepts OPENROUTER_API_KEY when the selected model uses the openrouter provider",
    },
    probe: "static-public",
  },
};

const result = (status, reason, evidence = {}) => ({ status, reason, evidence });

const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};

const readJson = (filePath) => JSON.parse(fs.readFileSync(filePath, "utf8"));

const locateRepoRoot = (options = {}) => {
  const candidates = [
    options.repoRoot,
    process.env.ALPHACLAW_REPO_ROOT,
    process.argv[2],
    process.cwd(),
  ].filter(Boolean);
  for (const candidate of candidates) {
    const resolved = path.resolve(candidate);
    if (fs.existsSync(path.join(resolved, "package.json"))) return resolved;
  }
  throw new Error("Could not locate repository root with package.json");
};

const createContext = (repoRoot) => {
  const packagePath = path.join(repoRoot, "package.json");
  const pkg = readJson(packagePath);
  const repoRequire = createRequire(packagePath);
  const has = (relativePath) => fs.existsSync(path.join(repoRoot, relativePath));
  const read = (relativePath) =>
    fs.readFileSync(path.join(repoRoot, relativePath), "utf8");
  const load = (relativePath) => require(path.join(repoRoot, relativePath));
  const installedVersion = (name) => {
    const candidates = [
      path.join(repoRoot, "node_modules", name, "package.json"),
      path.join(path.dirname(packagePath), "node_modules", name, "package.json"),
    ];
    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) return readJson(candidate).version;
    }
    try {
      let directory = path.dirname(repoRequire.resolve(name));
      while (directory && directory !== path.dirname(directory)) {
        const candidate = path.join(directory, "package.json");
        if (fs.existsSync(candidate)) {
          const manifest = readJson(candidate);
          if (manifest.name === name) return manifest.version;
        }
        directory = path.dirname(directory);
      }
    } catch {}
    try {
      return repoRequire(`${name}/package.json`).version;
    } catch {
      return null;
    }
  };
  const target = {
    hasHonoApp: has("lib/server/http/hono-app.js"),
    hasBodyLimits: has("lib/server/http/body-limits.js"),
    hasStaticFiles: has("lib/server/http/static-files.js"),
    hasSse: has("lib/server/http/sse.js"),
    hasClientIp: has("lib/server/http/client-ip.js"),
    declaredHonoVersion: pkg.dependencies?.hono,
    installedHonoVersion: installedVersion("hono"),
    declaredExpressVersion: pkg.dependencies?.express,
    installedExpressVersion: installedVersion("express"),
  };
  return {
    root: repoRoot,
    packagePath,
    pkg,
    repoRequire,
    has,
    read,
    load,
    target,
  };
};

const listen = (server) =>
  new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });

const closeServer = (server) =>
  new Promise((resolve) => {
    if (!server || !server.listening) return resolve();
    server.close(() => resolve());
    setTimeout(() => {
      try {
        server.closeAllConnections?.();
      } catch {}
      resolve();
    }, 500).unref?.();
  });

const httpRequest = ({
  port,
  method = "GET",
  requestPath,
  headers = {},
  body,
  timeoutMs = 15000,
}) =>
  new Promise((resolve, reject) => {
    const payload = body === undefined ? null : Buffer.from(body);
    const requestHeaders = { ...headers };
    if (payload) requestHeaders["content-length"] = String(payload.length);
    const request = http.request(
      {
        host: "127.0.0.1",
        port,
        method,
        path: requestPath,
        headers: requestHeaders,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
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
    request.setTimeout(timeoutMs, () => {
      request.destroy(new Error(`request timeout: ${method} ${requestPath}`));
    });
    request.on("error", reject);
    if (payload) request.write(payload);
    request.end();
  });

const buildApp = (context, options = {}) => {
  const {
    trustProxy,
    body = false,
    raw = true,
    v1 = true,
    v1Enabled = true,
    staticFiles = false,
    staticPublic = false,
  } = options;
  const state = { marker: 0 };
  if (context.target.declaredHonoVersion) {
    return { app: null, state, kind: "live" };
  }
  if (context.target.hasHonoApp) {
    const { createHonoApp } = context.load("lib/server/http/hono-app.js");
    const app = createHonoApp();
    app.hono.use("*", async (_c, next) => {
      state.marker += 1;
      await next();
    });
    if (trustProxy !== undefined) {
      app.hono.use("*", async (c, next) => {
        c.set("alphaclaw.trustProxyHops", trustProxy);
        await next();
      });
    }
    if (body) {
      const bodyLimits = context.load("lib/server/http/body-limits.js");
      if (raw) {
        app.hono.use(
          "*",
          bodyLimits.createRawBodyMiddleware({ limitBytes: kDefaultJsonLimit }),
        );
      }
      if (v1) {
        app.hono.use(
          "*",
          bodyLimits.createV1BodyMiddleware({
            limitBytes: kDefaultV1Limit,
            isEnabled: () => v1Enabled,
          }),
        );
      }
      app.hono.use(
        "*",
        bodyLimits.createJsonBodyMiddleware({ limitBytes: kDefaultJsonLimit }),
      );
    }
    if (staticPublic) {
      const staticModule = context.load("lib/server/http/static-files.js");
      app.use(staticModule.createPublicStaticMiddleware());
    }
    return { app, state, kind: "hono" };
  }

  const express = context.repoRequire("express");
  const app = express();
  if (trustProxy !== undefined) app.set("trust proxy", trustProxy);
  if (body) {
    if (raw) {
      app.use(
        ["/webhook", "/hooks"],
        express.raw({ type: "*/*", limit: "5mb" }),
      );
      app.use("/gmail-pubsub", express.raw({ type: "*/*", limit: "5mb" }));
    }
    if (v1) {
      const v1Parser = express.json({ limit: "50mb" });
      app.use("/v1", (req, res, next) => {
        if (!v1Enabled) return res.status(404).json({ error: "Not found" });
        return v1Parser(req, res, next);
      });
    }
    app.use(express.json({ limit: "5mb" }));
  }
  if (staticPublic) {
    app.use(express.static(path.join(context.root, "lib", "public")));
  }
  return { app, state, kind: "express" };
};

const startBuiltApp = (context, built) => {
  if (built.kind === "hono") {
    const { createAdaptorServer } = context.repoRequire("@hono/node-server");
    return createAdaptorServer({ fetch: built.app.fetch });
  }
  built.app.use((error, _request, response, next) => {
    if (response.headersSent) return next(error);
    const status = Number(error?.status) || 500;
    response.status(status).json({ error: error?.message || "Request failed" });
  });
  return http.createServer(built.app);
};

const expressLoadedInCache = () =>
  Object.keys(require.cache).some(
    (key) =>
      key.includes(`${path.sep}node_modules${path.sep}express${path.sep}`) &&
      !key.endsWith(`${path.sep}package.json`) &&
      !key.endsWith(`${path.sep}README.md`),
  );

const checkTargetTechnology = (
  context,
  built,
  { modules = [], requireMarker = true } = {},
) => {
  const reasons = [];
  const evidence = {
    required_technology: kTargetTechnology,
    source_technology: kSourceTechnology,
    declared_hono_version: context.target.declaredHonoVersion,
    installed_hono_version: context.target.installedHonoVersion,
    serving_kind: built.kind,
    hono_middleware_invocations: built.state.marker,
    required_modules: {},
    express_loaded: expressLoadedInCache(),
  };
  for (const modulePath of modules) {
    evidence.required_modules[modulePath] = true;
  }
  if (context.target.declaredHonoVersion !== kTargetTechnology.version) {
    reasons.push(
      `package.json must declare hono ${kTargetTechnology.version}, found ${context.target.declaredHonoVersion || "none"}`,
    );
  }
  if (context.target.installedHonoVersion !== kTargetTechnology.version) {
    reasons.push(
      `installed hono must be ${kTargetTechnology.version}, found ${context.target.installedHonoVersion || "none"}`,
    );
  }
  if (built.kind !== "hono" && context.target.declaredHonoVersion) {
    reasons.push("package declares Hono but the probe built an Express app");
  }
  if (evidence.express_loaded) {
    reasons.push("express was loaded on the serving path");
  }
  evidence.pass = reasons.length === 0;
  evidence.reasons = reasons;
  return evidence;
};

const runBoundEntryTest = (context, entry) => {
  if (!entry) return { ok: true, skipped: true, evidence: {} };
  const testFile = path.join(context.root, entry.file);
  if (!fs.existsSync(testFile)) {
    return {
      ok: false,
      evidence: { entry: entry.file, test: entry.name, error: "test file missing" },
    };
  }
  let vitest = path.join(context.root, "node_modules", "vitest", "vitest.mjs");
  if (!fs.existsSync(vitest)) {
    try {
      const vitestPackage = context.repoRequire.resolve("vitest/package.json");
      vitest = path.join(path.dirname(vitestPackage), "vitest.mjs");
    } catch {}
  }
  if (!fs.existsSync(vitest)) {
    return {
      ok: false,
      infrastructure: true,
      evidence: { entry: entry.file, test: entry.name, error: "vitest missing" },
    };
  }
  const startedAt = Date.now();
  const completed = spawnSync(
    process.execPath,
    [
      vitest,
      "run",
      entry.file,
      "-t",
      entry.name,
      "--reporter=dot",
      "--no-color",
    ],
    {
      cwd: context.root,
      env: {
        ...process.env,
        CI: "1",
        VITE_CONFIG_NATIVE_IGNORE_WARNING: "true",
      },
      encoding: "utf8",
      timeout: 120000,
      maxBuffer: 16 * 1024 * 1024,
    },
  );
  const output = `${completed.stdout || ""}\n${completed.stderr || ""}`;
  return {
    ok: completed.status === 0,
    infrastructure: completed.error?.code === "ETIMEDOUT",
    evidence: {
      entry: entry.file,
      test: entry.name,
      exit_code: completed.status,
      duration_ms: Date.now() - startedAt,
      tail: output.trim().split(/\r?\n/).slice(-4).join("\n"),
    },
  };
};

const makeLoginThrottle = (context, overrides = {}) => {
  const { createLoginThrottle } = context.load("lib/server/login-throttle.js");
  const { getClientKey } = context.load("lib/server/helpers.js");
  return {
    ...createLoginThrottle({
      windowMs: 60_000,
      maxAttempts: 2,
      baseLockMs: 60_000,
      maxLockMs: 60_000,
      globalWindowMs: 60_000,
      globalMaxAttempts: 100,
      globalBaseLockMs: 60_000,
      globalMaxLockMs: 60_000,
      stateTtlMs: 180_000,
      ...overrides,
    }),
    getClientKey,
  };
};

const registerMinimalAuth = (context, built, throttle) => {
  const { registerAuthRoutes } = context.load("lib/server/routes/auth.js");
  return registerAuthRoutes({ app: built.app, loginThrottle: throttle });
};

async function probeHealth(context) {
  const built = buildApp(context, { body: false });
  const { registerPageRoutes } = context.load("lib/server/routes/pages.js");
  registerPageRoutes({
    app: built.app,
    requireAuth: (_req, _res, next) => next(),
    isGatewayRunning: async () => true,
  });
  const server = startBuiltApp(context, built);
  const port = await listen(server);
  try {
    const response = await httpRequest({ port, requestPath: "/health" });
    const body = JSON.parse(response.text || "{}");
    const behavior =
      response.status === 200 &&
      ["healthy", "starting"].includes(body.status) &&
      ["running", "starting"].includes(body.gateway);
    const technology = checkTargetTechnology(context, built, {
      modules: ["lib/server/http/hono-app.js"],
    });
    let fetchEvidence = null;
    if (built.kind === "hono") {
      const fetchResponse = await built.app.fetch(
        new Request("http://probe.local/health"),
      );
      const fetchBody = await fetchResponse.json();
      fetchEvidence = {
        status: fetchResponse.status,
        is_response: fetchResponse instanceof Response,
        body: fetchBody,
      };
    }
    const fetchOk =
      built.kind !== "hono" ||
      (fetchEvidence?.is_response === true &&
        fetchEvidence.status === 200 &&
        fetchEvidence.body?.status === body.status);
    const reasons = [];
    if (!behavior) reasons.push("/health did not return the expected JSON contract");
    if (!fetchOk) reasons.push("/health was not served by a Hono fetch handler");
    reasons.push(...technology.reasons);
    return result(
      behavior && fetchOk && technology.pass ? "PASS" : "FAIL",
      reasons.length ? reasons.join("; ") : "Hono served /health through its fetch handler",
      {
        http: { status: response.status, body },
        direct_fetch: fetchEvidence,
        technology,
      },
    );
  } finally {
    await closeServer(server);
  }
}

async function probeJsonBody(context) {
  const built = buildApp(context, { body: true, v1: false });
  let handlerCalls = 0;
  let handlerBody = null;
  built.app.put("/api/env", (req, res) => {
    handlerCalls += 1;
    handlerBody = req.body;
    res.json({ ok: true });
  });
  const throttle = makeLoginThrottle(context);
  const previousPassword = process.env.SETUP_PASSWORD;
  process.env.SETUP_PASSWORD = "probe-setup-password";
  registerMinimalAuth(context, built, throttle);
  const server = startBuiltApp(context, built);
  const port = await listen(server);
  try {
    const login = await httpRequest({
      port,
      method: "POST",
      requestPath: "/api/auth/login",
      headers: { "content-type": "application/json" },
      body: Buffer.from(JSON.stringify({ password: "probe-setup-password" })),
    });
    const loginBody = JSON.parse(login.text || "{}");
    const loginOk =
      login.status === 200 &&
      loginBody.ok === true &&
      String(login.headers["set-cookie"] || "").includes("setup_token=");
    const cookie = String(login.headers["set-cookie"] || "").split(";")[0];
    const small = await httpRequest({
      port,
      method: "PUT",
      requestPath: "/api/env",
      headers: {
        "content-type": "application/json",
        cookie,
      },
      body: Buffer.from(JSON.stringify({ key: "value" })),
    });
    const parsedByMiddleware =
      small.status === 200 &&
      handlerCalls === 1 &&
      handlerBody?.key === "value";
    const oversizedBody = Buffer.from(
      JSON.stringify({ value: "x".repeat(kDefaultJsonLimit + 1024) }),
    );
    const oversized = await httpRequest({
      port,
      method: "PUT",
      requestPath: "/api/env",
      headers: {
        "content-type": "application/json",
        cookie,
      },
      body: oversizedBody,
    });
    const limitOk = oversized.status === 413 && handlerCalls === 1;
    const technology = checkTargetTechnology(context, built, {
      modules: [
        "lib/server/http/hono-app.js",
        "lib/server/http/body-limits.js",
      ],
    });
    const reasons = [];
    if (!loginOk) reasons.push("gzip-free JSON login was not parsed by body middleware");
    if (!parsedByMiddleware) {
      reasons.push("JSON middleware did not populate the route body before the handler");
    }
    if (!limitOk) reasons.push("a body over 5 MB was not rejected with 413 before the handler");
    reasons.push(...technology.reasons);
    return result(
      loginOk && parsedByMiddleware && limitOk && technology.pass ? "PASS" : "FAIL",
      reasons.length
        ? reasons.join("; ")
        : "Hono body middleware parsed JSON and enforced the 5 MB limit",
      {
        login: { status: login.status, body: loginBody },
        handler: { calls: handlerCalls, body: handlerBody },
        oversized: { status: oversized.status, handler_calls_after: handlerCalls },
        technology,
      },
    );
  } finally {
    process.env.SETUP_PASSWORD = previousPassword;
    await closeServer(server);
  }
}

async function probeRawWebhook(context) {
  const gatewayRequests = [];
  const gateway = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      gatewayRequests.push({
        method: req.method,
        url: req.url,
        headers: req.headers,
        body: Buffer.concat(chunks),
      });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  const gatewayPort = await listen(gateway);
  const built = buildApp(context, { body: true, v1: false });
  const { createWebhookMiddleware } = context.load(
    "lib/server/webhook-middleware.js",
  );
  const webhookMiddleware = createWebhookMiddleware({
    gatewayUrl: `http://127.0.0.1:${gatewayPort}`,
    insertRequest: () => {},
    maxPayloadBytes: 1024 * 1024,
  });
  built.app.post("/webhook/foo", webhookMiddleware);
  const server = startBuiltApp(context, built);
  const port = await listen(server);
  try {
    const exactBytes = Buffer.from('{"a":1}', "utf8");
    const forwarded = await httpRequest({
      port,
      method: "POST",
      requestPath: "/webhook/foo",
      headers: {
        "content-type": "application/json",
        authorization: "Bearer probe",
      },
      body: exactBytes,
    });
    const captured = gatewayRequests[0];
    const exactForwarded =
      forwarded.status === 200 &&
      captured?.method === "POST" &&
      captured?.url === "/hooks/foo" &&
      captured.body.equals(exactBytes);
    const oversizedBody = Buffer.alloc(kDefaultJsonLimit + 1, 0x61);
    const oversized = await httpRequest({
      port,
      method: "POST",
      requestPath: "/webhook/foo",
      headers: {
        "content-type": "application/json",
        authorization: "Bearer probe",
      },
      body: oversizedBody,
    });
    const limitOk =
      oversized.status === 413 && gatewayRequests.length === 1;
    const technology = checkTargetTechnology(context, built, {
      modules: [
        "lib/server/http/hono-app.js",
        "lib/server/http/body-limits.js",
      ],
    });
    const usesRawBodyMiddleware = context.has("lib/server/http/body-limits.js")
      ? context.read("lib/server/http/body-limits.js").includes("kRawBodyPrefixes")
      : false;
    const reasons = [];
    if (!exactForwarded) {
      reasons.push("raw webhook bytes were not forwarded byte-for-byte to the gateway");
    }
    if (!limitOk) reasons.push("a raw webhook body over 5 MB was not rejected with 413");
    if (!usesRawBodyMiddleware) {
      reasons.push("the target raw-body middleware is not installed");
    }
    reasons.push(...technology.reasons);
    return result(
      exactForwarded && limitOk && usesRawBodyMiddleware && technology.pass
        ? "PASS"
        : "FAIL",
      reasons.length
        ? reasons.join("; ")
        : "Hono raw-body middleware forwarded exact bytes and enforced the 5 MB limit",
      {
        forwarded: {
          status: forwarded.status,
          method: captured?.method,
          url: captured?.url,
          bytes: captured?.body?.toString("utf8"),
        },
        oversized: { status: oversized.status, gateway_calls: gatewayRequests.length },
        raw_body_middleware: usesRawBodyMiddleware,
        technology,
      },
    );
  } finally {
    await closeServer(server);
    await closeServer(gateway);
  }
}

async function probeStaticPublic(context) {
  const built = buildApp(context, { body: false, staticPublic: true });
  const server = startBuiltApp(context, built);
  const port = await listen(server);
  try {
    const login = await httpRequest({ port, requestPath: "/login.html" });
    const css = await httpRequest({ port, requestPath: "/css/theme.css" });
    const loginFile = fs.readFileSync(
      path.join(context.root, "lib", "public", "login.html"),
    );
    const cssFile = fs.readFileSync(
      path.join(context.root, "lib", "public", "css", "theme.css"),
    );
    const loginOk =
      login.status === 200 &&
      String(login.headers["content-type"] || "").startsWith("text/html") &&
      login.buffer.equals(loginFile);
    const cssOk =
      css.status === 200 &&
      String(css.headers["content-type"] || "").startsWith("text/css") &&
      css.buffer.equals(cssFile);
    const technology = checkTargetTechnology(context, built, {
      modules: [
        "lib/server/http/hono-app.js",
        "lib/server/http/static-files.js",
      ],
    });
    const staticModuleText = context.has("lib/server/http/static-files.js")
      ? context.read("lib/server/http/static-files.js")
      : "";
    const usesHonoServeStatic =
      staticModuleText.includes("@hono/node-server/serve-static") &&
      staticModuleText.includes("serveStatic");
    const reasons = [];
    if (!loginOk) reasons.push("public /login.html was not served as raw file bytes");
    if (!cssOk) reasons.push("public /css/theme.css was not served as raw file bytes");
    if (!usesHonoServeStatic) {
      reasons.push("static files are not served by the Hono node serve-static adapter");
    }
    reasons.push(...technology.reasons);
    return result(
      loginOk && cssOk && usesHonoServeStatic && technology.pass ? "PASS" : "FAIL",
      reasons.length
        ? reasons.join("; ")
        : "Hono static middleware served the public files without authentication",
      {
        login: { status: login.status, bytes: login.buffer.length },
        css: { status: css.status, bytes: css.buffer.length },
        hono_serve_static: usesHonoServeStatic,
        technology,
      },
    );
  } finally {
    await closeServer(server);
  }
}

function parseSseFrames(text) {
  const normalized = text.replace(/\r\n/g, "\n");
  return normalized
    .split("\n\n")
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => {
      const lines = block.split("\n");
      const event =
        lines.find((line) => line.startsWith("event: "))?.slice("event: ".length) ||
        (lines.find((line) => line.startsWith(":")) ? "comment" : "");
      const data = lines
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice("data: ".length))
        .join("\n");
      return { event, data, block };
    });
}

async function probeSse(context) {
  const built = buildApp(context, { body: false });
  const systemRoutes = context.load("lib/server/routes/system.js");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "f2p-sse-"));
  const noop = () => {};
  systemRoutes.registerSystemRoutes({
    app: built.app,
    fs,
    readEnvFile: () => [],
    writeEnvFile: noop,
    reloadEnv: () => false,
    kKnownVars: new Set(),
    kKnownKeys: new Set(),
    kSystemVars: new Set(["ALPHACLAW_ROOT_DIR"]),
    syncChannelConfig: noop,
    isGatewayRunning: async () => true,
    isOnboarded: () => true,
    getChannelStatus: () => ({}),
    openclawVersionService: { readOpenclawVersion: () => ({ version: "probe" }) },
    alphaclawVersionService: { readAlphaclawVersion: () => ({ version: "probe" }) },
    kAlphaclawGithubReleasesBaseUrl: "https://example.invalid",
    clawCmd: async () => "",
    restartGateway: noop,
    OPENCLAW_DIR: tempDir,
    restartRequiredState: {
      markRequired: noop,
      getSnapshot: async () => ({ restartRequired: false }),
    },
    topicRegistry: {},
    authProfiles: {},
    watchdog: { getStatus: () => ({ health: "ok" }) },
    doctorService: { buildStatus: () => ({ state: "ok" }) },
    ensureGatewayProxyConfig: noop,
    getBaseUrl: () => "http://127.0.0.1",
  });
  const server = startBuiltApp(context, built);
  const port = await listen(server);
  const realSetInterval = global.setInterval;
  const realClearInterval = global.clearInterval;
  const requestedIntervals = [];
  const activeIntervals = new Map();
  const clearedIntervals = [];
  let streamRequest = null;
  try {
    global.setInterval = (callback, delay, ...args) => {
      requestedIntervals.push(delay);
      const scaledDelay = delay === 2000 ? 40 : delay === 15000 ? 50 : delay;
      const id = realSetInterval(callback, scaledDelay, ...args);
      activeIntervals.set(id, delay);
      return id;
    };
    global.clearInterval = (id) => {
      if (activeIntervals.has(id)) clearedIntervals.push(activeIntervals.get(id));
      realClearInterval(id);
    };

    const startedAt = Date.now();
    const stream = await new Promise((resolve, reject) => {
      let firstChunkAt = null;
      const chunks = [];
      streamRequest = http.request(
        {
          host: "127.0.0.1",
          port,
          method: "GET",
          path: "/api/events/status",
          headers: { accept: "text/event-stream" },
        },
        (response) => {
          response.on("data", (chunk) => {
            if (firstChunkAt === null) firstChunkAt = Date.now() - startedAt;
            chunks.push(chunk.toString("utf8"));
          });
          response.on("end", () => {});
          setTimeout(() => {
            resolve({
              status: response.statusCode,
              headers: response.headers,
              text: chunks.join(""),
              firstChunkAt,
            });
          }, 600);
        },
      );
      streamRequest.setTimeout(10000, () =>
        reject(new Error("SSE request timed out")),
      );
      streamRequest.on("error", reject);
      streamRequest.end();
    });

    const frames = parseSseFrames(stream.text);
    const statusFrames = frames.filter((frame) => frame.event === "status");
    const firstPayload = statusFrames[0]?.data
      ? JSON.parse(statusFrames[0].data)
      : null;
    const keepAlive = frames.some(
      (frame) => frame.event === "comment" && frame.block.includes(": keepalive"),
    );
    const headersOk =
      String(stream.headers["content-type"] || "").includes("text/event-stream") &&
      String(stream.headers["cache-control"] || "").includes("no-cache") &&
      String(stream.headers["cache-control"] || "").includes("no-transform") &&
      String(stream.headers["x-accel-buffering"] || "").toLowerCase() === "no";
    const payloadOk =
      firstPayload &&
      Object.prototype.hasOwnProperty.call(firstPayload, "status") &&
      Object.prototype.hasOwnProperty.call(firstPayload, "watchdogStatus") &&
      Object.prototype.hasOwnProperty.call(firstPayload, "doctorStatus") &&
      typeof firstPayload.timestamp === "string";
    const intervalRequested =
      requestedIntervals.includes(2000) && requestedIntervals.includes(15000);
    const streamedIncrementally =
      statusFrames.length >= 2 && keepAlive && stream.firstChunkAt < 2000;

    streamRequest?.destroy();
    await new Promise((resolve) => setTimeout(resolve, 120));
    const timersStopped =
      clearedIntervals.includes(2000) && clearedIntervals.includes(15000);

    const technology = checkTargetTechnology(context, built, {
      modules: ["lib/server/http/hono-app.js", "lib/server/http/sse.js"],
    });
    const reasons = [];
    if (!headersOk) reasons.push("SSE response headers do not match the contract");
    if (!payloadOk) reasons.push("initial status frame is missing required fields");
    if (!intervalRequested) reasons.push("status/keepalive timers were not scheduled at 2s/15s");
    if (!streamedIncrementally) {
      reasons.push("the response did not emit incremental status and keepalive frames");
    }
    if (!timersStopped) reasons.push("SSE timers were not cleared after disconnect");
    reasons.push(...technology.reasons);
    return result(
      headersOk &&
        payloadOk &&
        intervalRequested &&
        streamedIncrementally &&
        timersStopped &&
        technology.pass
        ? "PASS"
        : "FAIL",
      reasons.length
        ? reasons.join("; ")
        : "Hono streamed incremental SSE frames and stopped timers on disconnect",
      {
        http: {
          status: stream.status,
          content_type: stream.headers["content-type"],
          first_chunk_ms: stream.firstChunkAt,
          status_frames: statusFrames.length,
          keepalive: keepAlive,
        },
        requested_intervals: requestedIntervals,
        cleared_intervals: clearedIntervals,
        payload: firstPayload,
        technology,
      },
    );
  } finally {
    streamRequest?.destroy();
    global.setInterval = realSetInterval;
    global.clearInterval = realClearInterval;
    await closeServer(server);
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

const rawUpgradeRequest = (port, requestPath, headers = {}) =>
  new Promise((resolve) => {
    const socket = net.connect(port, "127.0.0.1", () => {
      const key = crypto.randomBytes(16).toString("base64");
      const lines = [
        `GET ${requestPath} HTTP/1.1`,
        `Host: 127.0.0.1:${port}`,
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        `Sec-WebSocket-Key: ${key}`,
        ...Object.entries(headers).map(([name, value]) => `${name}: ${value}`),
        "",
        "",
      ];
      socket.write(lines.join("\r\n"));
    });
    let received = "";
    const finish = () => {
      socket.destroy();
      resolve(received.split("\r\n")[0] || "");
    };
    socket.on("data", (chunk) => {
      received += chunk.toString("utf8");
      if (received.includes("\r\n")) finish();
    });
    socket.on("error", finish);
    socket.setTimeout(2000, finish);
  });

const openWebSocket = (WebSocket, url, options) =>
  new Promise((resolve, reject) => {
    const socket = new WebSocket(url, options);
    const timer = setTimeout(() => {
      try {
        socket.terminate();
      } catch {}
      reject(new Error("websocket open timeout"));
    }, 5000);
    socket.once("open", () => {
      clearTimeout(timer);
      resolve(socket);
    });
    socket.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });

async function probeWebSocket(context) {
  const built = buildApp(context, { body: true, v1: false });
  const previousPassword = process.env.SETUP_PASSWORD;
  process.env.SETUP_PASSWORD = "probe-ws-password";
  const throttle = makeLoginThrottle(context);
  const { isAuthorizedRequest } = registerMinimalAuth(context, built, throttle);
  const { WebSocketServer, WebSocket } = context.repoRequire("ws");
  const chatWss = new WebSocketServer({ noServer: true });
  chatWss.on("connection", (socket) => {
    socket.on("message", (data) => socket.send(data));
  });
  const chatWsService = {
    handleUpgrade(request, socket, head) {
      chatWss.handleUpgrade(request, socket, head, (ws) => {
        chatWss.emit("connection", ws, request);
      });
    },
  };
  const { createWatchdogTerminalWsBridge } = context.load(
    "lib/server/watchdog-terminal-ws.js",
  );
  const server = startBuiltApp(context, built);
  createWatchdogTerminalWsBridge({
    server,
    proxy: { ws: () => {} },
    getGatewayUrl: () => "http://127.0.0.1:1",
    isAuthorizedRequest,
    watchdogTerminal: {
      subscribe: () => ({ unsubscribe() {} }),
      writeInput() {},
    },
    chatWsService,
  });
  const port = await listen(server);
  let authenticatedSocket = null;
  try {
    const login = await httpRequest({
      port,
      method: "POST",
      requestPath: "/api/auth/login",
      headers: { "content-type": "application/json" },
      body: Buffer.from(JSON.stringify({ password: "probe-ws-password" })),
    });
    const cookie = String(login.headers["set-cookie"] || "").split(";")[0];
    const unauthenticatedStatus = await rawUpgradeRequest(
      port,
      "/api/ws/chat",
    );
    authenticatedSocket = await openWebSocket(
      WebSocket,
      `ws://127.0.0.1:${port}/api/ws/chat`,
      { headers: { Cookie: cookie } },
    );
    const echo = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("websocket echo timeout")), 5000);
      authenticatedSocket.once("message", (data) => {
        clearTimeout(timer);
        resolve(String(data));
      });
      authenticatedSocket.send("probe-echo");
    });
    authenticatedSocket.close();
    const unauthorized =
      unauthenticatedStatus.includes("401") &&
      !unauthenticatedStatus.includes("101");
    const authenticated = echo === "probe-echo";
    const technology = checkTargetTechnology(context, built, {
      modules: ["lib/server/http/hono-app.js"],
    });
    const reasons = [];
    if (!unauthorized) {
      reasons.push("unauthenticated chat upgrade did not receive a 401 before bridging");
    }
    if (!authenticated) reasons.push("authenticated chat upgrade did not echo frames");
    reasons.push(...technology.reasons);
    return result(
      unauthorized && authenticated && technology.pass ? "PASS" : "FAIL",
      reasons.length
        ? reasons.join("; ")
        : "Hono node server wired the authenticated WebSocket upgrade and rejected anonymous clients",
      {
        unauthenticated_upgrade: unauthenticatedStatus,
        authenticated_echo: echo,
        technology,
      },
    );
  } finally {
    try {
      authenticatedSocket?.terminate();
    } catch {}
    for (const client of chatWss.clients) {
      try {
        client.terminate();
      } catch {}
    }
    await new Promise((resolve) => chatWss.close(resolve));
    process.env.SETUP_PASSWORD = previousPassword;
    await closeServer(server);
  }
}

async function probeTrustProxy(context) {
  const previousPassword = process.env.SETUP_PASSWORD;
  process.env.SETUP_PASSWORD = "probe-trust-password";
  const gateway = http.createServer((_req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end("{}");
  });
  const gatewayPort = await listen(gateway);
  const scenarios = [];
  try {
    for (const trustProxy of [1, 0]) {
      const built = buildApp(context, {
        body: true,
        v1: true,
        v1Enabled: true,
        trustProxy,
      });
      const loginThrottle = makeLoginThrottle(context);
      const v1Throttle = makeLoginThrottle(context, { scope: "probe-v1" });
      const auth = registerMinimalAuth(context, built, loginThrottle);
      const { registerProxyRoutes } = context.load("lib/server/routes/proxy.js");
      registerProxyRoutes({
        app: built.app,
        proxy: { web: (_req, res) => res.status(200).json({ ok: true }) },
        getGatewayUrl: () => `http://127.0.0.1:${gatewayPort}`,
        getGatewayToken: () => "gateway-token",
        isOpenAiCompatApiEnabled: () => true,
        openAiCompatApiThrottle: v1Throttle,
        SETUP_API_PREFIXES: [],
        requireAuth: auth.requireAuth,
        oauthCallbackMiddleware: (_req, res) => res.status(204).end(),
        webhookMiddleware: (_req, res) => res.status(204).end(),
      });
      const server = startBuiltApp(context, built);
      const port = await listen(server);
      const loginAttempt = (forwardedFor) =>
        httpRequest({
          port,
          method: "POST",
          requestPath: "/api/auth/login",
          headers: {
            "content-type": "application/json",
            "x-forwarded-for": forwardedFor,
          },
          body: Buffer.from(JSON.stringify({ password: "wrong-password" })),
        });
      const v1Attempt = (forwardedFor) =>
        httpRequest({
          port,
          method: "POST",
          requestPath: "/v1/chat/completions",
          headers: {
            "content-type": "application/json",
            authorization: "Bearer wrong-token",
            "x-forwarded-for": forwardedFor,
          },
          body: Buffer.from(JSON.stringify({ model: "probe" })),
        });
      const loginA1 = await loginAttempt("198.51.100.7");
      const loginA2 = await loginAttempt("198.51.100.7");
      const loginB1 = await loginAttempt("203.0.113.9");
      const v1A1 = await v1Attempt("198.51.100.7");
      const v1A2 = await v1Attempt("198.51.100.7");
      const v1B1 = await v1Attempt("203.0.113.9");
      const scenario = {
        trust_proxy_hops: trustProxy,
        login: {
          same_key_first: loginA1.status,
          same_key_second: loginA2.status,
          rotated_key: loginB1.status,
        },
        v1: {
          same_key_first: v1A1.status,
          same_key_second: v1A2.status,
          rotated_key: v1B1.status,
        },
      };
      const expectedRotated = trustProxy === 1 ? 401 : 429;
      scenario.distinguishes_forwarded_for =
        loginA1.status === 401 &&
        loginA2.status === 429 &&
        loginB1.status === expectedRotated &&
        v1A1.status === 401 &&
        v1A2.status === 429 &&
        v1B1.status === expectedRotated;
      scenarios.push(scenario);
      await closeServer(server);
    }
    const built = buildApp(context, { body: false });
    const technology = checkTargetTechnology(context, built, {
      modules: [
        "lib/server/http/hono-app.js",
        "lib/server/http/client-ip.js",
      ],
      requireMarker: false,
    });
    const clientIpText = context.has("lib/server/http/client-ip.js")
      ? context.read("lib/server/http/client-ip.js")
      : "";
    const usesForwardedFor =
      clientIpText.includes("x-forwarded-for") &&
      clientIpText.includes("trustProxyHops");
    const behavior = scenarios.every(
      (scenario) => scenario.distinguishes_forwarded_for,
    );
    const reasons = [];
    if (!behavior) {
      reasons.push("TRUST_PROXY_HOPS did not change the login and /v1 throttle key");
    }
    if (!usesForwardedFor) {
      reasons.push("the target client-IP resolver does not implement forwarded hops");
    }
    reasons.push(...technology.reasons);
    return result(
      behavior && usesForwardedFor && technology.pass ? "PASS" : "FAIL",
      reasons.length
        ? reasons.join("; ")
        : "Hono client-IP middleware honored TRUST_PROXY_HOPS for login and /v1 throttling",
      { scenarios, forwarded_for_resolver: usesForwardedFor, technology },
    );
  } finally {
    process.env.SETUP_PASSWORD = previousPassword;
    await closeServer(gateway);
  }
}

async function probeGzipProxy(context) {
  const gatewayRequests = [];
  const gateway = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const requestBody = Buffer.concat(chunks);
      gatewayRequests.push({
        method: req.method,
        url: req.url,
        headers: req.headers,
        body: requestBody,
      });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ ok: true, data: [] }));
    });
  });
  const gatewayPort = await listen(gateway);
  const built = buildApp(context, {
    body: true,
    v1: true,
    v1Enabled: true,
  });
  const proxyRecords = [];
  const sourceProxy = {
    web: (req, res) => {
      proxyRecords.push({ method: req.method, url: req.url });
      res.status(200).json({ ok: true });
    },
  };
  const loginThrottle = makeLoginThrottle(context, { maxAttempts: 100 });
  const previousPassword = process.env.SETUP_PASSWORD;
  process.env.SETUP_PASSWORD = "probe-gzip-password";
  const auth = registerMinimalAuth(context, built, loginThrottle);
  const { registerProxyRoutes } = context.load("lib/server/routes/proxy.js");
  registerProxyRoutes({
    app: built.app,
    proxy: sourceProxy,
    getGatewayUrl: () => `http://127.0.0.1:${gatewayPort}`,
    getGatewayToken: () => "gateway-token",
    isOpenAiCompatApiEnabled: () => true,
    openAiCompatApiThrottle: null,
    SETUP_API_PREFIXES: [],
    requireAuth: auth.requireAuth,
    oauthCallbackMiddleware: (_req, res) => res.status(204).end(),
    webhookMiddleware: (_req, res) => res.status(204).end(),
  });
  const server = startBuiltApp(context, built);
  const port = await listen(server);
  try {
    const loginPayload = Buffer.from(
      JSON.stringify({ password: "probe-gzip-password" }),
    );
    const gzippedLogin = zlib.gzipSync(loginPayload);
    const login = await httpRequest({
      port,
      method: "POST",
      requestPath: "/api/auth/login",
      headers: {
        "content-type": "application/json",
        "content-encoding": "gzip",
      },
      body: gzippedLogin,
    });
    const loginBody = JSON.parse(login.text || "{}");
    const cookie = String(login.headers["set-cookie"] || "").split(";")[0];
    const loginOk = login.status === 200 && loginBody.ok === true;

    const v1PayloadObject = {
      model: "probe-model",
      messages: [{ role: "user", content: "hello" }],
    };
    const v1Raw = Buffer.from(JSON.stringify(v1PayloadObject));
    const gzippedV1 = zlib.gzipSync(v1Raw);
    const v1Response = await httpRequest({
      port,
      method: "POST",
      requestPath: "/v1/chat/completions",
      headers: {
        "content-type": "application/json",
        "content-encoding": "gzip",
        authorization: "Bearer gateway-token",
      },
      body: gzippedV1,
    });
    const v1GatewayRequest = gatewayRequests.find(
      (request) => request.url === "/v1/chat/completions",
    );
    let forwardedJson = null;
    try {
      forwardedJson = JSON.parse(v1GatewayRequest?.body?.toString("utf8") || "");
    } catch {}
    const gzipForwarded =
      v1Response.status === 200 &&
      forwardedJson &&
      forwardedJson.model === v1PayloadObject.model &&
      forwardedJson.messages?.[0]?.content === "hello" &&
      !v1GatewayRequest?.headers?.["content-encoding"] &&
      String(v1GatewayRequest?.headers?.["content-length"] || "") ===
        String(v1GatewayRequest?.body?.length || "") &&
      !v1GatewayRequest?.body?.equals(gzippedV1);

    const openclaw = await httpRequest({
      port,
      requestPath: "/openclaw/abc",
      headers: { cookie },
    });
    const unknownApi = await httpRequest({
      port,
      requestPath: "/api/unknown-route-with-no-setup-handler",
      headers: { cookie },
    });
    const models = await httpRequest({
      port,
      requestPath: "/v1/models",
      headers: { authorization: "Bearer gateway-token" },
    });
    const wrongBearer = await httpRequest({
      port,
      requestPath: "/v1/models",
      headers: { authorization: "Bearer wrong-token" },
    });
    const disabledBuilt = buildApp(context, {
      body: true,
      v1: true,
      v1Enabled: false,
    });
    registerMinimalAuth(
      context,
      disabledBuilt,
      makeLoginThrottle(context, { maxAttempts: 100 }),
    );
    const { registerProxyRoutes: registerDisabledProxy } = context.load(
      "lib/server/routes/proxy.js",
    );
    registerDisabledProxy({
      app: disabledBuilt.app,
      proxy: sourceProxy,
      getGatewayUrl: () => `http://127.0.0.1:${gatewayPort}`,
      getGatewayToken: () => "gateway-token",
      isOpenAiCompatApiEnabled: () => false,
      openAiCompatApiThrottle: null,
      SETUP_API_PREFIXES: [],
      requireAuth: (_req, _res, next) => next(),
      oauthCallbackMiddleware: (_req, res) => res.status(204).end(),
      webhookMiddleware: (_req, res) => res.status(204).end(),
    });
    const disabledServer = startBuiltApp(context, disabledBuilt);
    const disabledPort = await listen(disabledServer);
    const disabled = await httpRequest({
      port: disabledPort,
      requestPath: "/v1/chat/completions",
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: "Bearer gateway-token",
      },
      body: Buffer.from(JSON.stringify({ model: "probe-model" })),
    });
    await closeServer(disabledServer);

    const openclawGatewayRequest = gatewayRequests.find(
      (request) => request.url === "/abc",
    );
    const unknownGatewayRequest = gatewayRequests.find(
      (request) =>
        request.url === "/api/unknown-route-with-no-setup-handler",
    );
    const modelsGatewayRequest = gatewayRequests.find(
      (request) => request.url === "/v1/models",
    );
    const proxyBehaviorOk =
      openclaw.status === 200 &&
      unknownApi.status === 200 &&
      models.status === 200 &&
      wrongBearer.status === 401 &&
      disabled.status === 404 &&
      (openclawGatewayRequest ||
        proxyRecords.some((record) => record.url === "/abc")) &&
      (unknownGatewayRequest ||
        proxyRecords.some(
          (record) => record.url === "/api/unknown-route-with-no-setup-handler",
        )) &&
      (modelsGatewayRequest ||
        proxyRecords.some((record) => record.url === "/v1/models"));

    const technology = checkTargetTechnology(context, built, {
      modules: [
        "lib/server/http/hono-app.js",
        "lib/server/http/body-limits.js",
      ],
    });
    const inflatesGzip = context.has("lib/server/http/body-limits.js")
      ? context
          .read("lib/server/http/body-limits.js")
          .includes("zlib.gunzipSync")
      : false;
    const reasons = [];
    if (!loginOk) reasons.push("gzipped login JSON was not inflated before parsing");
    if (!gzipForwarded) {
      reasons.push("gzipped /v1 JSON was not inflated and stripped before forwarding");
    }
    if (!inflatesGzip) reasons.push("the target body middleware has no gzip inflater");
    if (!proxyBehaviorOk) reasons.push("proxy dispatch did not preserve the required /v1 and /openclaw contracts");
    reasons.push(...technology.reasons);
    return result(
      loginOk && gzipForwarded && inflatesGzip && proxyBehaviorOk && technology.pass
        ? "PASS"
        : "FAIL",
      reasons.length
        ? reasons.join("; ")
        : "Hono body middleware inflated gzip bodies and preserved proxy dispatch",
      {
        login: { status: login.status, body: loginBody },
        gzip_forwarding: {
          status: v1Response.status,
          compressed_bytes: gzippedV1.length,
          forwarded_bytes: v1GatewayRequest?.body?.length,
          forwarded_json: forwardedJson,
          content_encoding: v1GatewayRequest?.headers?.["content-encoding"] || null,
        },
        proxy: {
          openclaw: openclaw.status,
          unknown_api: unknownApi.status,
          models: models.status,
          wrong_bearer: wrongBearer.status,
          disabled: disabled.status,
        },
        technology,
      },
    );
  } finally {
    process.env.SETUP_PASSWORD = previousPassword;
    await closeServer(server);
    await closeServer(gateway);
  }
}

const PROBES = {
  health: probeHealth,
  "json-body": probeJsonBody,
  "raw-webhook": probeRawWebhook,
  "static-public": probeStaticPublic,
  sse: probeSse,
  websocket: probeWebSocket,
  "trust-proxy": probeTrustProxy,
  "gzip-proxy": probeGzipProxy,
};

async function runLiveContract(repoRoot, probeName) {
  const live = require(path.join(__dirname, "../../integration/live-server.js"));
  // This probe needs a real configurable upstream. The shared sidecar's
  // reply() is a no-op, so it cannot exercise response-header filtering.
  let ownedGateway;
  let ownedServer;
  try {
    const options = {
      setupPassword: "secret",
      openaiCompatEnabled: true,
      gatewayToken: "gateway-token",
    };
    let session;
    if (probeName === "gzip-proxy") {
      ownedGateway = await live.createGatewaySpy();
      ownedServer = await live.startLiveServer({
        repoRoot,
        gatewayPort: ownedGateway.port,
        port: await live.freePort(),
        ...options,
      });
      session = { gateway: ownedGateway, server: ownedServer };
    } else {
      session = await live.sharedLiveSession(repoRoot, options);
    }
    const { gateway, server } = session;
    const call = (options) => live.httpCall(server.baseUrl, options);
    let proxyChecks;
    const login = await call({
      method: "POST",
      requestPath: "/api/auth/login",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: "secret" }),
    });
    const cookie = login.cookie;
    const checks = {
      health: async () => {
        const response = await call({ requestPath: "/health" });
        return (
          response.status === 200 &&
          ["healthy", "starting"].includes(response.json?.status) &&
          ["running", "starting"].includes(response.json?.gateway)
        );
      },
      "static-public": async () => {
        const page = await call({ requestPath: "/login.html" });
        const stylesheet = await call({ requestPath: "/css/theme.css" });
        return (
          page.status === 200 &&
          /text\/html/i.test(page.headers["content-type"] || "") &&
          stylesheet.status === 200
        );
      },
      "json-body": async () => {
        const setCookie = login.headers["set-cookie"] || "";
        const rejected = await call({
          method: "PUT",
          requestPath: "/api/env",
          headers: { "content-type": "application/json", cookie },
          body: "a".repeat(5 * 1024 * 1024 + 1),
        });
        return (
          login.status === 200 &&
          login.json?.ok === true &&
          /httponly/i.test(setCookie) &&
          /samesite=lax/i.test(setCookie) &&
          rejected.status === 413
        );
      },
      "raw-webhook": async () => {
        const seen = gateway.calls.length;
        const response = await call({
          method: "POST",
          requestPath: "/webhook/foo?token=query-token&from=query",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ token: "body-token", from: "body" }),
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
      sse: async () => {
        const response = await call({
          requestPath: "/api/events/status",
          headers: { cookie },
        });
        return (
          response.status === 200 &&
          /text\/event-stream/i.test(response.headers["content-type"] || "") &&
          /no-cache/.test(response.headers["cache-control"] || "") &&
          response.headers["x-accel-buffering"] === "no" &&
          /event:\s*status/.test(response.text)
        );
      },
      websocket: async () => {
        const upgrade = (requestPath) =>
          new Promise((resolve) => {
            const socket = net.connect(server.port, "127.0.0.1");
            let text = "";
            const finish = () => {
              socket.destroy();
              resolve(text);
            };
            socket.setTimeout(4000, finish);
            socket.on("data", (chunk) => {
              text += String(chunk);
            });
            socket.on("close", finish);
            socket.on("error", finish);
            socket.on("connect", () => {
              socket.write(
                `GET ${requestPath} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: cHJvYmUta2V5LXZhbHVl\r\n\r\n`,
              );
            });
          });
        const seen = gateway.calls.length;
        const rejected = await Promise.all(
          ["/api/ws/chat", "/api/watchdog/terminal/ws", "/openclaw/x"].map(
            async (requestPath) => /401/.test(await upgrade(requestPath)),
          ),
        );
        return rejected.every(Boolean) && gateway.calls.length === seen;
      },
      "trust-proxy": async () => {
        const responses = [];
        for (let attempt = 0; attempt < 12; attempt += 1) {
          const response = await call({
            method: "POST",
            requestPath: "/api/auth/login",
            headers: {
              "content-type": "application/json",
              "x-forwarded-for": "198.51.100.7, 203.0.113.9",
            },
            body: JSON.stringify({ password: "wrong-password" }),
          });
          responses.push(response);
          if (response.status === 429) break;
        }
        const blocked = responses.find((response) => response.status === 429);
        return (
          blocked?.status === 429 &&
          Number(blocked.headers["retry-after"]) > 0 &&
          Number(blocked.json?.retryAfterSec) > 0
        );
      },
      "gzip-proxy": async () => {
        const seen = gateway.calls.length;
        const requestPath = "/v1/models/probe%2Fmodel?encoded=%2F%3F&item=a&item=b";
        const payload = { model: "probe-model", messages: [{ role: "user", content: "hello" }] };
        const expectedBody = "first gateway chunk\nsecond gateway chunk\n";
        gateway.reply({
          status: 207,
          headers: {
            "content-type": "text/plain; charset=utf-8",
            "set-cookie": "upstream_session=private; Path=/",
            connection: "close, X-Upstream-Private, x-upstream-second",
            "x-upstream-private": "must not cross the proxy",
            "x-upstream-second": "must not cross the proxy either",
            "proxy-authenticate": "Basic realm=upstream",
            "x-end-to-end": "preserved",
          },
          body: expectedBody,
        });
        const response = await call({
          method: "POST",
          requestPath,
          headers: {
            authorization: "Bearer gateway-token",
            "content-type": "application/json",
            "content-encoding": "gzip",
            cookie: "setup_token=must-not-be-forwarded",
          },
          body: zlib.gzipSync(Buffer.from(JSON.stringify(payload))),
        });
        const forwarded = gateway.calls.slice(seen).at(-1);
        let forwardedBody;
        try {
          forwardedBody = JSON.parse(forwarded?.bodyText || "");
        } catch {}
        proxyChecks = {
          statusPreserved: response.status === 207,
          bodyPreserved: response.text === expectedBody,
          endToEndHeaderPreserved: response.headers["x-end-to-end"] === "preserved",
          setCookieStripped: !("set-cookie" in response.headers),
          fixedHopHeadersStripped: !("proxy-authenticate" in response.headers),
          connectionTokensStripped: !("x-upstream-private" in response.headers) &&
            !("x-upstream-second" in response.headers),
          methodPreserved: forwarded?.method === "POST",
          encodedUrlPreserved: forwarded?.url === requestPath,
          gzipJsonInflated: forwardedBody?.model === payload.model &&
            forwardedBody?.messages?.[0]?.content === payload.messages[0].content,
          contentLengthRebuilt: Number(forwarded?.headers["content-length"]) ===
            Buffer.byteLength(forwarded?.bodyText || ""),
          contentEncodingStripped: !forwarded?.headers["content-encoding"],
          cookieStripped: !forwarded?.headers.cookie,
        };
        return Object.values(proxyChecks).every(Boolean);
      },
    };
    const ok = checks[probeName] ? await checks[probeName]() : false;
    return result(ok ? "PASS" : "FAIL", ok ? "live contract passed" : "live contract failed", {
      probe: probeName,
      ...(proxyChecks ? { checks: proxyChecks } : {}),
    });
  } catch (error) {
    return result("FAIL", String(error && error.message ? error.message : error), { probe: probeName });
  } finally {
    if (ownedServer) await ownedServer.stop();
    if (ownedGateway) await ownedGateway.close();
  }
}

async function runObservation(observationId, options = {}) {
  const config = OBSERVATIONS[observationId];
  if (!config) {
    return result("INCONCLUSIVE", `Unknown observation: ${observationId}`, {});
  }
  const repoRoot = locateRepoRoot(options);
  const context = createContext(repoRoot);
  if (context.target.declaredHonoVersion) {
    return runLiveContract(repoRoot, config.probe);
  }
  const entryResult = runBoundEntryTest(context, config.entry);
  if (!entryResult.ok) {
    return result(
      entryResult.infrastructure ? "INCONCLUSIVE" : "FAIL",
      entryResult.infrastructure
        ? "bound test entry could not be executed"
        : "bound test entry failed",
      {
        observation_id: observationId,
        requirement_ids: config.requirement_ids,
        repo_root: repoRoot,
        bound_entry: entryResult.evidence,
      },
    );
  }
  const probe = PROBES[config.probe];
  if (!probe) {
    return result("INCONCLUSIVE", `Unknown probe: ${config.probe}`, {});
  }
  const probeResult = await probe(context);
  return result(probeResult.status, probeResult.reason, {
    observation_id: observationId,
    requirement_ids: config.requirement_ids,
    repo_root: repoRoot,
    bound_entry: entryResult.evidence,
    probe: probeResult.evidence,
  });
}

module.exports = {
  OBSERVATIONS,
  kSourceTechnology,
  kTargetTechnology,
  locateRepoRoot,
  runObservation,
};
