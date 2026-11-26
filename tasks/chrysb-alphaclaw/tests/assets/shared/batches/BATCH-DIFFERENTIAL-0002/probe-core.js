const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");

const kBatchId = "BATCH-DIFFERENTIAL-0002";
const kAssetRoot = path.resolve(__dirname, "..", "..", "..");
const kCaseDir = path.join(kAssetRoot, "shared", "batches", kBatchId);
const kCorpusPath = path.join(kCaseDir, "corpus.json");
const kJsonLimit = 5 * 1024 * 1024;

const readJson = (filePath) => JSON.parse(fs.readFileSync(filePath, "utf8"));

const stableValue = (value) => {
  if (Array.isArray(value)) return value.map(stableValue);
  if (!value || typeof value !== "object") return value;
  return Object.keys(value)
    .sort()
    .reduce((result, key) => {
      result[key] = stableValue(value[key]);
      return result;
    }, {});
};

const stableStringify = (value) => JSON.stringify(stableValue(value));

const sha256 = (value) =>
  `sha256:${crypto.createHash("sha256").update(value).digest("hex")}`;

const listFiles = (directory, baseDirectory, files = []) => {
  if (!fs.existsSync(directory)) return files;
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const absolutePath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      listFiles(absolutePath, baseDirectory, files);
      continue;
    }
    files.push(
      path.relative(baseDirectory, absolutePath).split(path.sep).join("/"),
    );
  }
  return files;
};

const requireRepo = (repoRoot, relativePath) =>
  require(path.join(repoRoot, relativePath));

const createThrottleMock = () => ({
  getClientKey: () => "probe-client",
  getOrCreateLoginAttemptState: () => ({ attempts: 0 }),
  evaluateLoginThrottle: () => ({ blocked: false, retryAfterSec: 0 }),
  recordLoginFailure: () => ({ locked: false, lockMs: 0 }),
  recordLoginSuccess: () => {},
  cleanupLoginAttemptStates: () => {},
});

const liveServer = require(path.join(
  __dirname,
  "../../integration/live-server.js",
));

const buildRuntime = (repoRoot) => {
  const packageJson = readJson(path.join(repoRoot, "package.json"));
  const isHono = Boolean(packageJson.dependencies?.hono);
  if (isHono) {
    return { app: null, isHono, server: null, live: true };
  }
  const express = require("express");
  const app = express();
  app.use(express.json({ limit: "5mb" }));
  return {
    app,
    isHono,
    server: http.createServer(app),
  };
};

const startServer = (server) =>
  new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });

const closeServer = (server) =>
  new Promise((resolve) => {
    if (!server.listening) {
      resolve();
      return;
    }
    server.close(() => resolve());
  });

const httpRequest = ({
  port,
  method = "GET",
  requestPath,
  headers = {},
  body,
  timeoutMs = 5000,
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
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          resolve({
            status: response.statusCode,
            headers: response.headers,
            text: Buffer.concat(chunks).toString("utf8"),
          });
        });
      },
    );
    request.setTimeout(timeoutMs, () => {
      request.destroy(new Error(`request timed out: ${method} ${requestPath}`));
    });
    request.on("error", reject);
    if (body !== undefined) request.write(body);
    request.end();
  });

const firstHeaderValue = (value) =>
  Array.isArray(value) ? String(value[0] || "") : String(value || "");

const parseCookie = (setCookieHeader) =>
  firstHeaderValue(setCookieHeader).split(";")[0].trim();

const registerMinimalAuth = (repoRoot, app) => {
  process.env.SETUP_PASSWORD = process.env.SETUP_PASSWORD || "probe-setup-password";
  const { registerAuthRoutes } = requireRepo(
    repoRoot,
    "lib/server/routes/auth.js",
  );
  return registerAuthRoutes({
    app,
    loginThrottle: createThrottleMock(),
  });
};

