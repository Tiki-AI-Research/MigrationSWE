"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const net = require("net");
const Module = require("module");
const childProcess = require("child_process");

const kStdoutWrite = process.stdout.write.bind(process.stdout);

const kSetupPassword = "secret";
const kFiveMegabytes = 5 * 1024 * 1024;
const kWebhookFixturePath = path.resolve(
  __dirname,
  "..",
  "..",
  "..",
  "fixtures",
  "batches",
  "BATCH-F2P-0003",
  "webhook-body.json",
);

const kHttpLayerEntries = ["lib", "bin"];

const kExpressSpecifierPattern = /\bexpress\b/i;
const kExpressUsagePattern =
  /(?:\bexpress\s*\(\s*\)|\bexpress\s*\.\s*(?:json|static|raw|Router|urlencoded)\s*\()/;
const kHonoImportPattern =
  /(?:require\s*\(\s*["']hono["']\s*\)|from\s+["']hono["']|import\s*\(\s*["']hono["']\s*\))/;
const kNodeAdapterPattern =
  /(?:@hono\/node-server|getRequestListener|createAdaptorServer)/;

const kStringLiteral =
  /'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`/g;

const unquoteLiteral = (token) => token.slice(1, -1).replace(/\\(['"`\\])/g, "$1");

const maskSourceStrings = (source) =>
  source.replace(
    /'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`/g,
    (token) => (token.length >= 2 ? token[0] + token[token.length - 1] : token),
  );

const extractSpecifierExpressions = (source) => {
  const expressions = [];
  const callPattern = /\b(?:require|import)\s*\(/g;
  let match;
  while ((match = callPattern.exec(source))) {
    const start = callPattern.lastIndex - 1;
    let depth = 0;
    let quote = "";
    let index = start;
    while (index < source.length) {
      const character = source[index];
      if (quote) {
        if (character === "\\") {
          index += 2;
          continue;
        }
        if (character === quote) quote = "";
        index += 1;
        continue;
      }
      if (character === "'" || character === '"' || character === "`") {
        quote = character;
        index += 1;
        continue;
      }
      if (character === "(") depth += 1;
      else if (character === ")") {
        depth -= 1;
        if (depth === 0) {
          expressions.push(source.slice(start + 1, index));
          break;
        }
      }
      index += 1;
    }
  }
  const fromPattern = /\bfrom\s*(['"])((?:\\.|[^'\\])*)\1/g;
  while ((match = fromPattern.exec(source))) expressions.push(match[0]);
  return expressions;
};

const foldedConstantStrings = (fragment) => {
  const values = [];
  for (const token of fragment.match(kStringLiteral) || []) {
    values.push(unquoteLiteral(token));
  }
  const arrayJoin = /\[\s*([^\]]*?)\s*\]\s*\.\s*join\s*\(([^)]*)\)/g;
  let match;
  while ((match = arrayJoin.exec(fragment))) {
    const parts = (match[1].match(kStringLiteral) || []).map(unquoteLiteral);
    const separator = (match[2].match(kStringLiteral) || [""]).map(unquoteLiteral)[0];
    if (parts.length) values.push(parts.join(separator || ""));
  }
  const concatChain =
    /(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")(?:\s*\+\s*(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"))+/g;
  while ((match = concatChain.exec(fragment))) {
    const parts = (match[0].match(kStringLiteral) || []).map(unquoteLiteral);
    if (parts.length >= 2) values.push(parts.join(""));
  }
  const fromCharCode = /String\s*\.\s*fromCharCode\s*\(([^()]*)\)/g;
  while ((match = fromCharCode.exec(fragment))) {
    const codes = (match[1].match(/\d+/g) || []).map(Number);
    if (codes.length && codes.every((code) => code > 0 && code < 0x110000)) {
      values.push(String.fromCharCode(...codes));
    }
  }
  const bufferFrom =
    /Buffer\s*\.\s*from\s*\(\s*(['"])([A-Za-z0-9+/=_-]+)\1\s*,\s*(['"])(base64|base64url|hex)\3\s*\)\s*\.\s*toString\s*\(/g;
  while ((match = bufferFrom.exec(fragment))) {
    try {
      values.push(Buffer.from(match[2], match[4]).toString("utf8"));
    } catch {
      continue;
    }
  }
  return values;
};

const sourceUsesExpress = (source) => {
  for (const expression of extractSpecifierExpressions(source)) {
    if (
      foldedConstantStrings(expression).some((value) =>
        kExpressSpecifierPattern.test(value),
      )
    ) {
      return true;
    }
  }
  return kExpressUsagePattern.test(maskSourceStrings(source));
};

const resolveRepoRoot = () => {
  const fromArgv = process.argv[2];
  if (fromArgv) {
    const candidate = path.resolve(fromArgv);
    if (fs.existsSync(path.join(candidate, "package.json"))) return candidate;
  }
  if (process.env.REPO_ROOT) {
    const candidate = path.resolve(process.env.REPO_ROOT);
    if (fs.existsSync(path.join(candidate, "package.json"))) return candidate;
  }
  return process.cwd();
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const getFreePort = () =>
  new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });

const request = (port, options = {}) => {
  const {
    method = "GET",
    path: requestPath = "/",
    headers = {},
    body = null,
    timeoutMs = 20000,
  } = options;

  return new Promise((resolve) => {
    const req = http.request(
      {
        host: "127.0.0.1",
        port,
        method,
        path: requestPath,
        headers,
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () =>
          resolve({
            status: res.statusCode,
            headers: res.headers,
            body: Buffer.concat(chunks),
          }),
        );
      },
    );

    req.setTimeout(timeoutMs, () => {
      req.destroy(new Error("request timed out"));
    });
    req.on("error", (error) =>
      resolve({
        status: 0,
        headers: {},
        body: Buffer.alloc(0),
        error: error.message,
      }),
    );
    if (body !== null && body !== undefined) req.write(body);
    req.end();
  });
};

const toText = (response) => response.body.toString("utf8");

const parseJsonBody = (response) => {
  try {
    return JSON.parse(toText(response));
  } catch {
    return null;
  }
};

const collectJavaScriptFiles = (repo, relativePath) => {
  const absolutePath = path.join(repo, relativePath);
  if (!fs.existsSync(absolutePath)) return [];
  const stat = fs.statSync(absolutePath);
  if (stat.isFile()) return [relativePath];
  if (!stat.isDirectory()) return [];

  const collected = [];
  for (const entry of fs.readdirSync(absolutePath, { withFileTypes: true })) {
    const child = path.join(relativePath, entry.name);
    if (entry.isDirectory()) {
      collected.push(...collectJavaScriptFiles(repo, child));
    } else if (entry.isFile() && entry.name.endsWith(".js")) {
      collected.push(child);
    }
  }
  return collected;
};

const scanHttpLayer = (repo) => {
  const files = kHttpLayerEntries.flatMap((entry) =>
    collectJavaScriptFiles(repo, entry),
  );
  const uniqueFiles = Array.from(new Set(files));
  const offenders = [];
  const honoFiles = [];
  const nodeAdapterFiles = [];

  for (const relativePath of uniqueFiles) {
    const source = fs.readFileSync(path.join(repo, relativePath), "utf8");
    if (sourceUsesExpress(source)) {
      offenders.push(relativePath);
    }
    if (kHonoImportPattern.test(source)) honoFiles.push(relativePath);
    if (kNodeAdapterPattern.test(source)) nodeAdapterFiles.push(relativePath);
  }

  return {
    files: uniqueFiles,
    expressOffenders: offenders,
    honoFiles,
    nodeAdapterFiles,
  };
};

const computeRuntime = (repo, loadedModules) => {
  const scan = scanHttpLayer(repo);
  const loaded = Array.from(loadedModules);
  const honoLoaded = loaded.some(
    (requested) => requested === "hono" || requested.startsWith("hono/"),
  );
  const nodeAdapterLoaded = loaded.some(
    (requested) =>
      requested === "@hono/node-server" ||
      requested.startsWith("@hono/node-server/"),
  );
  const usesHono = honoLoaded || scan.honoFiles.length > 0;
  const usesNodeAdapter =
    nodeAdapterLoaded || scan.nodeAdapterFiles.length > 0;
  const honoVersion = resolvePackageVersion(repo, "hono");
  const honoVersionPinned = honoVersion === "4.13.7";
  return {
    loadedExpress: loaded.filter((requested) =>
      requested === "express" || requested.startsWith("express/"),
    ),
    honoLoaded,
    nodeAdapterLoaded,
    honoFiles: scan.honoFiles,
    nodeAdapterFiles: scan.nodeAdapterFiles,
    expressOffenders: scan.expressOffenders,
    scannedFiles: scan.files,
    usesHono,
    usesNodeAdapter,
    honoVersion,
    honoVersionPinned,
    noExpressInHttpLayer: scan.expressOffenders.length === 0,
    targetRuntime:
      usesHono && honoVersionPinned && scan.expressOffenders.length === 0,
  };
};

const stripSourceComments = (text) => {
  const characters = text.split("");
  let state = "normal";
  let quote = "";
  let index = 0;
  while (index < text.length) {
    const character = text[index];
    if (state === "normal") {
      if (character === "/" && text[index + 1] === "/") {
        while (index < text.length && text[index] !== "\n") {
          characters[index] = " ";
          index += 1;
        }
        continue;
      }
      if (character === "/" && text[index + 1] === "*") {
        characters[index] = " ";
        characters[index + 1] = " ";
        index += 2;
        while (
          index < text.length &&
          !(text[index] === "*" && text[index + 1] === "/")
        ) {
          if (text[index] !== "\n") characters[index] = " ";
          index += 1;
        }
        if (index < text.length) characters[index] = " ";
        index += 1;
        if (index < text.length) characters[index] = " ";
        index += 1;
        continue;
      }
      if (character === "'" || character === '"' || character === "`") {
        state = "string";
        quote = character;
        index += 1;
        continue;
      }
      index += 1;
      continue;
    }
    if (character === "\\") {
      index += 2;
      continue;
    }
    if (character === quote) state = "normal";
    index += 1;
  }
  return characters.join("");
};

const resolveLocalModule = (root, baseRelative) => {
  const candidates = [
    baseRelative,
    `${baseRelative}.js`,
    `${baseRelative}.cjs`,
    `${baseRelative}.mjs`,
    `${baseRelative}/index.js`,
    `${baseRelative}/index.cjs`,
    `${baseRelative}/index.mjs`,
  ];
  for (const candidate of candidates) {
    try {
      if (fs.statSync(path.join(root, candidate)).isFile()) return candidate;
    } catch {
      continue;
    }
  }
  return null;
};

// The serving-runtime check must follow the modules the production entry
// actually loads (comment-stripped), not a single file, so equivalent adapter
// wiring in a helper module passes while decoy modules outside the reachable
// graph do not.
const reachableModuleSources = (repo, entry = "lib/server.js") => {
  const root = path.resolve(repo);
  const seen = new Set();
  const queue = [entry];
  const sources = [];
  while (queue.length && seen.size < 400) {
    const relative = queue.shift();
    if (seen.has(relative)) continue;
    seen.add(relative);
    let source;
    try {
      source = fs.readFileSync(path.join(root, relative), "utf8");
    } catch {
      continue;
    }
    const stripped = stripSourceComments(source);
    sources.push(stripped);
    const specifierPattern =
      /\brequire\s*\(\s*["']([^"']+)["']\s*\)|\bfrom\s*["']([^"']+)["']|\bimport\s*\(\s*["']([^"']+)["']\s*\)/g;
    let match;
    while ((match = specifierPattern.exec(stripped))) {
      const specifier = match[1] || match[2] || match[3];
      if (!specifier || !specifier.startsWith(".")) continue;
      const base = path.posix.join(path.posix.dirname(relative), specifier);
      const resolved = resolveLocalModule(root, base);
      if (resolved) queue.push(resolved);
    }
  }
  return sources.join("\n");
};

const startMockGateway = () =>
  new Promise((resolve, reject) => {
    const records = [];
    const server = http.createServer((req, res) => {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        const body = Buffer.concat(chunks);
        records.push({
          method: req.method,
          url: req.url,
          headers: req.headers,
          body,
        });
        const payload = Buffer.from(
          JSON.stringify({ ok: true, gatewayPath: req.url }),
          "utf8",
        );
        res.writeHead(200, {
          "content-type": "application/json; charset=utf-8",
          "content-length": String(payload.length),
          connection: "close",
          "x-mock-gateway": "yes",
        });
        res.end(payload);
      });
    });
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () =>
      resolve({
        server,
        records,
        port: server.address().port,
      }),
    );
  });

