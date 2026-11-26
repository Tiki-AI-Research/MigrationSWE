const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const zlib = require("node:zlib");
const { EventEmitter } = require("node:events");

// Exercise the real probe without requiring candidate dependencies or sockets.
// Each mutation represents observable behavior of an incorrect HTTP proxy.
const exercise = async (mutate = () => {}, failStartup = false) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "proxy-probe-selfcheck-"));
  fs.writeFileSync(path.join(root, "package.json"), JSON.stringify({
    name: "probe-fixture", dependencies: { hono: "4.13.7" },
  }));
  const calls = [];
  const cleaned = [];
  let reply;
  const gateway = {
    calls,
    port: 10001,
    reply: (value) => { reply = value; },
    close: async () => { cleaned.push("gateway"); },
  };
  const server = {
    baseUrl: "http://probe.invalid",
    stop: async () => { cleaned.push("server"); },
  };
  const live = {
    sharedLiveSession: async () => { throw new Error("must use a configurable gateway"); },
    createGatewaySpy: async () => gateway,
    freePort: async () => 10002,
    startLiveServer: async (options) => {
      assert.equal(options.gatewayPort, gateway.port);
      assert.equal(options.openaiCompatEnabled, true);
      if (failStartup) throw new Error("startup failed");
      return server;
    },
    httpCall: async (_baseUrl, options) => {
      if (options.requestPath === "/api/auth/login") {
        return { status: 200, headers: {}, json: { ok: true }, cookie: "setup_token=fixture" };
      }
      assert.equal(options.headers["content-encoding"], "gzip");
      assert.ok(options.headers.cookie);
      assert.ok(options.requestPath.includes("%2F"));
      const bodyText = JSON.stringify(JSON.parse(zlib.gunzipSync(options.body)), null, 2);
      const forwarded = {
        method: options.method, url: options.requestPath, bodyText,
        headers: { "content-length": String(Buffer.byteLength(bodyText)) },
      };
      const response = { status: reply.status, text: reply.body, headers: { ...reply.headers } };
      for (const name of ["connection", "proxy-authenticate", "set-cookie",
        ...reply.headers.connection.split(",").map((item) => item.trim().toLowerCase())]) {
        delete response.headers[name];
      }
      mutate({ response, forwarded, reply });
      calls.push(forwarded);
      return response;
    },
  };
  try {
    const filename = path.resolve(__dirname, "../assets/shared/batches/BATCH-F2P-0002/probe-lib.js");
    const probe = { exports: {} };
    const nativeRequire = Module.createRequire(filename);
    const load = (name) => name.endsWith("/integration/live-server.js") ? live : nativeRequire(name);
    vm.runInThisContext(Module.wrap(fs.readFileSync(filename, "utf8")), { filename })(
      probe.exports, load, probe, filename, path.dirname(filename),
    );
    const result = await probe.exports.runObservation("F2P-037", { repoRoot: root });
    return { result, cleaned };
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
};

test("accepts equivalent JSON serialization and cleans up its private session", async () => {
  const { result, cleaned } = await exercise();
  assert.equal(result.status, "PASS");
  assert.ok(Object.values(result.evidence.checks).every(Boolean));
  assert.deepEqual(cleaned, ["server", "gateway"]);
});

const mutations = {
  setCookie: ({ response }) => { response.headers["set-cookie"] = "secret=1"; },
  fixedHopHeader: ({ response }) => { response.headers["proxy-authenticate"] = "secret"; },
  mixedCaseConnectionToken: ({ response }) => { response.headers["x-upstream-private"] = "secret"; },
  secondConnectionToken: ({ response }) => { response.headers["x-upstream-second"] = "secret"; },
  wrongStatus: ({ response }) => { response.status = 200; },
  wrongBody: ({ response }) => { response.text = "{}"; },
  missingEndToEndHeader: ({ response }) => { delete response.headers["x-end-to-end"]; },
  wrongMethod: ({ forwarded }) => { forwarded.method = "GET"; },
  decodedUrl: ({ forwarded }) => { forwarded.url = decodeURIComponent(forwarded.url); },
  unparsedBody: ({ forwarded }) => { forwarded.bodyText = "compressed bytes"; },
  staleContentLength: ({ forwarded }) => { forwarded.headers["content-length"] = "2"; },
  contentEncoding: ({ forwarded }) => { forwarded.headers["content-encoding"] = "gzip"; },
  cookie: ({ forwarded }) => { forwarded.headers.cookie = "setup_token=secret"; },
};
for (const [name, mutate] of Object.entries(mutations)) {
  test(`rejects ${name} regression`, async () => {
    const { result, cleaned } = await exercise(mutate);
    assert.equal(result.status, "FAIL");
    assert.ok(Object.values(result.evidence.checks).some((value) => !value));
    assert.deepEqual(cleaned, ["server", "gateway"]);
  });
}

