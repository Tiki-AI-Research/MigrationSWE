import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { readFile } from "node:fs/promises";
import { listenForProbe } from "../BATCH-F2P-0001/predicates.mjs";

const now = () => new Date().toISOString();

export function makeDetail(runId: string, status = "completed") {
  return {
    run: {
      id: runId,
      scenarioId: "fixture-scenario",
      labId: "kanban",
      browserMode: "headless",
      model: "test-model",
      maxResponseTurns: 24,
      prompt: "p",
      status,
      startedAt: now(),
      ...(status === "completed" ? { completedAt: now() } : {}),
    },
    scenario: {
      id: "fixture-scenario",
      labId: "kanban",
      category: "productivity",
      title: "Fixture",
      description: "Fixture scenario",
      defaultPrompt: "p",
      workspaceTemplatePath: "/tmp/ws",
      tags: ["fixture"],
    },
    workspacePath: "/tmp/ws",
    eventStreamUrl: `/api/runs/${runId}/events`,
    replayUrl: `/api/runs/${runId}/replay`,
    events: [
      {
        id: `${runId}-1`,
        runId,
        sequence: 0,
        type: status === "completed" ? "run_completed" : "run_started",
        level: "ok",
        message: "done",
        createdAt: now(),
      },
    ],
  };
}

export function makeManager(overrides: Record<string, unknown> = {}) {
  const calls: unknown[] = [];
  const base: Record<string, unknown> = {
    calls,
    async resetScenario(id: string) {
      return { scenarioId: id, resetAt: now() };
    },
    async startRun(input: unknown) {
      calls.push(input);
      return makeDetail("run-1", "running");
    },
    async getActiveRunDetail() {
      return null;
    },
    async getRunDetail(id: string) {
      return makeDetail(id, "completed");
    },
    async stopRun(id: string) {
      return makeDetail(id, "cancelled");
    },
    async getReplayBundle(id: string) {
      const d = makeDetail(id, "completed");
      return {
        version: 3,
        run: d.run,
        scenario: d.scenario,
        events: d.events,
        artifacts: {
          eventsPath: "e",
          replayPath: "r",
          runPath: "u",
          screenshotsDirectory: "s",
          workspacePath: "w",
        },
      };
    },
    subscribe() {
      return () => {};
    },
    async shutdown() {},
  };
  return Object.assign(base, overrides);
}

export type ProbeResult = {
  observation_id: string;
  status: "PASS" | "FAIL";
  detail: string;
};