const createRoot = (gatewayPort) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "alphaclaw-f2p-"));
  const openclawDir = path.join(root, ".openclaw");
  fs.mkdirSync(openclawDir, { recursive: true });
  fs.mkdirSync(path.join(openclawDir, "workspace"), { recursive: true });
  fs.writeFileSync(
    path.join(openclawDir, "openclaw.json"),
    JSON.stringify(
      {
        gateway: {
          port: gatewayPort,
          auth: { token: "f2p-gateway-token" },
        },
      },
      null,
      2,
    ),
  );
  return root;
};

const waitForHealth = async (port) => {
  const deadline = Date.now() + 25000;
  let last = null;
  while (Date.now() < deadline) {
    const response = await request(port, {
      path: "/health",
      timeoutMs: 2000,
    });
    last = response;
    if (response.status === 200) return response;
    await sleep(100);
  }
  return last || { status: 0, headers: {}, body: Buffer.alloc(0) };
};

const patchChildProcess = (calls) => {
  for (const name of ["spawn", "exec", "execFile", "fork"]) {
    const original = childProcess[name];
    if (typeof original !== "function") continue;
    childProcess[name] = function patchedChildProcess(...args) {
      calls.push({
        api: name,
        command: String(args[0] === undefined ? "" : args[0]),
        args: args[1],
        options: args[2],
      });
      return original.apply(this, args);
    };
  }
};

