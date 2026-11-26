"use strict";

const fs = require("fs");
const crypto = require("crypto");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const kStatuses = new Set([
  "PASS",
  "FAIL",
  "NA",
  "INFRA_ERROR",
  "INCONCLUSIVE",
  "TIMEOUT",
]);

const kDefaultTimeoutMs = 240000;
const kBatchTimeoutMs = 900000;

const kDiff0001FallbackFiles = {
  "DIFF-015": ["tests/server/routes-system.test.js"],
  "DIFF-032": ["tests/server/routes-proxy.test.js"],
};

const liveServer = require("./live-server");

const contractDifferentialChecks = {
  "DIFF-012": async (baseUrl) => {
    const response = await liveServer.httpCall(baseUrl, { requestPath: "/health" });
    return response.status === 200 && ["healthy", "starting"].includes(response.json?.status);
  },
  "DIFF-013": async (baseUrl) => {
    const response = await liveServer.httpCall(baseUrl, { requestPath: "/" });
    return response.status === 302 && response.headers.location === "/login.html";
  },
  "DIFF-014": async (baseUrl) => {
    const response = await liveServer.httpCall(baseUrl, { requestPath: "/login.html" });
    return response.status === 200 && /html/i.test(response.headers["content-type"] || "");
  },
  "DIFF-015": async (baseUrl, cookie) => {
    const response = await liveServer.httpCall(baseUrl, {
      requestPath: "/api/events/status",
      headers: { cookie },
    });
    return response.status === 200 && /text\/event-stream/i.test(response.headers["content-type"] || "");
  },
  "DIFF-018": async (baseUrl) => {
    const response = await liveServer.httpCall(baseUrl, { requestPath: "/health" });
    return response.status === 200;
  },
  "DIFF-019": async (baseUrl) => {
    const response = await liveServer.httpCall(baseUrl, { requestPath: "/health" });
    return response.status === 200;
  },
  "DIFF-032": async (baseUrl, cookie) => {
    const response = await liveServer.httpCall(baseUrl, {
      requestPath: "/api/events/status",
      headers: { cookie },
    });
    return /no-cache/.test(response.headers["cache-control"] || "") &&
      response.headers["x-accel-buffering"] === "no";
  },
};

const runContractDifferential = async (id, repoRoot) => {
  const check = contractDifferentialChecks[id];
  if (!check) return { status: "INCONCLUSIVE", reason: "no differential coverage" };
  const gateway = await liveServer.createGatewaySpy();
  const port = await liveServer.freePort();
  let server;
  try {
    server = await liveServer.startLiveServer({
      repoRoot: repoRoot,
      gatewayPort: gateway.port,
      port,
      setupPassword: "fixture-setup-password",
    });
    const login = await liveServer.httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/api/auth/login",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: "fixture-setup-password" }),
    });
    const ok = await check(server.baseUrl, login.cookie);
    return { status: ok ? "PASS" : "FAIL" };
  } catch (error) {
    return { status: "FAIL", reason: error.message };
  } finally {
    if (server) await server.stop();
    await gateway.close();
  }
};

const normalizeStatus = (value) => {
  const status = String(value || "").toUpperCase();
  return kStatuses.has(status) ? status : "INCONCLUSIVE";
};

const readJson = (filePath) => JSON.parse(fs.readFileSync(filePath, "utf8"));

const extractJson = (text) => {
  const source = String(text || "");
  const trimmed = source.trim();
  if (!trimmed) return null;
  try {
    return JSON.parse(trimmed);
  } catch {}
  const start = trimmed.indexOf("{");
  const end = trimmed.lastIndexOf("}");
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(trimmed.slice(start, end + 1));
    } catch {}
  }
  return null;
};

const extractLastJsonLine = (text) => {
  const lines = String(text || "").split(/\r?\n/);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index].trim();
    if (!line.startsWith("{")) continue;
    try {
      return JSON.parse(line);
    } catch {}
  }
  return extractJson(text);
};

const extractProbeResult = (text) => {
  const lines = String(text || "").split(/\r?\n/);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index].trim();
    if (!line.startsWith("PROBE_RESULT ")) continue;
    const parsed = extractJson(line.slice("PROBE_RESULT ".length));
    if (parsed) return parsed;
  }
  return null;
};

