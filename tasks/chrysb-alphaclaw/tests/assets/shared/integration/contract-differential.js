const live = require("./live-server");

const checks = {
  "DIFF-012": async (baseUrl) => {
    const response = await live.httpCall(baseUrl, { requestPath: "/health" });
    return response.status === 200 && ["healthy", "starting"].includes(response.json?.status);
  },
  "DIFF-013": async (baseUrl) => {
    const response = await live.httpCall(baseUrl, { requestPath: "/" });
    return response.status === 302 && response.headers.location === "/login.html";
  },
  "DIFF-014": async (baseUrl) => {
    const response = await live.httpCall(baseUrl, { requestPath: "/login.html" });
    return response.status === 200 && /html/i.test(response.headers["content-type"] || "");
  },
  "DIFF-015": async (baseUrl, cookie) => {
    const response = await live.httpCall(baseUrl, {
      requestPath: "/api/events/status",
      headers: { cookie },
    });
    return response.status === 200 && /text\/event-stream/i.test(response.headers["content-type"] || "");
  },
  "DIFF-018": async (baseUrl) => (await live.httpCall(baseUrl, { requestPath: "/health" })).status === 200,
  "DIFF-019": async (baseUrl) => (await live.httpCall(baseUrl, { requestPath: "/health" })).status === 200,
  "DIFF-032": async (baseUrl, cookie) => {
    const response = await live.httpCall(baseUrl, {
      requestPath: "/api/events/status",
      headers: { cookie },
    });
    return /no-cache/.test(response.headers["cache-control"] || "") &&
      response.headers["x-accel-buffering"] === "no";
  },
};

const main = async () => {
  const repoRoot = process.argv[2];
  const ids = (process.argv[3] || "").split(",").filter(Boolean);
  const session = await live.sharedLiveSession(repoRoot, {
    setupPassword: "fixture-setup-password",
  });
  const server = session.server;
  const out = {};
  try {
    const login = await live.httpCall(server.baseUrl, {
      method: "POST",
      requestPath: "/api/auth/login",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password: "fixture-setup-password" }),
    });
    for (const id of ids) {
      try {
        out[id] = checks[id] && await checks[id](server.baseUrl, login.cookie) ? "PASS" : "FAIL";
      } catch {
        out[id] = "FAIL";
      }
    }
  } finally {
  }
  process.stdout.write(`${JSON.stringify(out)}\n`);
};

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exit(1);
});