const withServer = async (repo, run) => {
  const mock = await startMockGateway();
  const port = await getFreePort();
  const root = createRoot(mock.port);
  const loadedModules = new Set();
  const spawnCalls = [];
  const originalLoad = Module._load;
  const originalLog = console.log;
  const originalInfo = console.info;
  const originalDebug = console.debug;

  process.env.PORT = String(port);
  process.env.SETUP_PASSWORD = kSetupPassword;
  process.env.ALPHACLAW_ROOT_DIR = root;
  process.env.TRUST_PROXY_HOPS = "0";

  Module._load = function recordingLoad(requested, parent, isMain) {
    loadedModules.add(String(requested));
    return originalLoad.call(this, requested, parent, isMain);
  };
  patchChildProcess(spawnCalls);
  console.log = () => {};
  console.info = () => {};
  console.debug = () => {};

  try {
    require(path.join(repo, "lib", "server.js"));
  } finally {
    Module._load = originalLoad;
    console.log = originalLog;
    console.info = originalInfo;
    console.debug = originalDebug;
  }

  console.log = () => {};
  console.info = () => {};
  console.debug = () => {};

  try {
    const health = await waitForHealth(port);
    return await run({
      repo,
      port,
      root,
      mock,
      loadedModules,
      spawnCalls,
      health,
    });
  } finally {
    try {
      mock.server.closeAllConnections?.();
    } catch {}
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 2000);
      mock.server.close(() => {
        clearTimeout(timer);
        resolve();
      });
    });
    try {
      fs.rmSync(root, { recursive: true, force: true });
    } catch {}
  }
};

const performLogin = async (port) => {
  const payload = Buffer.from(
    JSON.stringify({ password: kSetupPassword }),
    "utf8",
  );
  const response = await request(port, {
    method: "POST",
    path: "/api/auth/login",
    headers: {
      "content-type": "application/json",
      "content-length": String(payload.length),
    },
    body: payload,
  });
  const setCookie = response.headers["set-cookie"] || [];
  const cookie = String(setCookie[0] || "").split(";")[0];
  return { response, cookie, setCookie };
};