const resolveRepoRoot = (args = []) => {
  const readArg = (name) => {
    const index = args.indexOf(name);
    if (index === -1) return null;
    return args[index + 1] || null;
  };
  const inlineRepo = args
    .find((arg) => arg.startsWith("--repo="))
    ?.slice("--repo=".length);
  const candidates = [
    readArg("--repo"),
    inlineRepo,
    process.env.MIGRATION_REPO_ROOT,
    process.env.MIGRATION_REPO,
    process.env.REPO_ROOT,
    process.env.REPO,
    process.cwd(),
    "/workspace/repo",
  ].filter(Boolean);
  for (const candidate of candidates) {
    const resolved = path.resolve(candidate);
    if (fs.existsSync(path.join(resolved, "package.json"))) return resolved;
  }
  return path.resolve(process.cwd());
};

const makeEnvironment = ({ repoRoot, assetsRoot, sessionScope }) => {
  const environment = {
    ...process.env,
    MIGRATION_REPO_ROOT: repoRoot,
    MIGRATION_REPO: repoRoot,
    REPO_ROOT: repoRoot,
    REPO: repoRoot,
    MIGRATION_ASSET_ROOT: assetsRoot,
    MIGRATION_LIVE_SESSION_SCOPE: sessionScope,
    VITE_CONFIG_NATIVE_IGNORE_WARNING: "true",
  };
  if (!environment.SETUP_PASSWORD) {
    environment.SETUP_PASSWORD = "probe-setup-password";
  }
  if (!environment.ALPHACLAW_ROOT_DIR) {
    environment.ALPHACLAW_ROOT_DIR = fs.mkdtempSync(
      path.join(os.tmpdir(), "migration-observations-"),
    );
  }
  const nodePath = [path.join(repoRoot, "node_modules")];
  if (process.env.NODE_PATH) nodePath.push(process.env.NODE_PATH);
  environment.NODE_PATH = nodePath.join(path.delimiter);
  return environment;
};

const runProcess = ({
  command,
  args,
  repoRoot,
  assetsRoot,
  timeoutMs = kDefaultTimeoutMs,
  sessionScope,
}) => {
  const result = spawnSync(command, args, {
    cwd: repoRoot,
    env: makeEnvironment({ repoRoot, assetsRoot, sessionScope }),
    encoding: "utf8",
    timeout: timeoutMs,
    maxBuffer: 64 * 1024 * 1024,
  });
  const timedOut = Boolean(
    result.error && (result.error.code === "ETIMEDOUT" || result.signal),
  );
  return {
    timedOut,
    exitCode: result.status,
    signal: result.signal,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    error: result.error
      ? String(result.error.message || result.error)
      : null,
  };
};

const escapeRegExp = (value) =>
  String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const tempPath = (prefix, extension = ".json") =>
  path.join(
    os.tmpdir(),
    `${prefix}-${process.pid}-${Date.now()}-${Math.random()
      .toString(16)
      .slice(2)}${extension}`,
  );

const removeQuietly = (filePath) => {
  try {
    fs.rmSync(filePath, { force: true });
  } catch {}
};

