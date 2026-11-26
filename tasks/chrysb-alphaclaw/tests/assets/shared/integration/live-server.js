const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const kBootTimeoutMs = 180000;

const freePort = () =>
  new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });

const waitForHealth = async (baseUrl, deadline, signal) => {
  while (Date.now() < deadline && !signal?.aborted) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(() => controller.abort(), 1000);
    try {
      const response = await fetch(`${baseUrl}/health`, { signal: controller.signal });
      if (response.ok) return true;
    } catch {} finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    }
    if (signal?.aborted) return false;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
};

const stopProcess = (child) => {
  try {
    if (child.pid) process.kill(-child.pid, "SIGKILL");
  } catch {}
  try {
    child.kill("SIGKILL");
  } catch {}
};

const createGatewaySpy = async () => {
  const calls = [];
  const sockets = new Set();
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
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  server.unref();
  let closing;
  return {
    calls,
    port: server.address().port,
    reply: (next) => {
      reply = next;
    },
    close: () => {
      if (!closing) {
        closing = new Promise((resolve) => {
          server.close(resolve);
          // An aborted proxy can leave an incomplete upstream request open.
          // server.close() alone waits for it forever and hides the probe's
          // actual result behind a harness timeout.
          for (const socket of sockets) socket.destroy();
        });
      }
      return closing;
    },
  };
};

const writeRoot = ({ rootDir, gatewayPort, openaiCompatEnabled, onboarded = true }) => {
  const openclawDir = path.join(rootDir, ".openclaw");
  fs.mkdirSync(openclawDir, { recursive: true });
  fs.writeFileSync(
    path.join(openclawDir, "openclaw.json"),
    JSON.stringify({ gateway: { port: gatewayPort } }, null, 2),
  );
  if (onboarded) {
    fs.writeFileSync(
      path.join(rootDir, "onboarded.json"),
      JSON.stringify({ onboarding_version: "contract-probe" }, null, 2),
    );
  }
  fs.writeFileSync(
    path.join(openclawDir, "alphaclaw.json"),
    JSON.stringify(
      { features: { openaiCompatApi: { enabled: openaiCompatEnabled === true } } },
      null,
      2,
    ),
  );
};

const startLiveServer = async ({
  repoRoot,
  gatewayPort,
  port,
  setupPassword = "contract-secret",
  gatewayToken = "contract-gateway-token",
  trustProxyHops = "1",
  env = {},
  rootDir,
  onboarded = true,
  openaiCompatEnabled = false,
}) => {
  const ownedRoot = !rootDir;
  const resolvedRoot = rootDir || fs.mkdtempSync(path.join(os.tmpdir(), "alphaclaw-live-"));
  writeRoot({
    rootDir: resolvedRoot,
    gatewayPort,
    openaiCompatEnabled,
    onboarded,
  });
  const child = spawn(
    process.execPath,
    [path.join(repoRoot, "bin", "alphaclaw.js"), "start"],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        PORT: String(port),
        SETUP_PASSWORD: setupPassword,
        ALPHACLAW_ROOT_DIR: resolvedRoot,
        HOME: resolvedRoot,
        OPENCLAW_GATEWAY_TOKEN: gatewayToken,
        TRUST_PROXY_HOPS: String(trustProxyHops),
        NO_COLOR: "1",
        ...env,
      },
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    },
  );
  child.unref();
  child.stdout.unref();
  child.stderr.unref();
  let output = "";
  child.stdout.on("data", (chunk) => {
    output = (output + String(chunk)).slice(-4000);
  });
  child.stderr.on("data", (chunk) => {
    output = (output + String(chunk)).slice(-4000);
  });
  const baseUrl = `http://127.0.0.1:${port}`;
  const polling = new AbortController();
  let onExit;
  let onError;
  let onClose;
  let outputClosed = false;
  const closed = new Promise((resolve) => {
    onClose = () => { outputClosed = true; resolve(); };
    child.once("close", onClose);
  });
  const exited = new Promise((resolve) => {
    onExit = (code, signal) => resolve({ failure: `server exited before becoming ready (code=${code}, signal=${signal})` });
    onError = (error) => resolve({ failure: `server failed to start: ${error.message}` });
    child.once("exit", onExit);
    child.once("error", onError);
    if (child.exitCode !== null && child.exitCode !== undefined) onExit(child.exitCode, child.signalCode);
  });
  try {
    const result = await Promise.race([
      waitForHealth(baseUrl, Date.now() + kBootTimeoutMs, polling.signal).then((ready) => ({ ready })),
      exited,
    ]);
    if (!result.ready) {
      polling.abort();
      // 'exit' can precede the final stderr data. Drain it briefly, bounded
      // even when a descendant inherits the child's output descriptors.
      if (result.failure && !outputClosed) {
        let timer;
        try {
          await Promise.race([closed, new Promise((resolve) => { timer = setTimeout(resolve, 100); })]);
        } finally {
          clearTimeout(timer);
        }
      }
      throw new Error(`${result.failure || "server did not become ready"}:\n${output}`);
    }
  } catch (error) {
    stopProcess(child);
    if (ownedRoot) fs.rmSync(resolvedRoot, { recursive: true, force: true });
    throw error;
  } finally {
    polling.abort();
    child.removeListener("exit", onExit);
    child.removeListener("error", onError);
    child.removeListener("close", onClose);
  }
  return {
    baseUrl,
    port,
    rootDir: resolvedRoot,
    output: () => output,
    stop: () => {
      stopProcess(child);
      if (ownedRoot) fs.rmSync(resolvedRoot, { recursive: true, force: true });
      return Promise.resolve();
    },
  };
};