export async function runProbes(repoRoot: string): Promise<ProbeResult[]> {
  const serverMod: any = await import(
    pathToFileURL(join(repoRoot, "javascript-app/src/server.ts")).href
  );
  const errMod: any = await import(
    pathToFileURL(join(repoRoot, "javascript-app/src/errors.ts")).href
  );
  const createServer = serverMod.createServer;
  const RunnerCoreError = errMod.RunnerCoreError;

  const results: ProbeResult[] = [];
  const record = (id: string, ok: boolean, detail: string) => {
    results.push({ observation_id: id, status: ok ? "PASS" : "FAIL", detail });
  };
  const withServer = async (manager: unknown, fn: (app: any) => Promise<void>) => {
    const app = createServer({ manager, dataRoot: "/tmp/cua-data" });
    try {
      await fn(app);
    } finally {
      try {
        await app.close();
      } catch {
        /* ignore teardown errors */
      }
    }
  };
  const origError = console.error;
  console.error = () => {};

  try {
    // R035 (F2P-042 / F2P-033 / F2P-038): createServer().listen() resolves to
    // "http://127.0.0.1:<port>" and close() resolves.
    for (const id of ["F2P-042", "F2P-033", "F2P-038"]) {
      const app = createServer({ manager: makeManager(), dataRoot: "/tmp/cua-data" });
      let ok = false;
      let detail = "";
      try {
        const url = await listenForProbe(app);
        await app.close();
        ok = typeof url === "string" && /^http:\/\/127\.0\.0\.1:\d+$/.test(url);
        detail = `listen returned ${JSON.stringify(url)}`;
      } catch (error: any) {
        detail = `threw ${error?.message}`;
      }
      record(id, ok, detail);
    }

    // R041 (F2P-028 / F2P-029 / F2P-040): idle GET /api/runs/active returns
    // HTTP 200, body exactly null and content-type application/json.
    for (const id of ["F2P-028", "F2P-029", "F2P-040"]) {
      await withServer(makeManager(), async (app) => {
        const res = await app.inject({ method: "GET", url: "/api/runs/active" });
        const ct = String(res.headers["content-type"] ?? "");
        const ok = res.statusCode === 200 && res.body === "null" && ct.includes("application/json");
        record(id, ok, `status=${res.statusCode} body=${JSON.stringify(res.body)} ct=${ct}`);
      });
    }

    // R038 (F2P-027): a rejecting manager call reaches the four-argument error
    // middleware, returning 500 internal_runner_error and logging the Error.
    {
      const manager = makeManager({
        async getActiveRunDetail() {
          throw new Error("boom");
        },
      });
      const logged: unknown[][] = [];
      console.error = (...args: unknown[]) => {
        logged.push(args);
      };
      let ok = false;
      let detail = "";
      const app = createServer({ manager, dataRoot: "/tmp/cua-data" });
      try {
        const res = await app.inject({ method: "GET", url: "/api/runs/active" });
        const body = res.body ? JSON.parse(res.body) : {};
        const loggedOriginal = logged.some((args) =>
          args.some((a) => a instanceof Error && a.message === "boom"),
        );
        ok = res.statusCode === 500 && body.code === "internal_runner_error" && loggedOriginal;
        detail = `status=${res.statusCode} code=${body.code} loggedOriginal=${loggedOriginal}`;
      } finally {
        console.error = () => {};
        try {
          await app.close();
        } catch {
          /* ignore */
        }
      }
      record("F2P-027", ok, detail);
    }

    // R051 (F2P-035 / F2P-025 / F2P-026): rejected awaited manager calls return
    // the JSON runner error envelope instead of hanging the request.
    for (const [id, path] of [
      ["F2P-035", "/api/runs/test-run"],
      ["F2P-025", "/api/runs/test-run"],
      ["F2P-026", "/api/runs"],
    ] as const) {
      const manager = makeManager({
        async getRunDetail() {
          throw new Error("kaboom");
        },
        async startRun() {
          throw new Error("kaboom");
        },
      });
      let ok = false;
      let detail = "";
      try {
        await withServer(manager, async (app) => {
          const res =
            path === "/api/runs"
              ? await app.inject({
                  method: "POST",
                  url: path,
                  payload: { scenarioId: "fixture-scenario", prompt: "p" },
                })
              : await app.inject({ method: "GET", url: path });
          const body = res.body ? JSON.parse(res.body) : {};
          ok =
            res.statusCode >= 400 &&
            typeof body.code === "string" &&
            typeof body.error === "string";
          detail = `status=${res.statusCode} code=${body.code}`;
        });
      } catch (error: any) {
        detail = `threw ${error?.message}`;
      }
      record(id, ok, detail);
    }

    // R036 (F2P-041): express.json parses the request body before the handler so
    // manager.startRun receives the parsed payload and the route returns 202.
    {
      const manager = makeManager();
      await withServer(manager, async (app) => {
        const res = await app.inject({
          method: "POST",
          url: "/api/runs",
          payload: { scenarioId: "fixture-scenario", prompt: "p" },
        });
        const passed: any = (manager as any).calls[0];
        const ok =
          res.statusCode === 202 &&
          passed &&
          passed.scenarioId === "fixture-scenario" &&
          passed.prompt === "p";
        record("F2P-041", ok, `status=${res.statusCode} passed=${JSON.stringify(passed)}`);
      });
    }

    // R044 (F2P-043 / F2P-030): percent-encoded traversal run id is rejected with
    // 400 invalid_request before any filesystem lookup.
    for (const id of ["F2P-043", "F2P-030"]) {
      await withServer(makeManager(), async (app) => {
        const res = await app.inject({ method: "GET", url: "/api/runs/..%2F..%2Foutside" });
        const body = res.body ? JSON.parse(res.body) : {};
        const ok = res.statusCode === 400 && body.code === "invalid_request";
        record(id, ok, `status=${res.statusCode} code=${body.code}`);
      });
    }

    // R040 (F2P-044 / F2P-046): every response carries Vary: Origin, even with no
    // Origin request header.
    for (const id of ["F2P-044", "F2P-046"]) {
      await withServer(makeManager(), async (app) => {
        const res = await app.inject({ method: "GET", url: "/health" });
        const vary = String(res.headers["vary"] ?? "");
        const ok = vary
          .toLowerCase()
          .split(",")
          .map((s) => s.trim())
          .includes("origin");
        record(id, ok, `vary=${JSON.stringify(vary)}`);
      });
    }

    // R045 (F2P-031): the removed verificationEnabled field is rejected with 400
    // invalid_request and manager.startRun is never called.
    {
      const manager = makeManager();
      await withServer(manager, async (app) => {
        const res = await app.inject({
          method: "POST",
          url: "/api/runs",
          payload: { scenarioId: "fixture-scenario", prompt: "p", verificationEnabled: true },
        });
        const body = res.body ? JSON.parse(res.body) : {};
        const ok =
          res.statusCode === 400 &&
          body.code === "invalid_request" &&
          (manager as any).calls.length === 0;
        record("F2P-031", ok, `status=${res.statusCode} code=${body.code} startRunCalls=${(manager as any).calls.length}`);
      });
    }

    // R046 (F2P-045): maxResponseTurns over 50 is rejected with 400 invalid_request
    // and an error message that mentions maxResponseTurns.
    {
      const manager = makeManager();
      await withServer(manager, async (app) => {
        const res = await app.inject({
          method: "POST",
          url: "/api/runs",
          payload: { scenarioId: "fixture-scenario", prompt: "p", maxResponseTurns: 51 },
        });
        const body = res.body ? JSON.parse(res.body) : {};
        const ok =
          res.statusCode === 400 &&
          body.code === "invalid_request" &&
          String(body.error).includes("maxResponseTurns");
        record("F2P-045", ok, `status=${res.statusCode} code=${body.code} error=${body.error}`);
      });
    }

    // R042 (F2P-034): SSE for a stored completed run streams
    // text/event-stream with a run_completed frame and echoes the allowed Origin.
    {
      await withServer(makeManager(), async (app) => {
        const res = await app.inject({
          method: "GET",
          url: "/api/runs/test-run/events",
          headers: { origin: "http://127.0.0.1:3000" },
        });
        const ct = String(res.headers["content-type"] ?? "");
        const acao = String(res.headers["access-control-allow-origin"] ?? "");
        const ok =
          res.statusCode === 200 &&
          ct.includes("text/event-stream") &&
          acao === "http://127.0.0.1:3000" &&
          res.body.includes('"type":"run_completed"');
        record("F2P-034", ok, `status=${res.statusCode} ct=${ct} acao=${acao}`);
      });
    }

    // R043 (F2P-036 / F2P-037): an unknown run returns 404 run_not_found with a
    // hint that says to start a new run.
    for (const id of ["F2P-036", "F2P-037"]) {
      const manager = makeManager({
        async getRunDetail() {
          throw new RunnerCoreError("Run not found", {
            code: "run_not_found",
            hint: "Start a new run. Open the console and launch a scenario.",
            statusCode: 404,
          });
        },
      });
      await withServer(manager, async (app) => {
        const res = await app.inject({ method: "GET", url: "/api/runs/missing-run" });
        const body = res.body ? JSON.parse(res.body) : {};
        const ok =
          res.statusCode === 404 &&
          body.code === "run_not_found" &&
          String(body.hint).includes("Start a new run");
        record(id, ok, `status=${res.statusCode} code=${body.code} hint=${body.hint}`);
      });
    }

    // R050 / R028 (F2P-047 / F2P-048 / F2P-032 / F2P-039): the runner module
    // imports express and node:http and opens its own listener without importing
    // fastify or delegating to a Fastify runtime.
    for (const id of ["F2P-047", "F2P-048", "F2P-032", "F2P-039"]) {
      const src = await readFile(join(repoRoot, "javascript-app/src/server.ts"), "utf8");
      const ok =
        /from\s+["']express["']/.test(src) &&
        /from\s+["']node:http["']/.test(src) &&
        !/from\s+["']fastify["']/.test(src);
      record(
        id,
        ok,
        `imports express=${/["']express["']/.test(src)} node:http=${/["']node:http["']/.test(src)} fastify=${/["']fastify["']/.test(src)}`,
      );
    }
  } finally {
    console.error = origError;
  }

  return results;
}