const readSseEvents = (port, cookie, options = {}) => {
  const { minEvents = 2, timeoutMs = 7000 } = options;
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    let req = null;

    const finish = (result) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try {
        req?.destroy();
      } catch {}
      resolve(result);
    };

    req = http.get(
      {
        host: "127.0.0.1",
        port,
        path: "/api/events/status",
        headers: {
          cookie,
          accept: "text/event-stream",
        },
      },
      (res) => {
        let text = "";
        res.on("data", (chunk) => {
          text += chunk.toString("utf8");
          const eventCount = (text.match(/event:\s*status/g) || []).length;
          if (eventCount >= minEvents) {
            finish({
              status: res.statusCode,
              headers: res.headers,
              text,
              eventCount,
            });
          }
        });
        res.on("end", () =>
          finish({
            status: res.statusCode,
            headers: res.headers,
            text,
            eventCount: (text.match(/event:\s*status/g) || []).length,
          }),
        );
      },
    );
    req.on("error", (error) =>
      finish({ status: 0, headers: {}, text: "", error: error.message }),
    );
    timer = setTimeout(
      () =>
        finish({
          status: 0,
          headers: {},
          text: "",
          error: "sse read timed out",
        }),
      timeoutMs,
    );
  });
};

const rawUpgrade = (port, requestPath, headers = {}) =>
  new Promise((resolve) => {
    const socket = net.connect(port, "127.0.0.1");
    let text = "";
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      try {
        socket.destroy();
      } catch {}
      resolve(result);
    };
    socket.setTimeout(6000, () =>
      finish({ text, error: "upgrade socket timed out" }),
    );
    socket.on("error", (error) => finish({ text, error: error.message }));
    socket.on("data", (chunk) => {
      text += chunk.toString("utf8");
      if (text.includes("\r\n\r\n")) finish({ text });
    });
    socket.on("connect", () => {
      const extraHeaders = Object.entries(headers)
        .map(([key, value]) => `${key}: ${value}`)
        .join("\r\n");
      socket.write(
        [
          `GET ${requestPath} HTTP/1.1`,
          `Host: 127.0.0.1:${port}`,
          "Connection: Upgrade",
          "Upgrade: websocket",
          "Sec-WebSocket-Version: 13",
          "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
          extraHeaders,
          "",
          "",
        ].join("\r\n"),
      );
    });
  });

function resolvePackageVersion(repo, packageName) {
  const readNearestPackageJson = (startPath) => {
    let current = startPath;
    while (current && current !== path.dirname(current)) {
      const candidate = path.join(current, "package.json");
      if (fs.existsSync(candidate)) {
        try {
          const parsed = JSON.parse(fs.readFileSync(candidate, "utf8"));
          if (parsed.name === packageName) return parsed.version || null;
        } catch {}
      }
      current = path.dirname(current);
    }
    return null;
  };
  try {
    const entryPath = require.resolve(packageName, { paths: [repo] });
    const found = readNearestPackageJson(path.dirname(entryPath));
    if (found) return found;
  } catch {}
  for (const moduleRoot of String(process.env.NODE_PATH || "")
    .split(path.delimiter)
    .filter(Boolean)) {
    const found = readNearestPackageJson(path.join(moduleRoot, packageName));
    if (found) return found;
  }
  return null;
}

const isHealthPayload = (response) => {
  const body = parseJsonBody(response);
  return (
    response.status === 200 &&
    body !== null &&
    (body.status === "healthy" || body.status === "starting") &&
    (body.gateway === "running" || body.gateway === "starting")
  );
};

const isLoginPayload = (login) => {
  const body = parseJsonBody(login.response);
  return (
    login.response.status === 200 &&
    body !== null &&
    body.ok === true &&
    login.cookie.startsWith("setup_token=")
  );
};

const buildChecks = (entries) => {
  const checks = {};
  for (const [name, value] of Object.entries(entries)) {
    checks[name] = Boolean(value);
  }
  return checks;
};

const scenarioRuntimeGraph = async (repo) =>
  withServer(repo, async (context) => {
    const login = await performLogin(context.port);
    const runtime = computeRuntime(repo, context.loadedModules);
    return {
      runtime,
      details: {
        health: {
          status: context.health.status,
          body: toText(context.health),
        },
        login: {
          status: login.response.status,
          body: toText(login.response),
        },
      },
      checks: buildChecks({
        "health-200": isHealthPayload(context.health),
        "login-200": isLoginPayload(login),
        "hono-runtime": runtime.usesHono,
        "no-express-in-http-layer": runtime.noExpressInHttpLayer,
      }),
    };
  });