const createRunner = ({ side, repoRoot, assetsRoot, inventory, onProgress }) => {
  const results = new Map();
  const sessionScope = crypto.randomBytes(16).toString("hex");
  const runCommand = (options) => runProcess({ ...options, sessionScope });
  const idsByBatch = new Map();
  for (const observation of inventory) {
    if (!idsByBatch.has(observation.batch_id)) {
      idsByBatch.set(observation.batch_id, []);
    }
    idsByBatch.get(observation.batch_id).push(observation.id);
  }

  const sidePath = (relativePath) =>
    path.join(assetsRoot, side, relativePath);
  const sharedPath = (relativePath) =>
    path.join(assetsRoot, "shared", relativePath);

  const setResult = (id, status, value, diagnostics) => {
    if (!results.has(id)) return;
    const normalized = normalizeStatus(status);
    results.set(id, {
      status: normalized,
      value:
        value && typeof value === "object"
          ? { ...value, result: normalized }
          : { result: normalized },
      ...(diagnostics ? { diagnostics } : {}),
    });
    onProgress?.(buildReport());
  };

  const markAll = (ids, status, reason) => {
    for (const id of ids) {
      setResult(id, status, {
        result: normalizeStatus(status),
        reason: reason || null,
      });
    }
  };

  const resolveVitest = () => {
    const candidates = [
      path.join(repoRoot, "node_modules", "vitest", "vitest.mjs"),
      path.join(repoRoot, "node_modules", ".bin", "vitest"),
    ];
    return candidates.find((candidate) => fs.existsSync(candidate)) || null;
  };

  const runP2P0001 = (ids) => {
    const script = sharedPath("batches/BATCH-P2P-0001/run-side.mjs");
    if (!fs.existsSync(script)) {
      markAll(ids, "INCONCLUSIVE", "P2P-0001 runner missing");
      return;
    }
    const outputFile = tempPath(`migration-p2p-0001-${side}`);
    const run = runCommand({
      command: process.execPath,
      args: [
        script,
        "--side",
        side,
        "--repo",
        repoRoot,
        "--out",
        outputFile,
      ],
      repoRoot,
      assetsRoot,
      timeoutMs: kBatchTimeoutMs,
    });
    let report = null;
    if (fs.existsSync(outputFile)) {
      try {
        report = readJson(outputFile);
      } catch {}
    }
    removeQuietly(outputFile);
    if (!report) {
      const status = run.timedOut ? "TIMEOUT" : "INCONCLUSIVE";
      markAll(ids, status, run.error || "P2P-0001 report unavailable");
      return;
    }
    const seen = new Set();
    for (const observation of report.observations || []) {
      const id = observation.observation_id || observation.id;
      if (!results.has(id)) continue;
      seen.add(id);
      setResult(id, observation.status, { result: observation.status },
        normalizeStatus(observation.status) === "PASS" ? undefined : {
          tests: observation.tests || [],
          runner: {
            exitCode: run.exitCode,
            signal: run.signal,
            timedOut: run.timedOut,
            error: run.error,
            stdout: run.stdout.slice(-8192),
            stderr: run.stderr.slice(-8192),
          },
        });
    }
    for (const id of ids) {
      if (!seen.has(id)) {
        setResult(id, "INCONCLUSIVE", {
          result: "INCONCLUSIVE",
          reason: "P2P-0001 observation missing",
        });
      }
    }
  };

  const runP2P0002Case = (testCase) => {
    if (testCase.kind === "vitest") {
      const vitest = resolveVitest();
      if (!vitest) {
        return { status: "INCONCLUSIVE", reason: "vitest unavailable" };
      }
      const args = [vitest, "run", testCase.file, "-t", escapeRegExp(testCase.name)];
      if (vitest.endsWith(".mjs")) {
        const run = runCommand({
          command: process.execPath,
          args,
          repoRoot,
          assetsRoot,
        });
        return {
          status: run.timedOut ? "TIMEOUT" : run.exitCode === 0 ? "PASS" : "FAIL",
          reason: run.error,
        };
      }
      const run = runCommand({
        command: vitest,
        args: args.slice(1),
        repoRoot,
        assetsRoot,
      });
      return {
        status: run.timedOut ? "TIMEOUT" : run.exitCode === 0 ? "PASS" : "FAIL",
        reason: run.error,
      };
    }
    if (testCase.kind === "node-test") {
      const candidates = [
        sidePath(`batches/BATCH-P2P-0002/${testCase.file}`),
        path.join(
          assetsRoot,
          "source",
          "batches",
          "BATCH-P2P-0002",
          testCase.file,
        ),
      ];
      const script = candidates.find((candidate) => fs.existsSync(candidate));
      if (!script) {
        return { status: "INCONCLUSIVE", reason: "node test asset missing" };
      }
      const run = runCommand({
        command: process.execPath,
        args: [
          "--test",
          "--test-name-pattern",
          escapeRegExp(testCase.name),
          script,
        ],
        repoRoot,
        assetsRoot,
      });
      return {
        status: run.timedOut ? "TIMEOUT" : run.exitCode === 0 ? "PASS" : "FAIL",
        reason: run.error,
      };
    }
    return { status: "INCONCLUSIVE", reason: "unknown P2P case kind" };
  };

  const runP2P0002 = (ids) => {
    const planPath = sharedPath("batches/BATCH-P2P-0002/test-plan.json");
    if (!fs.existsSync(planPath)) {
      markAll(ids, "INCONCLUSIVE", "P2P-0002 plan missing");
      return;
    }
    let plan;
    try {
      plan = readJson(planPath);
    } catch {
      markAll(ids, "INCONCLUSIVE", "P2P-0002 plan invalid");
      return;
    }
    for (const id of ids) {
      const entry = plan.observations?.[id];
      const cases = entry?.[side] || [];
      if (!cases.length) {
        setResult(id, "INCONCLUSIVE", {
          result: "INCONCLUSIVE",
          reason: "P2P-0002 observation missing",
        });
        continue;
      }
      const caseResults = cases.map(runP2P0002Case);
      let status = caseResults.every((item) => item.status === "PASS")
        ? "PASS"
        : "FAIL";
      if (caseResults.some((item) => item.status === "TIMEOUT")) {
        status = "TIMEOUT";
      } else if (
        caseResults.some((item) => item.status === "INCONCLUSIVE")
      ) {
        status = "INCONCLUSIVE";
      }
      setResult(id, status, { result: status });
    }
  };

  const runF2P0001 = (ids) => {
    const script = sidePath("batches/BATCH-F2P-0001/run-f2p-probes.js");
    if (!fs.existsSync(script)) {
      markAll(ids, "INCONCLUSIVE", "F2P-0001 probe missing");
      return;
    }
    const outputFile = tempPath(`migration-f2p-0001-${side}`);
    const run = runCommand({
      command: process.execPath,
      args: [script, repoRoot, outputFile],
      repoRoot,
      assetsRoot,
      timeoutMs: kBatchTimeoutMs,
    });
    let report = null;
    if (fs.existsSync(outputFile)) {
      try {
        report = readJson(outputFile);
      } catch {}
    }
    removeQuietly(outputFile);
    if (!report) report = extractJson(run.stdout);
    if (!report || !Array.isArray(report.observations)) {
      const status = run.timedOut ? "TIMEOUT" : "INCONCLUSIVE";
      markAll(ids, status, run.error || "F2P-0001 report unavailable");
      return;
    }
    const seen = new Set();
    for (const observation of report.observations) {
      const id = observation.observation_id || observation.id;
      if (!results.has(id)) continue;
      seen.add(id);
      setResult(id, observation.status, {
        result: observation.status,
        predicate: observation.predicate || null,
        target_technology_used: Boolean(observation.target_technology_used),
      });
    }
    for (const id of ids) {
      if (!seen.has(id)) {
        setResult(id, "INCONCLUSIVE", {
          result: "INCONCLUSIVE",
          reason: "F2P-0001 observation missing",
        });
      }
    }
  };

  const runPerObservationScript = ({ ids, batchId, parseOutput, timeoutMs }) => {
    for (const id of ids) {
      const script = sidePath(`batches/${batchId}/${id}.js`);
      if (!fs.existsSync(script)) {
        setResult(id, "INCONCLUSIVE", {
          result: "INCONCLUSIVE",
          reason: "probe script missing",
        });
        continue;
      }
      const run = runCommand({
        command: process.execPath,
        args: [script, repoRoot],
        repoRoot,
        assetsRoot,
        timeoutMs,
      });
      const parsed = parseOutput(run.stdout, run.stderr);
      if (!parsed || !parsed.status) {
        const status = run.timedOut ? "TIMEOUT" : "INCONCLUSIVE";
        setResult(id, status, {
          result: status,
          reason: run.error || "probe produced no status",
        });
        continue;
      }
      setResult(id, parsed.status, parseOutput.value
        ? parseOutput.value(parsed)
        : { result: parsed.status });
    }
  };

  const runF2P0002 = (ids) => {
    runPerObservationScript({
      ids,
      batchId: "BATCH-F2P-0002",
      parseOutput: Object.assign(
        (stdout) => extractLastJsonLine(stdout),
        {
          value: (parsed) => ({
            result: parsed.status,
            reason: parsed.reason || null,
            evidence: parsed.evidence || null,
          }),
        },
      ),
      timeoutMs: kDefaultTimeoutMs,
    });
  };

  const runF2P0003 = (ids) => {
    runPerObservationScript({
      ids,
      batchId: "BATCH-F2P-0003",
      parseOutput: Object.assign(
        (stdout) => extractProbeResult(stdout),
        {
          value: (parsed) => ({
            result: parsed.status,
            scenario: parsed.scenario || null,
            checks: parsed.checks || null,
            details: parsed.details || null,
            error: parsed.error || null,
          }),
        },
      ),
      timeoutMs: kDefaultTimeoutMs,
    });
  };

  const runDiff0001 = (ids) => {
    const planPath = sidePath(
      "batches/BATCH-DIFFERENTIAL-0001/probe-plan.json",
    );
    let coverage = {};
    if (fs.existsSync(planPath)) {
      try {
        coverage = readJson(planPath).observation_coverage || {};
      } catch {}
    }
    const contractIds = ids.filter(
      (id) =>
        contractDifferentialChecks[id] && !(coverage[id] || []).length,
    );
    const settled = new Set();
    if (contractIds.length && liveServer.usesTargetFramework(repoRoot)) {
      const script = path.join(__dirname, "contract-differential.js");
      const child = spawnSync(
        process.execPath,
        [script, repoRoot, contractIds.join(",")],
        { encoding: "utf8", timeout: kBatchTimeoutMs },
      );
      let parsed = null;
      try {
        parsed = JSON.parse(
          (child.stdout || "").trim().split("\n").pop() || "{}",
        );
      } catch {}
      for (const id of contractIds) {
        const status = parsed?.[id] || "FAIL";
        setResult(id, status, { result: status });
        settled.add(id);
      }
    }
    const filesByObservation = new Map();
    const allFiles = new Set();
    for (const id of ids) {
      if (settled.has(id)) continue;
      const files =
        (coverage[id] && coverage[id].length ? coverage[id] : null) ||
        kDiff0001FallbackFiles[id] ||
        [];
      filesByObservation.set(id, files);
      for (const file of files) allFiles.add(file);
    }
    const vitest = resolveVitest();
    if (!vitest) {
      markAll([...filesByObservation.keys()], "INCONCLUSIVE", "vitest unavailable");
      return;
    }
    const outputFile = tempPath(`migration-diff-0001-${side}`);
    const existingFiles = [...allFiles].filter((file) =>
      fs.existsSync(path.join(repoRoot, file)),
    );
    let report = null;
    let run = null;
    if (existingFiles.length) {
      const args = [
        "run",
        "--reporter=json",
        `--outputFile=${outputFile}`,
        "--no-color",
        ...existingFiles,
      ];
      run = runCommand({
        command: process.execPath,
        args: vitest.endsWith(".mjs")
          ? [vitest, ...args]
          : [vitest, ...args],
        repoRoot,
        assetsRoot,
        timeoutMs: kBatchTimeoutMs,
      });
      if (fs.existsSync(outputFile)) {
        try {
          report = readJson(outputFile);
        } catch {}
      }
      removeQuietly(outputFile);
    }
    if (run && run.timedOut) {
      markAll(
        [...filesByObservation.keys()],
        "TIMEOUT",
        "DIFF-0001 probe timed out",
      );
      return;
    }
    const fileStatus = new Map();
    for (const fileResult of report?.testResults || []) {
      const relativeFile = path
        .relative(repoRoot, fileResult.name)
        .split(path.sep)
        .join("/");
      const assertions = fileResult.assertionResults || [];
      const passed =
        assertions.length > 0 &&
        assertions.every(
          (assertion) =>
            assertion.status === "passed" || assertion.status === "pending",
        );
      fileStatus.set(relativeFile, passed ? "PASS" : "FAIL");
    }
    for (const id of ids) {
      if (settled.has(id)) continue;
      const files = filesByObservation.get(id) || [];
      if (!files.length) {
        setResult(id, "INCONCLUSIVE", {
          result: "INCONCLUSIVE",
          reason: "no differential coverage",
        });
        continue;
      }
      const missing = files.filter(
        (file) => !fs.existsSync(path.join(repoRoot, file)),
      );
      if (missing.length) {
        setResult(id, "FAIL", { result: "FAIL" });
        continue;
      }
      if (!report) {
        setResult(id, "INCONCLUSIVE", {
          result: "INCONCLUSIVE",
          reason: "differential test report unavailable",
        });
        continue;
      }
      const status = files.every((file) => fileStatus.get(file) === "PASS")
        ? "PASS"
        : "FAIL";
      setResult(id, status, { result: status });
    }
  };

  const runDiff0002 = (ids) => {
    const script = sidePath("batches/BATCH-DIFFERENTIAL-0002/probe.js");
    if (!fs.existsSync(script)) {
      markAll(ids, "INCONCLUSIVE", "DIFF-0002 probe missing");
      return;
    }
    const run = runCommand({
      command: process.execPath,
      args: [script, repoRoot],
      repoRoot,
      assetsRoot,
      timeoutMs: kBatchTimeoutMs,
    });
    const parsed = extractJson(run.stdout);
    if (!parsed) {
      const status = run.timedOut ? "TIMEOUT" : "INCONCLUSIVE";
      markAll(ids, status, run.error || "DIFF-0002 report unavailable");
      return;
    }
    for (const id of ids) {
      const caseResult = parsed.cases?.[id];
      if (!caseResult) {
        setResult(id, "INCONCLUSIVE", {
          result: "INCONCLUSIVE",
          reason: "DIFF-0002 observation missing",
        });
        continue;
      }
      // A per-case verdict: the probe wraps an individually failed case as
      // {expected, actual} (or {error}), so a passing case is never dragged
      // down by another case in the same batch.  ``runtimeMatches === false``
      // still fails the whole batch because the wrong runtime invalidates all
      // DIFFERENTIAL observations in it.
      const failed =
        Boolean(caseResult.error) ||
        (Object.prototype.hasOwnProperty.call(caseResult, "expected") &&
          Object.prototype.hasOwnProperty.call(caseResult, "actual")) ||
        parsed.runtimeMatches === false;
      setResult(id, failed ? "FAIL" : "PASS", {
        result: failed ? "FAIL" : "PASS",
      });
    }
  };

  const runBatch = (batchId, ids) => {
    switch (batchId) {
      case "BATCH-P2P-0001":
        return runP2P0001(ids);
      case "BATCH-P2P-0002":
        return runP2P0002(ids);
      case "BATCH-F2P-0001":
        return runF2P0001(ids);
      case "BATCH-F2P-0002":
        return runF2P0002(ids);
      case "BATCH-F2P-0003":
        return runF2P0003(ids);
      case "BATCH-DIFFERENTIAL-0001":
        return runDiff0001(ids);
      case "BATCH-DIFFERENTIAL-0002":
        return runDiff0002(ids);
      default:
        return markAll(ids, "INCONCLUSIVE", `unsupported batch ${batchId}`);
    }
  };

  const buildReport = () => ({
    schema_version: "migration-observations:v6",
    observations: inventory.map((observation) => {
      const result = results.get(observation.id) || {
        status: "INCONCLUSIVE",
        value: { result: "INCONCLUSIVE" },
      };
      return {
        id: observation.id,
        kind: observation.kind,
        status: normalizeStatus(result.status),
        value: {
          type: "json",
          value: result.value,
        },
        ...(result.diagnostics ? { diagnostics: result.diagnostics } : {}),
      };
    }),
  });

  const execute = () => {
    for (const observation of inventory) {
      results.set(observation.id, {
        status: "INCONCLUSIVE",
        value: { result: "INCONCLUSIVE", reason: "not executed" },
      });
    }
    onProgress?.(buildReport());
    for (const [batchId, ids] of idsByBatch) {
      try {
        runBatch(batchId, ids);
      } catch (error) {
        markAll(
          ids,
          "INFRA_ERROR",
          error && error.message ? error.message : String(error),
        );
      }
    }
    return buildReport();
  };

  return { execute };
};