const httpCall = async (
  baseUrl,
  { method = "GET", requestPath = "/", headers = {}, body } = {},
) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2500);
  let response;
  let reader;
  const chunks = [];
  let received = 0;
  let eventStream = false;
  try {
    response = await fetch(`${baseUrl}${requestPath}`, {
      method,
      headers,
      body,
      redirect: "manual",
      signal: controller.signal,
    });
    eventStream = /text\/event-stream/i.test(response.headers.get("content-type") || "");
    reader = response.body?.getReader();
    while (reader && received < 65536) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(Buffer.from(value));
      received += value.length;
      // These callers inspect a finite SSE sample. Waiting for EOF (or 64 KB)
      // on a healthy persistent stream can consume the entire verifier budget.
      if (eventStream && /\r?\n\r?\n/.test(Buffer.concat(chunks).toString("utf8"))) break;
    }
  } catch (error) {
    if (error.name !== "AbortError" || !response || !eventStream) throw error;
  } finally {
    try {
      await reader?.cancel();
    } catch {} finally {
      clearTimeout(timer);
    }
  }
  const buffer = Buffer.concat(chunks);
  let json = null;
  try {
    json = JSON.parse(buffer.toString("utf8"));
  } catch {}
  return {
    status: response.status,
    headers: Object.fromEntries(
      [...response.headers.entries()].map(([key, value]) => [key.toLowerCase(), value]),
    ),
    buffer,
    text: buffer.toString("utf8"),
    json,
    cookie: (response.headers.get("set-cookie") || "").split(";")[0],
  };
};

const gatewayLogPath = (port) => path.join(os.tmpdir(), `alphaclaw-gateway-${port}.jsonl`);

