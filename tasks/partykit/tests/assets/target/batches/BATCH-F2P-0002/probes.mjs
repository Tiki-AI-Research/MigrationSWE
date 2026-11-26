import fs from "node:fs/promises";
import { createRequire } from "node:module";
import { register } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { makePartyserverRoute, runAll, satisfiesRange } from "../../../shared/batches/BATCH-F2P-0002/suite.mjs";
import { setRoutePartykitHandler } from "../../../shared/batches/BATCH-F2P-0002/partyserver-stub.mjs";
import { setEnv } from "./cloudflare-workers-stub.mjs";

register(new URL("./loader.mjs", import.meta.url), import.meta.url);

const repoRoot = path.resolve(process.env.MIGRATION_REPO_ROOT ?? "/workspace/repo");
const requireFromRepo = createRequire(path.join(repoRoot, "package.json"));
const envByApp = new WeakMap();

async function importFirst(candidates) {
  let lastError;
  for (const candidate of candidates) {
    try {
      return await import(candidate);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError;
}

async function loadElysia() {
  const resolved = requireFromRepo.resolve("elysia");
  const module = await import(pathToFileURL(resolved).href);
  return module.Elysia ?? module.default;
}

async function loadMiddleware() {
  const dist = path.join(repoRoot, "packages/hono-party/dist/index.js");
  try {
    await fs.access(dist);
    return await import(pathToFileURL(dist).href);
  } catch {
    const source = path.join(repoRoot, "packages/hono-party/src/index.ts");
    try {
      const tsx = await import("tsx/esm/api");
      tsx.register();
    } catch {
      try {
        const tsxPath = requireFromRepo.resolve("tsx/esm/api");
        const tsx = await import(pathToFileURL(tsxPath).href);
        tsx.register();
      } catch {}
    }
    return await importFirst([pathToFileURL(source).href]);
  }
}

function packageJsonFor(name) {
  const direct = path.join(repoRoot, "packages", name, "package.json");
  try {
    const require = createRequire(path.join(repoRoot, "package.json"));
    const resolved = require.resolve(`${name}/package.json`);
    return JSON.parse(require("node:fs").readFileSync(resolved, "utf8"));
  } catch {
    try {
      return JSON.parse(requireFromRepo("node:fs").readFileSync(direct, "utf8"));
    } catch {
      return null;
    }
  }
}

const Elysia = await loadElysia();
const middlewareModule = await loadMiddleware();
const partyserverMiddleware = middlewareModule.partyserverMiddleware;
const packageJson = packageJsonFor("hono-party");
const installedVersions = {
  elysia: packageJsonFor("elysia")?.version ?? null,
  partyserver: packageJsonFor("partyserver")?.version ?? null
};

let partyserverImportable = false;
try {
  requireFromRepo.resolve("partyserver");
  partyserverImportable = true;
} catch {
  partyserverImportable = false;
}

function createApp({ ctx, routes = [], route = null, env = {} } = {}) {
  const state = { calls: [], lobby: { id: "room" } };
  setRoutePartykitHandler(makePartyserverRoute({ route, state }));
  setEnv(env);
  let app = new Elysia().use(partyserverMiddleware(ctx));
  for (const entry of routes) {
    app = app[entry.method.toLowerCase()](entry.path, () => entry.handler(state));
  }
  envByApp.set(app, env);
  return { app, state, env };
}

async function handle(app, request) {
  setEnv(envByApp.get(app) ?? {});
  return app.handle(request);
}

async function checkTargetTechnology() {
  const peer = packageJson?.peerDependencies?.elysia;
  const dev = packageJson?.devDependencies?.elysia;
  if (!peer || !dev || !satisfiesRange("1.3.0", peer) || !satisfiesRange("1.3.0", dev)) {
    return { ok: false, detail: "packages/hono-party/package.json does not declare elysia 1.3.0 in peer and dev dependencies" };
  }
  let manifest;
  try {
    const packagePath = requireFromRepo.resolve("elysia/package.json");
    manifest = JSON.parse(requireFromRepo("node:fs").readFileSync(packagePath, "utf8"));
  } catch (error) {
    return { ok: false, detail: `Elysia 1.3.0 is not installed in ${repoRoot}: ${error.message}` };
  }
  if (manifest.version !== "1.3.0") {
    return { ok: false, detail: `loaded Elysia ${manifest.version}, expected 1.3.0` };
  }
  try {
    const scenario = createApp({ route: () => null, routes: [{ method: "get", path: "/health", handler: () => "ok" }] });
    const response = await handle(scenario.app, new Request("http://localhost/health"));
    const text = await response.text();
    return {
      ok: response.status === 200 && text === "ok",
      detail: `Elysia ${manifest.version} plugin probe returned ${response.status} ${text}`
    };
  } catch (error) {
    return { ok: false, detail: `Elysia ${manifest.version} plugin probe failed: ${error.message}` };
  }
}

export const side = {
  name: "target",
  createApp,
  handle,
  packageJson,
  installedVersions,
  partyserverImportable,
  targetTechnology: await checkTargetTechnology()
};

export async function run() {
  return runAll(side);
}

export async function main() {
  const report = await run();
  const output = process.argv[2];
  if (output) {
    await fs.writeFile(output, `${JSON.stringify(report, null, 2)}\n`);
  } else {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  }
  return report;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
