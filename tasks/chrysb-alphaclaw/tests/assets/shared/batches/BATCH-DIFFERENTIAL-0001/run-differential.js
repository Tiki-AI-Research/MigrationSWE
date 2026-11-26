const path = require("path");
const caseFile = require(path.join(__dirname, "cases.json"));
const { deepEqual, normalizeExchange } = require("./normalize");

const kCookieName = "setup_token";
const kDefaultSetupPassword = "fixture-setup-password";

const getSetCookieHeaders = (headers = {}) => {
  const raw = headers["set-cookie"] || headers["Set-Cookie"];
  if (!raw) return [];
  return Array.isArray(raw) ? raw : [String(raw)];
};

const extractCookieValue = (setCookieHeaders, name) => {
  for (const header of setCookieHeaders) {
    const first = String(header).split(";")[0];
    const separator = first.indexOf("=");
    if (separator < 0) continue;
    if (first.slice(0, separator).trim() !== name) continue;
    return first.slice(separator + 1).trim();
  }
  return null;
};

const isClearedCookie = (header, name) => {
  const value = String(header);
  return (
    value.startsWith(`${name}=`) &&
    /max-age=0|expires=thu,?\s+01\s+jan\s+1970/i.test(value)
  );
};

const replaceVariables = (value, variables = {}) => {
  if (typeof value === "string") {
    return value.replace(/\$\{([A-Za-z0-9_.-]+)\}/g, (match, name) => {
      return Object.prototype.hasOwnProperty.call(variables, name)
        ? String(variables[name])
        : match;
    });
  }
  if (Array.isArray(value)) {
    return value.map((entry) => replaceVariables(entry, variables));
  }
  if (value && typeof value === "object") {
    const result = {};
    for (const [key, entry] of Object.entries(value)) {
      result[key] = replaceVariables(entry, variables);
    }
    return result;
  }
  return value;
};

const readValue = (exchange, selector) => {
  const parts = String(selector || "").split(".");
  let current = exchange;
  if (parts[0] === "body") {
    const raw = exchange?.body ?? exchange?.text;
    if (raw && typeof raw === "object" && !Buffer.isBuffer(raw)) {
      current = raw;
    } else {
      try {
        current = JSON.parse(
          Buffer.isBuffer(raw) ? raw.toString("utf8") : String(raw || ""),
        );
      } catch {
        return undefined;
      }
    }
    parts.shift();
  }
  for (const part of parts) {
    if (current === undefined || current === null) return undefined;
    current = current[part];
  }
  return current;
};

const captureValues = (step, exchange, state) => {
  if (!step.save) return;
  for (const [name, selector] of Object.entries(step.save)) {
    const value = readValue(exchange, selector);
    if (value !== undefined) state.variables[name] = value;
  }
};

const applySessionToRequest = (request, state) => {
  const next = replaceVariables(
    {
      ...request,
      headers: { ...(request.headers || {}) },
    },
    state.variables,
  );
  const cookie = state.cookies[kCookieName];
  if (request.session === "setup" && cookie) {
    const existing = next.headers.cookie;
    next.headers.cookie = existing
      ? `${existing}; ${kCookieName}=${cookie}`
      : `${kCookieName}=${cookie}`;
  }
  if (request.query_token_from_session && cookie) {
    const separator = next.path.includes("?") ? "&" : "?";
    next.path = `${next.path}${separator}token=${encodeURIComponent(cookie)}`;
  }
  return next;
};

const captureSession = (state, exchange) => {
  const headers = exchange.headers || {};
  const setCookieHeaders = getSetCookieHeaders(headers);
  const cookie = extractCookieValue(setCookieHeaders, kCookieName);
  if (cookie) state.cookies[kCookieName] = cookie;
  if (setCookieHeaders.some((header) => isClearedCookie(header, kCookieName))) {
    delete state.cookies[kCookieName];
  }
};

const resolveSetupPassword = (run, testCase) => {
  const configured =
    run?.server_env?.SETUP_PASSWORD ??
    testCase?.fixtures?.setup_password ??
    process.env.SETUP_PASSWORD;
  return configured === undefined || configured === null || configured === ""
    ? ""
    : String(configured);
};

const ensureSession = async (client, run, testCase, state) => {
  if (state.cookies[kCookieName]) return;
  const password = resolveSetupPassword(run, testCase);
  if (!password) return;
  const exchange = await client.request(
    {
      method: "POST",
      path: "/api/auth/login",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
    },
    state,
  );
  captureSession(state, exchange);
};

const stepNeedsSession = (step) =>
  step?.request?.session === "setup" ||
  step?.request?.query_token_from_session === true ||
  step?.websocket?.session === "setup";

const executeStep = async (client, step, state, defaults = {}) => {
  if (stepNeedsSession(step)) {
    await ensureSession(client, step.run || {}, step.testCase || {}, state);
  }
  const request = applySessionToRequest(step.request || {}, state);
  const exchange = await client.request(request, state);
  captureSession(state, exchange);
  captureValues(step, exchange, state);
  return {
    name: step.name,
    exchange: normalizeExchange(exchange, {
      ...defaults,
      ...(step.normalize || {}),
    }),
  };
};

const runSide = async (client, testCase) => {
  const results = [];
  for (const run of testCase.runs || []) {
    await client.startRun(run, testCase);
    const state = { cookies: {}, variables: {} };
    try {
      for (const step of run.steps || []) {
        results.push(
          await executeStep(
            client,
            { ...step, run, testCase },
            state,
            testCase.normalize || {},
          ),
        );
      }
    } finally {
      await client.stopRun(run, testCase);
    }
  }
  return results;
};

const runDifferential = async ({ source, target, cases = caseFile.cases }) => {
  const results = [];
  for (const testCase of cases) {
    const sourceResults = await runSide(source, testCase);
    const targetResults = await runSide(target, testCase);
    const equal = deepEqual(sourceResults, targetResults);
    const mismatches = [];
    if (!equal) {
      const length = Math.max(sourceResults.length, targetResults.length);
      for (let index = 0; index < length; index += 1) {
        if (!deepEqual(sourceResults[index], targetResults[index])) {
          mismatches.push({
            index,
            name: sourceResults[index]?.name || targetResults[index]?.name || null,
            source: sourceResults[index] || null,
            target: targetResults[index] || null,
          });
        }
      }
    }
    results.push({
      observation_id: testCase.observation_id,
      relation: "equal_after_normalization",
      status: equal ? "PASS" : "FAIL",
      mismatches,
    });
  }
  return results;
};

const clientContract = {
  startRun: "async (run, testCase) => void",
  request: "async (request, state) => ({ status, headers, body, gateway })",
  stopRun: "async (run, testCase) => void",
};

module.exports = {
  applySessionToRequest,
  captureSession,
  captureValues,
  clientContract,
  executeStep,
  runDifferential,
  runSide,
};