test("closes the gateway when server startup fails", async () => {
  const { result, cleaned } = await exercise(undefined, true);
  assert.equal(result.status, "FAIL");
  assert.match(result.reason, /startup failed/);
  assert.deepEqual(cleaned, ["gateway"]);
});

test("gateway cleanup finishes after a proxy leaves an incomplete request", async () => {
  // Model Node's close contract: active requests keep close's callback pending
  // until their sockets close. No listening port or candidate dependencies are
  // needed to reproduce the teardown hang.
  const active = new Set();
  let onClosed;
  let closeCalls = 0;
  const server = new EventEmitter();
  server.listen = (_port, _host, ready) => ready();
  server.address = () => ({ port: 10001 });
  server.unref = () => {};
  server.close = (callback) => {
    closeCalls += 1;
    onClosed = callback;
    if (!active.size) queueMicrotask(callback);
  };
  const filename = path.resolve(__dirname, "../assets/shared/integration/live-server.js");
  const live = { exports: {} };
  const nativeRequire = Module.createRequire(filename);
  const load = (name) => name === "http" ? { createServer: () => server } : nativeRequire(name);
  vm.runInThisContext(Module.wrap(fs.readFileSync(filename, "utf8")), { filename })(
    live.exports, load, live, filename, path.dirname(filename),
  );
  const gateway = await live.exports.createGatewaySpy();
  const socket = new EventEmitter();
  socket.destroyed = false;
  socket.destroy = () => {
    socket.destroyed = true;
    active.delete(socket);
    socket.emit("close");
    if (!active.size && onClosed) queueMicrotask(onClosed);
  };
  active.add(socket);
  server.emit("connection", socket);

  let timer;
  try {
    await Promise.race([
      gateway.close(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("gateway cleanup hung on an incomplete request")), 100);
      }),
    ]);
    assert.equal(socket.destroyed, true);
    await gateway.close();
    assert.equal(closeCalls, 1);
  } finally {
    clearTimeout(timer);
    socket.destroy();
  }
});

// The old helper spent its entire 180-second boot budget after a child had
// already exited. A virtual clock reproduces that path without a long wait.
const startupHarness = (root, mode, scope = "run-one") => {
  let now = 0;
  let port = 20000;
  let polls = 0;
  const children = [];
  const filename = path.resolve(__dirname, "../assets/shared/integration/live-server.js");
  const nativeRequire = Module.createRequire(filename);
  const overrides = {
    os: { ...os, tmpdir: () => root },
    net: {
      createServer() {
        const server = new EventEmitter();
        server.listen = (_port, _host, callback) => queueMicrotask(callback);
        server.address = () => ({ port: ++port });
        server.close = (callback) => queueMicrotask(callback);
        return server;
      },
    },
    child_process: {
      spawn(_command, args) {
        const child = new EventEmitter();
        child.kind = args[1] === "start" ? "server" : "gateway";
        child.pid = 987654;
        child.exitCode = null;
        child.stdout = new EventEmitter();
        child.stderr = new EventEmitter();
        child.unref = child.stdout.unref = child.stderr.unref = () => {};
        child.kill = () => { child.killed = true; };
        children.push(child);
        if (child.kind === "server" && ["exit", "error", "exit-late-output", "exit-open-output"].includes(mode)) {
          queueMicrotask(() => {
            if (mode !== "exit-late-output") child.stderr.emit("data", "synthetic candidate startup error");
            if (mode === "error") child.emit("error", new Error("synthetic spawn failure"));
            else {
              child.exitCode = 1;
              child.emit("exit", 1, null);
            }
            if (mode === "exit-late-output") {
              queueMicrotask(() => {
                child.stderr.emit("data", "synthetic candidate startup error");
                child.emit("close", 1, null);
              });
            } else if (mode !== "exit-open-output") child.emit("close", 1, null);
          });
        }
        return child;
      },
    },
  };
  const live = { exports: {} };
  vm.runInNewContext(fs.readFileSync(filename, "utf8"), {
    module: live, exports: live.exports, __dirname: path.dirname(filename),
    require: (name) => overrides[name] || nativeRequire(name),
    process: { ...process, env: { ...process.env, MIGRATION_LIVE_SESSION_SCOPE: scope }, kill() {} },
    Buffer, AbortController,
    Date: { now: () => now },
    setTimeout(callback, milliseconds) {
      const timer = {};
      if ([100, 250].includes(milliseconds)) queueMicrotask(() => {
        if (!timer.cancelled) { now += milliseconds; callback(); }
      });
      else assert.equal(milliseconds, 1000);
      return timer;
    },
    clearTimeout(timer) { if (timer) timer.cancelled = true; },
    fetch: async () => { polls++; return { ok: mode === "healthy" }; },
  }, { filename, timeout: 3000 });
  return { live: live.exports, children, elapsed: () => now, polls: () => polls };
};