const scenarioServingRuntime = async (repo) =>
  withServer(repo, async (context) => {
    const login = await performLogin(context.port);
    const runtime = computeRuntime(repo, context.loadedModules);
    const reachableSource = reachableModuleSources(repo);
    const usesHonoNodeAdapter =
      runtime.usesNodeAdapter &&
      /@hono\/node-server/.test(reachableSource) &&
      /(?:createAdaptorServer|getRequestListener|createHonoNodeServer|createHonoApp|app\.fetch|\bserve\s*\()/.test(
        reachableSource,
      );
    const noExpressApplication = !sourceUsesExpress(reachableSource);

    return {
      runtime,
      details: {
        health: {
          status: context.health.status,
          body: toText(context.health),
        },
        login: {
          status: login.response.status,
          body: toText(login.response),
        },
        serverUsesHonoNodeAdapter: usesHonoNodeAdapter,
        serverHasExpressApplication: !noExpressApplication,
      },
      checks: buildChecks({
        "health-200": isHealthPayload(context.health),
        "login-200": isLoginPayload(login),
        "hono-node-serving-runtime": usesHonoNodeAdapter,
        "no-express-application": noExpressApplication,
        "no-express-in-http-layer": runtime.noExpressInHttpLayer,
      }),
    };
  });

const scenarioInProcess = async (repo) =>
  withServer(repo, async (context) => {
    const login = await performLogin(context.port);
    const runtime = computeRuntime(repo, context.loadedModules);
    const expressChild = context.spawnCalls.some((call) =>
      /express/i.test(call.command),
    );
    return {
      runtime,
      details: {
        pid: process.pid,
        health: {
          status: context.health.status,
          body: toText(context.health),
        },
        login: {
          status: login.response.status,
          body: toText(login.response),
        },
        childProcessCalls: context.spawnCalls,
      },
      checks: buildChecks({
        "health-200": isHealthPayload(context.health),
        "login-200": isLoginPayload(login),
        "hono-runtime": runtime.usesHono,
        "same-process-serving": isHealthPayload(context.health) && isLoginPayload(login),
        "no-express-child-process": !expressChild,
      }),
    };
  });

const scenarioHealthHono = async (repo) =>
  withServer(repo, async (context) => {
    const runtime = computeRuntime(repo, context.loadedModules);
    return {
      runtime,
      details: {
        health: {
          status: context.health.status,
          body: toText(context.health),
        },
      },
      checks: buildChecks({
        "health-200-json": isHealthPayload(context.health),
        "hono-runtime": runtime.usesHono,
        "no-express-in-http-layer": runtime.noExpressInHttpLayer,
      }),
    };
  });

const scenarioLoginBody = async (repo) =>
  withServer(repo, async (context) => {
    const login = await performLogin(context.port);
    const runtime = computeRuntime(repo, context.loadedModules);
    const oversized = Buffer.from(
      JSON.stringify({ data: "x".repeat(kFiveMegabytes) }),
      "utf8",
    );
    const oversizedResponse = await request(context.port, {
      method: "PUT",
      path: "/api/env",
      headers: {
        "content-type": "application/json",
        "content-length": String(oversized.length),
        cookie: login.cookie,
      },
      body: oversized,
      timeoutMs: 30000,
    });
    return {
      runtime,
      details: {
        login: {
          status: login.response.status,
          body: toText(login.response),
        },
        oversized: {
          status: oversizedResponse.status,
          error: oversizedResponse.error || null,
        },
      },
      checks: buildChecks({
        "login-json-200": isLoginPayload(login),
        "oversized-json-413": oversizedResponse.status === 413,
        "hono-body-middleware-runtime": runtime.targetRuntime,
      }),
    };
  });

const scenarioRawWebhook = async (repo) =>
  withServer(repo, async (context) => {
    const runtime = computeRuntime(repo, context.loadedModules);
    const rawBody = fs.readFileSync(kWebhookFixturePath);
    const recordsBefore = context.mock.records.length;
    const rawResponse = await request(context.port, {
      method: "POST",
      path: "/webhook/test",
      headers: {
        "content-type": "application/json",
        "content-length": String(rawBody.length),
      },
      body: rawBody,
      timeoutMs: 15000,
    });
    await sleep(150);
    const forwarded = context.mock.records
      .slice(recordsBefore)
      .find((record) => record.url === "/hooks/test");
    const forwardedBytesMatch = Boolean(
      forwarded && forwarded.body.equals(rawBody),
    );

    // GET hooks are still raw-body endpoints. Fetch Request drops GET bodies
    // unless a Node adapter explicitly preserves IncomingMessage's bytes.
    const recordsBeforeGet = context.mock.records.length;
    const getResponse = await request(context.port, {
      method: "GET",
      path: "/webhook/test",
      headers: {
        "content-type": "application/json",
        "content-length": String(rawBody.length),
      },
      body: rawBody,
      timeoutMs: 15000,
    });
    const forwardedGet = context.mock.records
      .slice(recordsBeforeGet)
      .find((record) => record.url === "/hooks/test");

    const oversized = Buffer.alloc(kFiveMegabytes + 1, 0x61);
    const recordsBeforeOversized = context.mock.records.length;
    const oversizedResponse = await request(context.port, {
      method: "POST",
      path: "/webhook/test",
      headers: {
        "content-type": "application/octet-stream",
        "content-length": String(oversized.length),
      },
      body: oversized,
      timeoutMs: 30000,
    });
    await sleep(200);
    const forwardedOversized = context.mock.records
      .slice(recordsBeforeOversized)
      .some((record) => record.url === "/hooks/test");

    return {
      runtime,
      details: {
        rawResponse: {
          status: rawResponse.status,
          error: rawResponse.error || null,
        },
        forwardedPath: forwarded?.url || null,
        forwardedBytesMatch,
        getResponse: { status: getResponse.status, error: getResponse.error || null },
        forwardedGetMethod: forwardedGet?.method || null,
        forwardedGetBytesMatch: Boolean(forwardedGet && forwardedGet.body.equals(rawBody)),
        oversized: {
          status: oversizedResponse.status,
          error: oversizedResponse.error || null,
        },
        oversizedForwarded: forwardedOversized,
      },
      checks: buildChecks({
        "raw-webhook-forwarded": rawResponse.status === 200 && Boolean(forwarded),
        "raw-webhook-path-rewritten": forwarded?.url === "/hooks/test",
        "raw-webhook-bytes-preserved": forwardedBytesMatch,
        "get-hook-forwarded-as-post": getResponse.status === 200 && forwardedGet?.method === "POST",
        "get-hook-bytes-preserved": Boolean(forwardedGet && forwardedGet.body.equals(rawBody)),
        "oversized-raw-413": oversizedResponse.status === 413,
        "oversized-raw-not-forwarded": !forwardedOversized,
        "hono-raw-body-runtime": runtime.targetRuntime,
      }),
    };
  });

const scenarioRawWebhookRuntimeGraph = async (repo) =>
  withServer(repo, async (context) => {
    const login = await performLogin(context.port);
    const runtime = computeRuntime(repo, context.loadedModules);
    const rawBody = fs.readFileSync(kWebhookFixturePath);
    const recordsBefore = context.mock.records.length;
    const rawResponse = await request(context.port, {
      method: "POST",
      path: "/webhook/test",
      headers: {
        "content-type": "application/json",
        "content-length": String(rawBody.length),
      },
      body: rawBody,
      timeoutMs: 15000,
    });
    await sleep(150);
    const forwarded = context.mock.records
      .slice(recordsBefore)
      .find((record) => record.url === "/hooks/test");
    const forwardedBytesMatch = Boolean(
      forwarded && forwarded.body.equals(rawBody),
    );

    const oversized = Buffer.alloc(kFiveMegabytes + 1, 0x61);
    const recordsBeforeOversized = context.mock.records.length;
    const oversizedResponse = await request(context.port, {
      method: "POST",
      path: "/webhook/test",
      headers: {
        "content-type": "application/octet-stream",
        "content-length": String(oversized.length),
      },
      body: oversized,
      timeoutMs: 30000,
    });
    await sleep(200);
    const forwardedOversized = context.mock.records
      .slice(recordsBeforeOversized)
      .some((record) => record.url === "/hooks/test");

    return {
      runtime,
      details: {
        health: {
          status: context.health.status,
          body: toText(context.health),
        },
        login: {
          status: login.response.status,
          body: toText(login.response),
        },
        rawResponse: {
          status: rawResponse.status,
          error: rawResponse.error || null,
        },
        forwardedPath: forwarded?.url || null,
        forwardedBytesMatch,
        oversized: {
          status: oversizedResponse.status,
          error: oversizedResponse.error || null,
        },
        oversizedForwarded: forwardedOversized,
      },
      checks: buildChecks({
        "health-200": isHealthPayload(context.health),
        "login-200": isLoginPayload(login),
        "raw-webhook-forwarded": rawResponse.status === 200 && Boolean(forwarded),
        "raw-webhook-path-rewritten": forwarded?.url === "/hooks/test",
        "raw-webhook-bytes-preserved": forwardedBytesMatch,
        "oversized-raw-413": oversizedResponse.status === 413,
        "oversized-raw-not-forwarded": !forwardedOversized,
        "hono-raw-body-runtime": runtime.targetRuntime,
        "no-express-in-http-layer": runtime.noExpressInHttpLayer,
      }),
    };
  });

const scenarioStaticFiles = async (repo) =>
  withServer(repo, async (context) => {
    const runtime = computeRuntime(repo, context.loadedModules);
    const login = await request(context.port, {
      path: "/login.html",
      timeoutMs: 10000,
    });
    const css = await request(context.port, {
      path: "/css/theme.css",
      timeoutMs: 10000,
    });
    const loginContentType = String(login.headers["content-type"] || "");
    const cssContentType = String(css.headers["content-type"] || "");
    const loginText = toText(login);
    return {
      runtime,
      details: {
        login: {
          status: login.status,
          contentType: loginContentType,
          bytes: login.body.length,
        },
        css: {
          status: css.status,
          contentType: cssContentType,
          bytes: css.body.length,
        },
      },
      checks: buildChecks({
        "login-html-public-200":
          login.status === 200 &&
          loginContentType.includes("text/html") &&
          /<html/i.test(loginText),
        "theme-css-public-200":
          css.status === 200 &&
          cssContentType.includes("text/css") &&
          css.body.length > 0,
        "hono-static-runtime": runtime.targetRuntime,
      }),
    };
  });

const scenarioSseStream = async (repo) =>
  withServer(repo, async (context) => {
    const login = await performLogin(context.port);
    const runtime = computeRuntime(repo, context.loadedModules);
    const stream = await readSseEvents(context.port, login.cookie, {
      minEvents: 2,
      timeoutMs: 8000,
    });
    const contentType = String(stream.headers["content-type"] || "");
    const cacheControl = String(stream.headers["cache-control"] || "");
    const accelBuffering = String(stream.headers["x-accel-buffering"] || "");
    const firstDataLine = String(stream.text || "")
      .split(/\r?\n/)
      .find((line) => line.startsWith("data: "));
    let firstPayload = null;
    if (firstDataLine) {
      try {
        firstPayload = JSON.parse(firstDataLine.slice("data: ".length));
      } catch {}
    }
    return {
      runtime,
      details: {
        status: stream.status,
        eventCount: stream.eventCount,
        contentType,
        cacheControl,
        accelBuffering,
        firstPayload,
        error: stream.error || null,
      },
      checks: buildChecks({
        "sse-status-200": stream.status === 200,
        "sse-content-type": contentType.includes("text/event-stream"),
        "sse-cache-headers":
          cacheControl.includes("no-cache") &&
          cacheControl.includes("no-transform") &&
          accelBuffering === "no",
        "sse-incremental-status-events": Number(stream.eventCount || 0) >= 2,
        "sse-status-payload-shape":
          firstPayload !== null &&
          Object.prototype.hasOwnProperty.call(firstPayload, "status") &&
          Object.prototype.hasOwnProperty.call(firstPayload, "watchdogStatus") &&
          Object.prototype.hasOwnProperty.call(firstPayload, "doctorStatus") &&
          Object.prototype.hasOwnProperty.call(firstPayload, "timestamp"),
        "hono-sse-runtime": runtime.targetRuntime,
      }),
    };
  });

const scenarioWsAuth = async (repo) =>
  withServer(repo, async (context) => {
    const runtime = computeRuntime(repo, context.loadedModules);
    const unauthenticated = await rawUpgrade(
      context.port,
      "/api/ws/chat",
      {},
    );
    const statusLine = String(unauthenticated.text || "").split("\r\n")[0];
    return {
      runtime,
      details: {
        statusLine,
        error: unauthenticated.error || null,
      },
      checks: buildChecks({
        "ws-unauthorized-401": /^HTTP\/1\.1 401\b/.test(statusLine),
        "ws-closed-before-bridge": Boolean(unauthenticated.text),
        "hono-ws-runtime": runtime.targetRuntime,
      }),
    };
  });

const scenarioProxyPrefix = async (repo) =>
  withServer(repo, async (context) => {
    const login = await performLogin(context.port);
    const runtime = computeRuntime(repo, context.loadedModules);
    const recordsBefore = context.mock.records.length;

    const openclaw = await request(context.port, {
      path: "/openclaw/abc",
      headers: { cookie: login.cookie },
      timeoutMs: 15000,
    });
    await sleep(100);
    const openclawRecord = context.mock.records
      .slice(recordsBefore)
      .find((record) => record.url === "/abc");

    const recordsBeforeFallback = context.mock.records.length;
    const fallback = await request(context.port, {
      path: "/api/unregistered-f2p-0003",
      headers: { cookie: login.cookie },
      timeoutMs: 15000,
    });
    await sleep(100);
    const fallbackRecord = context.mock.records
      .slice(recordsBeforeFallback)
      .find((record) => record.url === "/api/unregistered-f2p-0003");

    const recordsBeforeLocal = context.mock.records.length;
    const local = await request(context.port, {
      path: "/api/auth/status",
      headers: { cookie: login.cookie },
      timeoutMs: 15000,
    });
    await sleep(100);
    const localProxied = context.mock.records
      .slice(recordsBeforeLocal)
      .some((record) => record.url.startsWith("/api/auth/status"));
    const localBody = parseJsonBody(local);

    // The 5 MB limit applies to JSON and raw webhook endpoints. Other
    // gateway requests must retain their binary body, including larger
    // uploads, on both the explicit and fallback proxy routes.
    const binaryBody = Buffer.alloc(kFiveMegabytes + 1, 0xa5);
    const binaryUploads = [];
    for (const [inboundPath, gatewayPath] of [
      ["/openclaw/upload?kind=binary", "/upload?kind=binary"],
      ["/api/unregistered-upload?kind=binary", "/api/unregistered-upload?kind=binary"],
    ]) {
      const before = context.mock.records.length;
      const response = await request(context.port, {
        method: "POST",
        path: inboundPath,
        headers: {
          cookie: login.cookie,
          "content-type": "application/octet-stream",
          "content-length": String(binaryBody.length),
        },
        body: binaryBody,
        timeoutMs: 30000,
      });
      const forwarded = context.mock.records.slice(before)
        .find((record) => record.url === gatewayPath);
      const bytesPreserved = Boolean(forwarded && forwarded.body.equals(binaryBody));
      const responseBody = parseJsonBody(response);
      binaryUploads.push({
        inboundPath,
        gatewayPath,
        status: response.status,
        forwardedMethod: forwarded?.method || null,
        bytesPreserved,
        responsePreserved: response.status === 200 &&
          response.headers["x-mock-gateway"] === "yes" &&
          responseBody?.ok === true && responseBody?.gatewayPath === gatewayPath,
      });
    }

    // Node permits framed GET bodies even though Fetch Request has no GET
    // body. Proxy dispatch must retain those bytes from IncomingMessage too.
    const getBinaryBody = Buffer.from([0x00, 0xff, 0x42, 0x80, 0x0d, 0x0a]);
    const getBinaryUploads = [];
    for (const [inboundPath, gatewayPath] of [
      ["/openclaw/upload?kind=binary-get", "/upload?kind=binary-get"],
      ["/api/unregistered-upload?kind=binary-get", "/api/unregistered-upload?kind=binary-get"],
    ]) {
      const before = context.mock.records.length;
      const response = await request(context.port, {
        method: "GET",
        path: inboundPath,
        headers: {
          cookie: login.cookie,
          "content-type": "application/octet-stream",
          "content-length": String(getBinaryBody.length),
        },
        body: getBinaryBody,
        timeoutMs: 15000,
      });
      const forwarded = context.mock.records.slice(before)
        .find((record) => record.url === gatewayPath);
      const responseBody = parseJsonBody(response);
      getBinaryUploads.push({
        inboundPath,
        gatewayPath,
        status: response.status,
        forwardedMethod: forwarded?.method || null,
        bytesPreserved: Boolean(forwarded && forwarded.body.equals(getBinaryBody)),
        responsePreserved: response.status === 200 &&
          response.headers["x-mock-gateway"] === "yes" &&
          responseBody?.ok === true && responseBody?.gatewayPath === gatewayPath,
      });
    }

    return {
      runtime,
      details: {
        openclaw: {
          status: openclaw.status,
          forwardedPath: openclawRecord?.url || null,
        },
        fallback: {
          status: fallback.status,
          forwardedPath: fallbackRecord?.url || null,
        },
        local: {
          status: local.status,
          body: localBody,
          proxied: localProxied,
        },
        binaryUploads,
        getBinaryUploads,
      },
      checks: buildChecks({
        "openclaw-prefix-stripped":
          openclaw.status === 200 && openclawRecord?.url === "/abc",
        "api-fallback-proxied":
          fallback.status === 200 &&
          fallbackRecord?.url === "/api/unregistered-f2p-0003",
        "registered-api-route-stays-local":
          local.status === 200 &&
          localBody &&
          localBody.authEnabled === true &&
          !localProxied,
        "hono-proxy-runtime": runtime.targetRuntime,
        "non-json-proxy-body-preserved": binaryUploads.every((upload) =>
          upload.forwardedMethod === "POST" && upload.bytesPreserved),
        "non-json-proxy-response-preserved": binaryUploads.every((upload) =>
          upload.responsePreserved),
        "get-non-json-proxy-body-preserved": getBinaryUploads.every((upload) =>
          upload.forwardedMethod === "GET" && upload.bytesPreserved),
        "get-non-json-proxy-response-preserved": getBinaryUploads.every((upload) =>
          upload.responsePreserved),
      }),
    };
  });

const scenarioDependencyManifest = async (repo) => {
  const packageJson = JSON.parse(
    fs.readFileSync(path.join(repo, "package.json"), "utf8"),
  );
  const packageLock = JSON.parse(
    fs.readFileSync(path.join(repo, "package-lock.json"), "utf8"),
  );
  const dependencies = packageJson.dependencies || {};
  const lockDependencies = packageLock.packages?.[""]?.dependencies || {};
  const lockEngines = packageLock.packages?.[""]?.engines || {};
  const honoVersion = resolvePackageVersion(repo, "hono");
  const nodeServerVersion = resolvePackageVersion(repo, "@hono/node-server");
  const httpProxyVersion = resolvePackageVersion(repo, "http-proxy");
  const wsVersion = resolvePackageVersion(repo, "ws");
  const expectedEnginesNode = ">=24.16.0 <25 || >=26.1.0";
  return {
    runtime: {
      usesHono: Boolean(dependencies.hono),
      noExpressInHttpLayer: dependencies.express === undefined,
      targetRuntime:
        dependencies.hono === "4.13.7" &&
        Boolean(dependencies["@hono/node-server"]) &&
        dependencies.express === undefined,
    },
    details: {
      dependencies,
      lockDependencies,
      versions: {
        hono: honoVersion,
        nodeServer: nodeServerVersion,
        httpProxy: httpProxyVersion,
        ws: wsVersion,
      },
    },
    checks: buildChecks({
      "hono-direct-dependency": dependencies.hono === "4.13.7",
      "hono-node-server-direct-dependency": Boolean(
        dependencies["@hono/node-server"],
      ),
      "express-not-direct-dependency": dependencies.express === undefined,
      "http-proxy-and-ws-retained":
        Boolean(dependencies["http-proxy"]) && Boolean(dependencies.ws),
      "lockfile-root-declares-hono":
        lockDependencies.hono === "4.13.7" &&
        Boolean(lockDependencies["@hono/node-server"]) &&
        lockDependencies.express === undefined,
      "engines-node-unchanged":
        (packageJson.engines || {}).node === expectedEnginesNode &&
        lockEngines.node === expectedEnginesNode,
      "hono-installed-version": honoVersion === "4.13.7",
      "hono-node-server-installed": Boolean(nodeServerVersion),
    }),
  };
};

const kScenarios = {
  runtimeGraph: scenarioRuntimeGraph,
  servingRuntime: scenarioServingRuntime,
  inProcess: scenarioInProcess,
  healthHono: scenarioHealthHono,
  loginBody: scenarioLoginBody,
  rawWebhook: scenarioRawWebhook,
  rawWebhookRuntimeGraph: scenarioRawWebhookRuntimeGraph,
  staticFiles: scenarioStaticFiles,
  sseStream: scenarioSseStream,
  wsAuth: scenarioWsAuth,
  proxyPrefix: scenarioProxyPrefix,
  dependencyManifest: scenarioDependencyManifest,
};

const runObservation = async ({
  observationId,
  side,
  scenario,
  requirementIds = [],
}) => {
  const repo = resolveRepoRoot();
  const scenarioRunner = kScenarios[scenario];
  if (!scenarioRunner) {
    kStdoutWrite(
      `PROBE_RESULT ${JSON.stringify({
        observation_id: observationId,
        side,
        status: "INFRA_ERROR",
        requirement_ids: requirementIds,
        repo,
        error: `unknown scenario ${scenario}`,
      })}\n`,
    );
    process.exit(2);
  }

  try {
    const result = await scenarioRunner(repo);
    const checks = result.checks || {};
    const passed = Object.values(checks).every(Boolean);
    kStdoutWrite(
      `PROBE_RESULT ${JSON.stringify({
        observation_id: observationId,
        side,
        status: passed ? "PASS" : "FAIL",
        requirement_ids: requirementIds,
        repo,
        scenario,
        checks,
        details: result.details || {},
      })}\n`,
    );
    process.exit(passed ? 0 : 1);
  } catch (error) {
    kStdoutWrite(
      `PROBE_RESULT ${JSON.stringify({
        observation_id: observationId,
        side,
        status: "INFRA_ERROR",
        requirement_ids: requirementIds,
        repo,
        scenario,
        error: error && error.message ? error.message : String(error),
        stack: error && error.stack ? error.stack : null,
      })}\n`,
    );
    process.exit(2);
  }
};

module.exports = {
  computeRuntime,
  resolveRepoRoot,
  runObservation,
};