const readGatewayCalls = (port) => {
  try {
    return fs
      .readFileSync(gatewayLogPath(port), "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  } catch {
    return [];
  }
};

const startGatewaySidecar = (port, logPath) => {
  const child = spawn(
    process.execPath,
    [
      "-e",
      `
        const fs = require("fs");
        const http = require("http");
        const [port, logPath] = process.argv.slice(1);
        fs.writeFileSync(logPath, "");
        http.createServer((incoming, outgoing) => {
          const chunks = [];
          incoming.on("data", (chunk) => chunks.push(chunk));
          incoming.on("end", () => {
            fs.appendFileSync(logPath, JSON.stringify({
              method: incoming.method,
              url: incoming.url,
              headers: incoming.headers,
              bodyText: Buffer.concat(chunks).toString("utf8"),
            }) + "\\n");
            outgoing.statusCode = 200;
            outgoing.setHeader("content-type", "application/json");
            outgoing.end("{}");
          });
        }).listen(Number(port), "127.0.0.1");
      `,
      String(port),
      logPath,
    ],
    { detached: true, stdio: "ignore" },
  );
  child.unref();
  return child;
};

const sharedSessions = new Map();

const sharedSessionFile = (repoRoot, options) =>
  path.join(
    os.tmpdir(),
    `alphaclaw-live-${crypto
      .createHash("sha256")
      .update(JSON.stringify([repoRoot, options, process.env.MIGRATION_LIVE_SESSION_SCOPE || "standalone"]))
      .digest("hex")
      .slice(0, 16)}.json`,
  );

const bootSharedSession = async (repoRoot, options, recordPath) => {
  let record;
  try {
    record = JSON.parse(fs.readFileSync(recordPath, "utf8"));
  } catch {}
  // Failed boots are shared only within this observation run. A new harness
  // receives a fresh scope and must make its own startup attempt.
  if (record?.failure && process.env.MIGRATION_LIVE_SESSION_SCOPE) {
    throw new Error(record.failure);
  }
  if (record?.baseUrl) {
    if (await waitForHealth(record.baseUrl, Date.now() + 2000)) {
      return {
        gateway: {
          get calls() {
            return readGatewayCalls(record.gatewayPort);
          },
          port: record.gatewayPort,
          reply() {},
          close: async () => {},
        },
        server: {
          baseUrl: record.baseUrl,
          port: record.port,
          output: () => "",
          stop: async () => {},
        },
        port: record.port,
      };
    }
  }
  const gatewayPort = await freePort();
  const sidecar = startGatewaySidecar(gatewayPort, gatewayLogPath(gatewayPort));
  sidecar.on("error", () => {});
  const gateway = {
    get calls() {
      return readGatewayCalls(gatewayPort);
    },
    port: gatewayPort,
    reply() {},
    close: async () => {},
  };
  try {
    const port = await freePort();
    const server = await startLiveServer({
      repoRoot,
      gatewayPort: gateway.port,
      port,
      ...options,
    });
    fs.writeFileSync(
      recordPath,
      JSON.stringify({ baseUrl: server.baseUrl, port, gatewayPort: gateway.port }),
    );
    return { gateway, server, port };
  } catch (error) {
    stopProcess(sidecar);
    fs.rmSync(gatewayLogPath(gatewayPort), { force: true });
    if (process.env.MIGRATION_LIVE_SESSION_SCOPE) {
      fs.writeFileSync(recordPath, JSON.stringify({ failure: error.message }));
    }
    throw error;
  }
};

const sharedLiveSession = (repoRoot, options = {}) => {
  const recordPath = sharedSessionFile(repoRoot, options);
  // Cache the attempt before awaiting it: simultaneous probes otherwise start
  // several processes, and rejected attempts would restart for every case.
  if (!sharedSessions.has(recordPath)) {
    sharedSessions.set(recordPath, bootSharedSession(repoRoot, options, recordPath));
  }
  return sharedSessions.get(recordPath);
};

const usesTargetFramework = (repoRoot) => {
  try {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(repoRoot, "package.json"), "utf8"),
    );
    return (
      manifest.dependencies?.hono === "4.13.7" &&
      Boolean(manifest.dependencies?.["@hono/node-server"]) &&
      manifest.dependencies?.express === undefined
    );
  } catch {
    return false;
  }
};

module.exports = {
  createGatewaySpy,
  freePort,
  httpCall,
  sharedLiveSession,
  startLiveServer,
  usesTargetFramework,
  waitForHealth,
  writeRoot,
};