for (const mode of ["exit", "error", "exit-late-output", "exit-open-output"]) {
  test(`reports ${mode} during startup without waiting for the health deadline`, async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "startup-fast-failure-"));
    try {
      const harness = startupHarness(root, mode);
      await assert.rejects(harness.live.startLiveServer({ repoRoot: root, gatewayPort: 10001, port: 10002 }),
        mode.startsWith("exit") ? /exited before becoming ready.*code=1[\s\S]*synthetic candidate/ : /synthetic spawn failure/);
      assert.ok(harness.elapsed() < 1000);
      assert.ok(harness.polls() < 4);
      assert.ok(harness.children.every((child) => child.killed));
      assert.equal(fs.readdirSync(root).filter((name) => name.startsWith("alphaclaw-live-")).length, 0);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
}

test("shares an in-flight boot across concurrent probes", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "startup-shared-success-"));
  try {
    const harness = startupHarness(root, "healthy");
    const sessions = await Promise.all(Array.from({ length: 7 }, () => harness.live.sharedLiveSession(root)));
    assert.ok(sessions.every((session) => session === sessions[0]));
    assert.equal(harness.children.filter((child) => child.kind === "server").length, 1);
    await sessions[0].server.stop();
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a failed shared boot is reused across probes and processes, but a new run retries", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "startup-shared-failure-"));
  try {
    const first = startupHarness(root, "exit");
    const failures = await Promise.allSettled(Array.from({ length: 7 }, () => first.live.sharedLiveSession(root)));
    assert.ok(failures.every((result) => result.status === "rejected"));
    assert.equal(first.children.filter((child) => child.kind === "server").length, 1);
    assert.ok(first.children.every((child) => child.killed));
    const secondProcess = startupHarness(root, "healthy");
    await assert.rejects(secondProcess.live.sharedLiveSession(root), /synthetic candidate/);
    assert.equal(secondProcess.children.length, 0);
    const nextRun = startupHarness(root, "healthy", "run-two");
    const session = await nextRun.live.sharedLiveSession(root);
    assert.equal(nextRun.children.filter((child) => child.kind === "server").length, 1);
    await session.server.stop();
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("an alive but unhealthy shared server consumes one boot budget across observations", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "startup-unhealthy-"));
  try {
    const first = startupHarness(root, "unhealthy");
    await assert.rejects(first.live.sharedLiveSession(root), /did not become ready/);
    assert.equal(first.elapsed(), 180000);
    const second = startupHarness(root, "unhealthy");
    await assert.rejects(second.live.sharedLiveSession(root), /did not become ready/);
    assert.equal(second.elapsed(), 0);
    assert.equal(second.children.length, 0);
    assert.ok(first.children.every((child) => child.killed));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("observation runner saves results before the next probe and scopes startup records per run", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "observation-checkpoints-"));
  const filename = path.resolve(__dirname, "../assets/shared/integration/run-observations.js");
  const inventoryPath = path.join(path.dirname(filename), "observations.json");
  const inventory = JSON.parse(fs.readFileSync(inventoryPath, "utf8")).observations
    .filter((row) => row.batch_id === "BATCH-F2P-0002").slice(0, 3);
  const reportPath = path.join(root, "report.json");
  const scopes = [];
  let calls = 0;
  const nativeRequire = Module.createRequire(filename);
  const module = { exports: {} };
  const overrides = {
    fs: {
      ...fs,
      readFileSync(file, ...args) {
        return file === inventoryPath ? JSON.stringify({ observations: inventory }) : fs.readFileSync(file, ...args);
      },
    },
    "./live-server": {},
    child_process: {
      spawnSync(_command, _args, options) {
        const checkpoint = JSON.parse(fs.readFileSync(reportPath, "utf8"));
        assert.equal(checkpoint.observations[calls % 3].status, "INCONCLUSIVE");
        if (calls % 3 > 0) assert.equal(checkpoint.observations[calls % 3 - 1].status, "FAIL");
        scopes.push(options.env.MIGRATION_LIVE_SESSION_SCOPE);
        calls++;
        return { status: 1, stdout: JSON.stringify({ status: "FAIL", reason: "candidate startup failed" }) };
      },
    },
  };
  try {
    fs.writeFileSync(path.join(root, "package.json"), "{}");
    vm.runInNewContext(fs.readFileSync(filename, "utf8"), {
      module, exports: module.exports, __dirname: path.dirname(filename),
      require: (name) => overrides[name] || nativeRequire(name),
      process: { ...process, env: { ...process.env, MIGRATION_REPO_ROOT: root, ALPHACLAW_ROOT_DIR: root }, stdout: { write() {} } },
    }, { filename, timeout: 3000 });
    for (let run = 0; run < 2; run++) {
      const report = await module.exports.run({ side: "target", argv: [reportPath] });
      assert.ok(report.observations.every((row) => row.status === "FAIL"));
    }
    assert.equal(calls, 6);
    assert.ok(scopes[0]);
    assert.equal(new Set(scopes.slice(0, 3)).size, 1);
    assert.equal(new Set(scopes.slice(3)).size, 1);
    assert.notEqual(scopes[0], scopes[3]);
    assert.equal(fs.readdirSync(root).filter((name) => name.endsWith(".tmp")).length, 0);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

// Run the real proxy scenario with a controlled transport. This checks the
// probe's acceptance rules without candidate dependencies or listening ports.
const exerciseProxyPrefix = async (mutateGet = () => {}) => {
  const filename = path.resolve(__dirname, "../assets/shared/batches/BATCH-F2P-0003/probe-lib.js");
  const source = fs.readFileSync(filename, "utf8");
  const start = source.indexOf("const scenarioProxyPrefix =");
  const end = source.indexOf("const scenarioDependencyManifest =", start);
  assert.ok(start >= 0 && end > start);
  const records = [];
  const getPaths = [];
  const context = { port: 10001, mock: { records }, loadedModules: new Set() };
  const scenario = vm.runInNewContext(`${source.slice(start, end)}\nscenarioProxyPrefix`, {
    Buffer,
    kFiveMegabytes: 5 * 1024 * 1024,
    withServer: async (_repo, run) => run(context),
    computeRuntime: () => ({ targetRuntime: true }),
    performLogin: async () => ({ cookie: "setup_token=fixture" }),
    sleep: async () => {},
    parseJsonBody: (response) => JSON.parse(response.body.toString("utf8")),
    buildChecks: (checks) => checks,
    request: async (_port, options) => {
      if (options.path === "/api/auth/status") {
        return { status: 200, headers: {}, body: Buffer.from('{"authEnabled":true}') };
      }
      const gatewayPath = options.path.replace(/^\/openclaw/, "");
      const record = {
        url: gatewayPath,
        method: options.method || "GET",
        body: options.body || Buffer.alloc(0),
      };
      const response = {
        status: 200,
        headers: { "x-mock-gateway": "yes" },
        body: Buffer.from(JSON.stringify({ ok: true, gatewayPath })),
      };
      if (options.path.includes("binary-get")) {
        assert.equal(options.method, "GET");
        assert.equal(Number(options.headers["content-length"]), options.body.length);
        assert.ok(options.body.includes(0x00) && options.body.includes(0xff));
        getPaths.push(options.path);
        mutateGet(record, response);
      }
      records.push(record);
      return response;
    },
  }, { filename, timeout: 3000 });
  const result = await scenario("/candidate");
  assert.deepEqual(getPaths, [
    "/openclaw/upload?kind=binary-get",
    "/api/unregistered-upload?kind=binary-get",
  ]);
  return result;
};

test("proxy prefix probe accepts GET binary bodies on both gateway routes", async () => {
  const result = await exerciseProxyPrefix();
  assert.ok(Object.values(result.checks).every(Boolean));
  assert.equal(result.details.getBinaryUploads.length, 2);
});

for (const [name, mutate] of Object.entries({
  droppedBody: (record) => { record.body = Buffer.alloc(0); },
  changedByte: (record) => { record.body = Buffer.from(record.body); record.body[1] = 0x7f; },
  changedMethod: (record) => { record.method = "POST"; },
  wrongStatus: (_record, response) => { response.status = 502; },
  missingHeader: (_record, response) => { response.headers = {}; },
  changedResponse: (_record, response) => { response.body = Buffer.from('{"ok":false}'); },
})) {
  test(`proxy prefix probe rejects GET ${name} regressions`, async () => {
    const result = await exerciseProxyPrefix(mutate);
    assert.ok(Object.values(result.checks).some((value) => !value));
  });
}