const findReportPath = (args) => {
  const readArg = (name) => {
    const index = args.indexOf(name);
    if (index === -1) return null;
    return args[index + 1] || null;
  };
  const explicit = readArg("--report");
  if (explicit) return explicit;
  const inline = args
    .find((arg) => arg.startsWith("--report="))
    ?.slice("--report=".length);
  if (inline) return inline;
  return (
    args.find(
      (arg) =>
        /\.json$/i.test(arg) &&
        !fs.existsSync(path.join(path.resolve(arg), "package.json")),
    ) || null
  );
};

const run = async ({ side, argv = process.argv.slice(2) }) => {
  const assetsRoot = path.resolve(__dirname, "..", "..");
  const reportPath = findReportPath(argv);
  const repoArgs = reportPath
    ? argv.filter((arg) => arg !== reportPath)
    : argv;
  const repoRoot = resolveRepoRoot(repoArgs);
  const inventoryPath = path.join(
    assetsRoot,
    "shared",
    "integration",
    "observations.json",
  );
  const inventory = readJson(inventoryPath).observations || [];
  const writeProgress = (progress) => {
    if (!reportPath) return;
    const destination = path.resolve(reportPath);
    const temporary = `${destination}.${process.pid}.tmp`;
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    try {
      fs.writeFileSync(temporary, `${JSON.stringify(progress, null, 2)}\n`);
      fs.renameSync(temporary, destination);
    } finally {
      fs.rmSync(temporary, { force: true });
    }
  };
  const report = createRunner({
    side,
    repoRoot,
    assetsRoot,
    inventory,
    onProgress: writeProgress,
  }).execute();
  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  writeProgress(report);
  process.stdout.write(serialized);
  return report;
};

module.exports = { run };

if (require.main === module) {
  const sideIndex = process.argv.indexOf("--side");
  const side = sideIndex >= 0 ? process.argv[sideIndex + 1] : "source";
  run({ side }).catch((error) => {
    process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
    process.exitCode = 1;
  });
}