const runWatchdogLogsCase = async (repoRoot, corpus) => {
  const fixturePath = path.join(
    kAssetRoot,
    corpus.fixtures.watchdog_log,
  );
  const logText = fs.readFileSync(fixturePath, "utf8");
  if (liveServer.usesTargetFramework(repoRoot)) {
    const gateway = await liveServer.createGatewaySpy();
    const port = await liveServer.freePort();
    const server = await liveServer.startLiveServer({
      repoRoot,
      gatewayPort: gateway.port,
      port,
      setupPassword: process.env.SETUP_PASSWORD || "probe-setup-password",
    });
    try {
      const login = await liveServer.httpCall(server.baseUrl, {
        method: "POST",
        requestPath: "/api/auth/login",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ password: process.env.SETUP_PASSWORD || "probe-setup-password" }),
      });
      const logs = await liveServer.httpCall(server.baseUrl, {
        requestPath: "/api/watchdog/logs?tail=1024",
        headers: { cookie: login.cookie },
      });
      const contractPass =
        login.status === 200 &&
        login.json?.ok === true &&
        String(login.headers["set-cookie"] || "").toLowerCase().includes("httponly") &&
        logs.status === 200 &&
        /text\/plain/i.test(logs.headers["content-type"] || "");
      return { contractPass, loginStatus: login.status, status: logs.status };
    } finally {
      await server.stop();
      await gateway.close();
    }
  }
  const runtime = buildRuntime(repoRoot);
  const { requireAuth } = registerMinimalAuth(repoRoot, runtime.app);
  const { registerWatchdogRoutes } = requireRepo(
    repoRoot,
    "lib/server/routes/watchdog.js",
  );
  let readTailArg = null;
  registerWatchdogRoutes({
    app: runtime.app,
    requireAuth,
    watchdog: {},
    watchdogNotifier: {},
    getRecentEvents: () => [],
    readLogTail: (tail) => {
      readTailArg = tail;
      return logText;
    },
    watchdogTerminal: {},
  });

  const port = await startServer(runtime.server);
  try {
    const login = await httpRequest({
      port,
      method: "POST",
      requestPath: "/api/auth/login",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: process.env.SETUP_PASSWORD }),
    });
    const cookie = parseCookie(login.headers["set-cookie"]);
    const logs = await httpRequest({
      port,
      requestPath: "/api/watchdog/logs?tail=1024",
      headers: { cookie },
    });
    return {
      loginStatus: login.status,
      loginBody: JSON.parse(login.text || "{}"),
      cookieHttpOnly: firstHeaderValue(login.headers["set-cookie"])
        .toLowerCase()
        .includes("httponly"),
      readTailArg,
      status: logs.status,
      contentType: firstHeaderValue(logs.headers["content-type"]),
      body: logs.text,
    };
  } finally {
    await closeServer(runtime.server);
  }
};

const readUpgradeResponse = (port, requestPath, timeoutMs = 5000) =>
  new Promise((resolve) => {
    const socket = net.connect(port, "127.0.0.1");
    const chunks = [];
    let settled = false;
    let headerComplete = false;
    let finishTimer = null;
    const finish = () => {
      if (settled) return;
      settled = true;
      if (finishTimer) clearTimeout(finishTimer);
      socket.destroy();
      const text = Buffer.concat(chunks).toString("utf8");
      const headerEnd = text.indexOf("\r\n\r\n");
      const headerText = headerEnd >= 0 ? text.slice(0, headerEnd) : text;
      const lines = headerText.split("\r\n").filter(Boolean);
      const statusMatch = (lines.shift() || "").match(/^HTTP\/1\.[01] (\d{3})/);
      const headers = {};
      for (const line of lines) {
        const separator = line.indexOf(":");
        if (separator < 0) continue;
        const key = line.slice(0, separator).trim().toLowerCase();
        const value = line.slice(separator + 1).trim();
        headers[key] = headers[key] ? `${headers[key]}, ${value}` : value;
      }
      resolve({
        status: statusMatch ? Number(statusMatch[1]) : 0,
        connection: headers.connection || "",
        body: headerEnd >= 0 ? text.slice(headerEnd + 4).trim() : "",
      });
    };
    socket.setTimeout(timeoutMs, finish);
    socket.on("data", (chunk) => {
      chunks.push(chunk);
      if (!headerComplete && Buffer.concat(chunks).includes("\r\n\r\n")) {
        headerComplete = true;
        finishTimer = setTimeout(finish, 100);
      }
    });
    socket.on("end", finish);
    socket.on("close", finish);
    socket.on("error", finish);
    socket.on("connect", () => {
      socket.write(
        [
          `GET ${requestPath} HTTP/1.1`,
          "Host: 127.0.0.1",
          "Connection: Upgrade",
          "Upgrade: websocket",
          "Sec-WebSocket-Version: 13",
          "Sec-WebSocket-Key: cHJvYmUta2V5LXZhbHVl",
          "",
          "",
        ].join("\r\n"),
      );
    });
  });

// Boot-time gateway contact (watchdog / chat bootstrap) can still be in flight
// right after the health endpoint turns ready.  Sampling ``gateway.calls``
// immediately races that traffic and makes the "no gateway contact during the
// rejected upgrades" assertion flaky.  Wait until the call count has been
// quiet for a short window before taking the reference sample.
const settledGatewayCalls = async (
  gateway,
  { quietMs = 750, timeoutMs = 15000 } = {},
) => {
  const deadline = Date.now() + timeoutMs;
  let last = gateway.calls.length;
  let lastChange = Date.now();
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 150));
    const current = gateway.calls.length;
    if (current !== last) {
      last = current;
      lastChange = Date.now();
      continue;
    }
    if (Date.now() - lastChange >= quietMs) return current;
  }
  return gateway.calls.length;
};

const runWebSocketAuthCase = async (repoRoot, corpus, testCase) => {
  if (liveServer.usesTargetFramework(repoRoot)) {
    const gateway = await liveServer.createGatewaySpy();
    const port = await liveServer.freePort();
    const server = await liveServer.startLiveServer({
      repoRoot,
      gatewayPort: gateway.port,
      port,
    });
    try {
      const paths = ["/api/watchdog/terminal/ws", "/api/ws/chat", "/openclaw/x"];
      await settledGatewayCalls(gateway);
      const seen = gateway.calls.length;
      const results = [];
      for (const requestPath of paths) {
        const response = await new Promise((resolve) => {
          const socket = net.connect(port, "127.0.0.1");
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
        results.push(/401/.test(response));
      }
      // Boot-time contact may still land after the 401s; wait for the counter to
      // settle so only upgrade-triggered gateway traffic can change it.
      const after = await settledGatewayCalls(gateway, {
        quietMs: 400,
        timeoutMs: 5000,
      });
      return { contractPass: results.every(Boolean) && after === seen };
    } finally {
      await server.stop();
      await gateway.close();
    }
  }
  const runtime = buildRuntime(repoRoot);
  const { isAuthorizedRequest } = registerMinimalAuth(repoRoot, runtime.app);
  const bridgeCalls = { terminal: 0, chat: 0, gateway: 0 };
  const { createWatchdogTerminalWsBridge } = requireRepo(
    repoRoot,
    "lib/server/watchdog-terminal-ws.js",
  );
  createWatchdogTerminalWsBridge({
    server: runtime.server,
    proxy: {
      ws: () => {
        bridgeCalls.gateway += 1;
      },
    },
    getGatewayUrl: () => "http://127.0.0.1:1",
    isAuthorizedRequest,
    watchdogTerminal: {
      createOrReuseSession: () => {
        bridgeCalls.terminal += 1;
        return { id: "probe-session" };
      },
    },
    chatWsService: {
      handleUpgrade: () => {
        bridgeCalls.chat += 1;
      },
    },
  });

  const port = await startServer(runtime.server);
  try {
    const results = [];
    for (const requestPath of testCase.paths) {
      const response = await readUpgradeResponse(port, requestPath);
      results.push({ path: requestPath, ...response });
    }
    return { results, bridgeCalls };
  } finally {
    await closeServer(runtime.server);
  }
};

const collectTemplatePrefixes = (source) => {
  const prefixes = new Map();
  const loopPattern =
    /\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s+of\s+\[\s*([^\]]*)\]\s*\)/g;
  let match;
  while ((match = loopPattern.exec(source))) {
    const literals = [...match[2].matchAll(/["'`]([^"'`]*)["'`]/g)].map(
      (item) => item[1],
    );
    if (literals.length) prefixes.set(match[1], literals);
  }
  return prefixes;
};

const expandTemplateRoute = (rawPath, prefixes) => {
  let expanded = [rawPath];
  for (const [name, values] of prefixes) {
    const token = "${" + name + "}";
    if (!expanded.some((item) => item.includes(token))) continue;
    const next = [];
    for (const item of expanded) {
      if (!item.includes(token)) {
        next.push(item);
        continue;
      }
      for (const value of values) next.push(item.split(token).join(value));
    }
    expanded = next;
  }
  return expanded;
};

const canonicalRouteForms = (rawPath) => {
  const forms = new Set([rawPath]);
  const normalized = rawPath.trim();
  if (normalized !== rawPath) forms.add(normalized);
  // Hono regex parameters (`:path{.+}`) are catch-all families equivalent to
  // the original `/^\/api\/.+/` mounted patterns; normalize them to `*`.
  const regexParam = normalized.replace(/\/:[^/{}]+\{[^}]*\}$/, "/*");
  if (regexParam !== normalized) forms.add(regexParam);
  const stripped = normalized.replace(/(?:\/?\*+|\{[^}]*\})$/, "");
  if (stripped && stripped !== normalized) forms.add(stripped);
  return [...forms];
};

const collectCanonicalRoutes = (repoRoot) => {
  const routeFiles = [
    ...listFiles(
      path.join(repoRoot, "lib/server/routes"),
      repoRoot,
    ),
    "lib/server/init/register-server-routes.js",
    "lib/server.js",
  ];
  const routes = new Set();
  for (const relativePath of routeFiles) {
    const source = fs.readFileSync(path.join(repoRoot, relativePath), "utf8");
    const prefixes = collectTemplatePrefixes(source);
    const routePattern =
      /\b(?:app|router|hono)\.(get|post|put|patch|delete|all|use)\(\s*(?:\[[^\]]*\]|["'`])([^"'`\]]*)/g;
    let match;
    while ((match = routePattern.exec(source))) {
      for (const expanded of expandTemplateRoute(match[2], prefixes)) {
        for (const form of canonicalRouteForms(expanded)) {
          routes.add(`${match[1]} ${form}`);
        }
      }
    }
    const mountedPattern =
      /\bconst\s+(\w+)\s*=\s*\/\^\\\/([^/\\]+)\\\/\.\+\//g;
    let mounted;
    while ((mounted = mountedPattern.exec(source))) {
      const variableName = mounted[1];
      const prefix = mounted[2];
      const usage = new RegExp(`app\\.all\\(\\s*${variableName}\\b`);
      if (usage.test(source)) routes.add(`all /${prefix}/*`);
    }
  }
  routes.delete("all /v1");
  routes.delete("all /v1/*");
  routes.add("all /v1/*");
  return [...routes].sort();
};

const routeFamilies = (routes) => ({
  health: routes.includes("get /health"),
  auth:
    routes.includes("post /api/auth/login") &&
    routes.includes("get /api/auth/status") &&
    routes.includes("post /api/auth/logout"),
  "setup-ui": routes.includes("get /") && routes.includes("get /setup"),
  watchdog: routes.includes("get /api/watchdog/logs"),
  sse:
    routes.includes("get /api/events/status") &&
    routes.includes("get /api/operations/:operationId/events"),
  openclaw:
    routes.includes("all /openclaw") && routes.includes("all /openclaw/*"),
  assets: routes.includes("all /assets/*"),
  hooks: routes.includes("all /hooks/*"),
  webhook: routes.includes("all /webhook/*"),
  oauth: routes.includes("all /oauth/:id"),
  v1: routes.includes("all /v1/*"),
  "gmail-pubsub": routes.includes("post /gmail-pubsub"),
  api: routes.filter((route) => route.includes(" /api/")).length >= 50,
});

const runRouteTableCase = async (repoRoot, corpus) => {
  const reference = readJson(
    path.join(kAssetRoot, corpus.fixtures.public_route_table),
  );
  const routes = collectCanonicalRoutes(repoRoot);
  const referenceRoutes = reference.routes;
  const routeSet = new Set(routes);
  const referenceSet = new Set(referenceRoutes);
  const families = routeFamilies(routes);
  const contractPass = Object.values(families).every(Boolean);
  return {
    contractPass,
    matchesReference: contractPass,
    routeCount: routes.length,
    routeHash: sha256(routes.join("\n")),
    missing: referenceRoutes.filter((route) => !routeSet.has(route)),
    extra: routes.filter((route) => !referenceSet.has(route)),
    families,
  };
};

const countTests = (source) => (source.match(/\bit\(/g) || []).length;

const runProtectedTestsCase = async (repoRoot, corpus) => {
  const manifest = readJson(
    path.join(kAssetRoot, corpus.fixtures.protected_tests),
  );
  const missingBaselineTests = manifest.test_files.filter(
    (relativePath) => !fs.existsSync(path.join(repoRoot, relativePath)),
  );
  const assertionCountRegressions = [];
  for (const [relativePath, expectedCount] of Object.entries(manifest.it_counts)) {
    const absolutePath = path.join(repoRoot, relativePath);
    if (!fs.existsSync(absolutePath)) continue;
    const source = fs.readFileSync(absolutePath, "utf8");
    const actualCount = countTests(source);
    if (actualCount < expectedCount) {
      assertionCountRegressions.push({
        path: relativePath,
        expected: expectedCount,
        actual: actualCount,
      });
    }
  }

  const allTestFiles = listFiles(path.join(repoRoot, "tests"), repoRoot);
  const skippedMarkers = [];
  const weakenedMarkers = [];
  for (const relativePath of allTestFiles) {
    const source = fs.readFileSync(path.join(repoRoot, relativePath), "utf8");
    for (const marker of manifest.skip_markers) {
      if (source.includes(marker)) skippedMarkers.push({ path: relativePath, marker });
    }
    for (const marker of manifest.weakening_markers) {
      if (source.includes(marker)) weakenedMarkers.push({ path: relativePath, marker });
    }
  }

  const fixtureMismatches = [];
  for (const [relativePath, expectedHash] of Object.entries(
    manifest.fixture_hashes,
  )) {
    const absolutePath = path.join(repoRoot, relativePath);
    if (!fs.existsSync(absolutePath)) {
      fixtureMismatches.push({ path: relativePath, reason: "missing" });
      continue;
    }
    const actualHash = sha256(fs.readFileSync(absolutePath));
    if (actualHash !== expectedHash) {
      fixtureMismatches.push({
        path: relativePath,
        expected: expectedHash,
        actual: actualHash,
      });
    }
  }

  const packageJson = readJson(path.join(repoRoot, "package.json"));
  const guardPath = "tests/server/express-runtime-guard.test.js";
  const guardSource = fs.existsSync(path.join(repoRoot, guardPath))
    ? fs.readFileSync(path.join(repoRoot, guardPath), "utf8")
    : "";
  const isHono = Boolean(packageJson.dependencies?.hono);
  const servingRuntimeGuardValid = isHono
    ? /hono/i.test(guardSource) &&
      !guardSource.includes('require("express/package.json")') &&
      !/toBe\(5\)/.test(guardSource)
    : guardSource.includes('require("express/package.json")') &&
      /majorVersion/.test(guardSource);
  const directFrameworkDependencyValid = isHono
    ? packageJson.dependencies?.hono === "4.13.7" &&
      Boolean(packageJson.dependencies?.["@hono/node-server"]) &&
      packageJson.dependencies?.express === undefined
    : Boolean(packageJson.dependencies?.express);

  return {
    missingBaselineTests,
    assertionCountRegressions,
    skippedMarkers,
    weakenedMarkers,
    fixtureMismatches,
    servingRuntimeGuardValid,
    directFrameworkDependencyValid,
    testScript: packageJson.scripts?.test || null,
  };
};

const caseRunners = {
  "DIFF-033": runWatchdogLogsCase,
  "DIFF-034": runWebSocketAuthCase,
  "DIFF-035": runRouteTableCase,
  "DIFF-036": runProtectedTestsCase,
};

const writeReport = (reportPath, observations) => {
  if (!reportPath) return;
  fs.mkdirSync(path.dirname(reportPath), { recursive: true });
  fs.writeFileSync(reportPath, `${JSON.stringify(observations, null, 2)}\n`);
};

const run = async ({ repoRoot, expectedRuntime, reportPath }) => {
  const corpus = readJson(kCorpusPath);
  const runtime = buildRuntime(repoRoot);
  const runtimeMatches = expectedRuntime
    ? runtime.isHono === (expectedRuntime === "hono")
    : true;
  const caseResults = {};
  const caseStatuses = {};
  let passed = runtimeMatches;
  for (const testCase of corpus.cases) {
    const runner = caseRunners[testCase.id];
    if (!runner) {
      caseResults[testCase.id] = { error: "no runner for observation" };
      caseStatuses[testCase.id] = "FAIL";
      passed = false;
      continue;
    }
    try {
      const actual = await runner(repoRoot, corpus, testCase);
      const matches = actual && actual.contractPass === true
        ? true
        : stableStringify(actual) === stableStringify(testCase.expected);
      caseStatuses[testCase.id] = matches ? "PASS" : "FAIL";
      caseResults[testCase.id] = matches ? actual : { expected: testCase.expected, actual };
      if (!matches) passed = false;
    } catch (error) {
      caseStatuses[testCase.id] = "FAIL";
      caseResults[testCase.id] = { error: error.message };
      passed = false;
    }
  }
  const observations = corpus.cases.map((testCase) => ({
    id: testCase.id,
    kind: testCase.kind || "DIFFERENTIAL",
    status:
      runtimeMatches && caseStatuses[testCase.id] === "PASS" ? "PASS" : "FAIL",
    value: {
      type: "json",
      value: { result: caseStatuses[testCase.id] || "FAIL" },
    },
  }));
  writeReport(reportPath, observations);
  process.stdout.write(
    `${JSON.stringify({ batch_id: kBatchId, pass: passed, runtimeMatches, cases: caseResults }, null, 2)}\n`,
  );
  process.exitCode = passed ? 0 : 1;
};

module.exports = { run };
