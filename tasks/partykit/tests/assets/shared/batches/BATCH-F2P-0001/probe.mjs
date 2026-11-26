import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { satisfiesRange } from "../BATCH-F2P-0002/suite.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES =
  process.env.FIXTURES_DIR ??
  path.resolve(HERE, "../../../fixtures/batches/BATCH-F2P-0001");
const REPO_ROOT = process.env.REPO_ROOT ?? path.resolve(HERE, "../../../target");
const BASELINE_ROOT = process.env.BASELINE_ROOT ?? REPO_ROOT;
const PACKAGE_ENTRY =
  process.env.PACKAGE_ENTRY ??
  path.join(REPO_ROOT, "packages/hono-party/src/index.ts");

function firstExisting(candidates) {
  return candidates.find(Boolean);
}

const ELYSIA_ENTRY = firstExisting([
  process.env.ELYSIA_ENTRY,
  path.join(REPO_ROOT, "node_modules/elysia/dist/index.mjs"),
  "/opt/migration/dependencies/node_modules/elysia/dist/index.mjs"
]);

const PARTYSERVER_MOCK_URL = pathToFileURL(
  path.join(FIXTURES, "partyserver-mock.mjs")
).href;

process.env.PARTYSERVER_MOCK_URL = PARTYSERVER_MOCK_URL;
process.env.CLOUDFLARE_STUB_URL = pathToFileURL(
  path.join(FIXTURES, "cloudflare-workers-stub.mjs")
).href;

globalThis.__cloudflareEnv ??= {};

const { Elysia } = await import(pathToFileURL(ELYSIA_ENTRY).href);
const mock = await import(PARTYSERVER_MOCK_URL);
const packageModule = await import(pathToFileURL(PACKAGE_ENTRY).href);
const partyserverMiddleware = packageModule.partyserverMiddleware;

async function assertFrozenTargetTechnology() {
  const manifest = JSON.parse(
    await readFile(
      path.resolve(path.dirname(ELYSIA_ENTRY), "../package.json"),
      "utf8"
    )
  );
  assertEqual(manifest.name, "elysia", "target technology name");
  assertEqual(manifest.version, "1.3.0", "target technology version");
}

const OBSERVATION_ORDER = [
  "F2P-001",
  "F2P-002",
  "F2P-003",
  "F2P-004",
  "F2P-007",
  "F2P-008",
  "F2P-009",
  "F2P-011",
  "F2P-013",
  "F2P-019",
  "F2P-005",
  "F2P-006",
  "F2P-010",
  "F2P-016",
  "F2P-018",
  "F2P-020",
  "F2P-022",
  "F2P-012",
  "F2P-014",
  "F2P-015",
  "F2P-017",
  "F2P-021",
  "F2P-023",
  "F2P-024"
];

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function assertEqual(actual, expected, label) {
  assert(
    Object.is(actual, expected),
    `${label}: expected ${JSON.stringify(expected)}, received ${JSON.stringify(actual)}`
  );
}

function assertDeepEqual(actual, expected, label) {
  const actualJson = JSON.stringify(actual);
  const expectedJson = JSON.stringify(expected);
  assert(
    actualJson === expectedJson,
    `${label}: expected ${expectedJson}, received ${actualJson}`
  );
}

function resetRuntime(environment = {}) {
  mock.resetCalls();
  globalThis.__cloudflareEnv = environment;
}

async function readJson(relativePath) {
  const text = await readFile(path.join(REPO_ROOT, relativePath), "utf8");
  return JSON.parse(text);
}

async function r6RegistrationOrder() {
  resetRuntime();
  mock.setHandler(async () => null);

  const hits = [];
  const app = new Elysia()
    .use(
      partyserverMiddleware({
        options: {
          onBeforeRequest: () => {
            hits.push("hook");
          }
        }
      })
    )
    .get("/parties/chat/room", () => {
      hits.push("route");
      return "routed";
    });

  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );

  assertEqual(response.status, 200, "status");
  assertEqual(await response.text(), "routed", "body");
  assertDeepEqual(hits, ["hook", "route"], "hook/route order");
  assertEqual(mock.calls.length, 1, "routePartykitRequest call count");
}

async function r25ConnectResponseTakeover() {
  resetRuntime();
  mock.setHandler(async () => null);

  const downstream = [];
  const app = new Elysia()
    .use(
      partyserverMiddleware({
        options: {
          onBeforeConnect: () =>
            new Response("forbidden", {
              status: 403,
              headers: { "x-rejected": "yes" }
            })
        }
      })
    )
    .get("/parties/chat/room", () => {
      downstream.push("called");
      return "downstream";
    });

  const response = await app.handle(
    new Request("http://localhost/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    })
  );

  assertEqual(response.status, 403, "status");
  assertEqual(response.headers.get("x-rejected"), "yes", "x-rejected header");
  assertEqual(await response.text(), "forbidden", "body");
  assertDeepEqual(downstream, [], "downstream route calls");
}

async function r5ElysiaRegistration() {
  resetRuntime();

  const plugin = partyserverMiddleware();
  mock.setHandler(async () => null);
  const app = new Elysia().use(plugin)
    .get("/health", () => "ok")
    .get("/parties/chat/room", () => "downstream");

  const response = await app.handle(new Request("http://localhost/health"));
  assertEqual(response.status, 200, "status");
  assertEqual(await response.text(), "ok", "body");

  // Exercise registration rather than requiring a function-shaped plugin
  // or a particular Elysia lifecycle hook.
  mock.resetCalls();
  mock.setHandler(async () => new Response("registered", { status: 202 }));
  const routed = await app.handle(new Request("http://localhost/parties/chat/room"));
  assertEqual(routed.status, 202, "registered middleware status");
  assertEqual(await routed.text(), "registered", "registered middleware body");
  assertEqual(mock.calls.length, 1, "registered middleware delegation");
}

async function r35ManifestHonoRemoved() {
  const manifest = await readJson("packages/hono-party/package.json");

  assert(
    !Object.prototype.hasOwnProperty.call(
      manifest.peerDependencies ?? {},
      "hono"
    ),
    "peerDependencies still declares hono"
  );
  assert(
    !Object.prototype.hasOwnProperty.call(
      manifest.devDependencies ?? {},
      "hono"
    ),
    "devDependencies still declares hono"
  );

  for (const group of ["peerDependencies", "devDependencies"]) {
    const range = manifest[group]?.elysia;
    assert(
      satisfiesRange("1.3.0", range),
      `${group} does not declare an Elysia range containing 1.3.0`
    );
  }
}

async function r2OnErrorThenNext() {
  resetRuntime();
  mock.setHandler(async () => null);

  const seen = [];
  const downstream = [];
  const app = new Elysia()
    .use(
      partyserverMiddleware({
        options: {
          onBeforeRequest: () => {
            throw new Error("boom");
          }
        },
        onError: (error) => seen.push(error.message)
      })
    )
    .get("/parties/chat/room", () => {
      downstream.push("called");
      return "downstream";
    });

  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );

  assertDeepEqual(seen, ["boom"], "onError messages");
  assertEqual(response.status, 200, "status");
  assertEqual(await response.text(), "downstream", "body");
  assertDeepEqual(downstream, ["called"], "downstream route calls");
}

async function r12ContextEnvironment() {
  resetRuntime({ JWT_SECRET: "s3cr3t" });
  mock.setHandler(async () => null);

  const observed = {};
  const app = new Elysia()
    .use(
      partyserverMiddleware({
        options: {
          onBeforeRequest: (_request, _lobby, context) => {
            observed.secret = context.env.JWT_SECRET;
            observed.url = context.request.url;
          }
        }
      })
    )
    .get("/parties/chat/room", () => "downstream");

  const response = await app.handle(
    new Request("http://localhost/parties/chat/room")
  );

  assertEqual(observed.secret, "s3cr3t", "context.env.JWT_SECRET");
  assertEqual(
    observed.url,
    "http://localhost/parties/chat/room",
    "context.request.url"
  );
  assertEqual(response.status, 200, "status");
}

async function r1ConnectUnauthorized() {
  resetRuntime();
  mock.setHandler(async () => null);

  const downstream = [];
  const app = new Elysia()
    .use(
      partyserverMiddleware({
        options: {
          onBeforeConnect: () =>
            new Response("Unauthorized", { status: 401 })
        }
      })
    )
    .get("/parties/chat/room", () => {
      downstream.push("called");
      return "downstream";
    });

  const response = await app.handle(
    new Request("http://localhost/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    })
  );

  assertEqual(response.status, 401, "status");
  assertEqual(await response.text(), "Unauthorized", "body");
  assertDeepEqual(downstream, [], "downstream route calls");
}

async function r22UpgradeResponseWithoutWebSocket() {
  resetRuntime();
  mock.setHandler(async () => new Response("not found", { status: 404 }));

  const downstream = [];
  const app = new Elysia()
    .use(partyserverMiddleware())
    .get("/parties/chat/room", () => {
      downstream.push("called");
      return "downstream";
    });

  const response = await app.handle(
    new Request("http://localhost/parties/chat/room", {
      headers: { Upgrade: "websocket" }
    })
  );

  assertEqual(response.status, 404, "status");
  assertEqual(await response.text(), "not found", "body");
  assertDeepEqual(downstream, [], "downstream route calls");
}

async function r32ProductionGraphUsesNoHono() {
  const sourcePath = path.join(
    REPO_ROOT,
    "packages/hono-party/src/index.ts"
  );
  const source = await readFile(sourcePath, "utf8");
  const specifiers = [
    ...source.matchAll(
      /\b(?:from\s*|import\s*\(\s*)["']([^"']+)["']/g
    )
  ].map((match) => match[1]);

  assert(specifiers.length > 0, "no module specifiers found");

  const honoSpecifiers = specifiers.filter(
    (specifier) => specifier === "hono" || specifier.startsWith("hono/")
  );
  assertDeepEqual(honoSpecifiers, [], "Hono module specifiers");
  assert(
    specifiers.includes("elysia"),
    "production graph does not load Elysia"
  );
}

function collectChangedFiles(left, right, relative = "") {
  const pathA = path.join(left, relative);
  const pathB = path.join(right, relative);

  return (async () => {
    const [entriesA, entriesB] = await Promise.all([
      readdir(pathA, { withFileTypes: true }).catch(() => []),
      readdir(pathB, { withFileTypes: true }).catch(() => [])
    ]);
    const names = new Set([
      ...entriesA.map((entry) => entry.name),
      ...entriesB.map((entry) => entry.name)
    ]);
    const changed = [];

    for (const name of [...names].sort()) {
      if (
        name === ".git" ||
        name === "node_modules" ||
        name === "dist" ||
        name === ".turbo"
      ) {
        continue;
      }

      const nextRelative = relative ? `${relative}/${name}` : name;
      const entryA = entriesA.find((entry) => entry.name === name);
      const entryB = entriesB.find((entry) => entry.name === name);

      if (!entryA || !entryB) {
        changed.push(nextRelative);
        continue;
      }

      if (entryA.isDirectory() !== entryB.isDirectory()) {
        changed.push(nextRelative);
        continue;
      }

      if (entryA.isDirectory()) {
        changed.push(
          ...(await collectChangedFiles(left, right, nextRelative))
        );
        continue;
      }

      const [contentA, contentB] = await Promise.all([
        readFile(path.join(left, nextRelative)),
        readFile(path.join(right, nextRelative))
      ]);
      if (!contentA.equals(contentB)) changed.push(nextRelative);
    }

    return changed;
  })();
}

function isAllowedChange(relativePath) {
  if (relativePath === ".wrangler" || relativePath.startsWith(".wrangler/")) {
    return true;
  }
  if (
    relativePath === "packages/hono-party/src/index.ts" ||
    relativePath === "packages/hono-party/package.json"
  ) {
    return true;
  }

  if (
    /^packages\/hono-party\/(?:.+\/)?(?:test|tests)(?:\/.*)?$/.test(
      relativePath
    )
  ) {
    return true;
  }
  if (/^packages\/hono-party\/(?:vitest|jest|playwright)\.config\.[cm]?[jt]sx?$/.test(relativePath)) {
    return true;
  }
  if (/^packages\/hono-party\/tsconfig\.(?:test|spec)\.json$/.test(relativePath)) {
    return true;
  }

  return /^packages\/hono-party\/(?:.+\/)?(?:.+\.)?(?:test|spec)\.[cm]?[jt]sx?$/.test(
    relativePath
  );
}

async function r34RepositoryChangeScope() {
  const changed = await collectChangedFiles(BASELINE_ROOT, REPO_ROOT);
  const disallowed = changed.filter((relativePath) => !isAllowedChange(relativePath));

  assertDeepEqual(disallowed, [], "changes outside packages/hono-party");
  assert(
    changed.includes("packages/hono-party/src/index.ts"),
    "adapter implementation was not migrated"
  );
  assert(
    changed.includes("packages/hono-party/package.json"),
    "package manifest was not migrated"
  );

  const implementation = await readFile(
    path.join(REPO_ROOT, "packages/hono-party/src/index.ts"),
    "utf8"
  );
  assert(
    /\bfrom\s*["']elysia["']/.test(implementation),
    "migrated adapter does not load Elysia"
  );

  const importSpecifiers = [];
  for (const match of implementation.matchAll(
    /\bfrom\s*["']([^"']+)["']|\bimport\s*\(\s*["']([^"']+)["']\s*\)|\bimport\s*["']([^"']+)["']/g
  )) {
    importSpecifiers.push(match[1] ?? match[2] ?? match[3]);
  }
  assert(
    importSpecifiers.every(
      (specifier) => !/(?:^|\/)(?:test|tests)\//.test(specifier)
    ),
    "adapter implementation must not import test helpers"
  );
}

const PREDICATES = {
  R6: r6RegistrationOrder,
  R25: r25ConnectResponseTakeover,
  R5: r5ElysiaRegistration,
  R35: r35ManifestHonoRemoved,
  R2: r2OnErrorThenNext,
  R12: r12ContextEnvironment,
  R1: r1ConnectUnauthorized,
  R22: r22UpgradeResponseWithoutWebSocket,
  R32: r32ProductionGraphUsesNoHono,
  R34: r34RepositoryChangeScope
};

const OBSERVATIONS = {
  "F2P-001": ["R006", "R6"],
  "F2P-002": ["R025", "R25"],
  "F2P-003": ["R005", "R5"],
  "F2P-004": ["R035", "R35"],
  "F2P-007": ["R002", "R2"],
  "F2P-008": ["R012", "R12"],
  "F2P-009": ["R001", "R1"],
  "F2P-011": ["R022", "R22"],
  "F2P-013": ["R032", "R32"],
  "F2P-019": ["R034", "R34"],
  "F2P-005": ["R006", "R6"],
  "F2P-006": ["R006", "R6"],
  "F2P-010": ["R005", "R5"],
  "F2P-016": ["R025", "R25"],
  "F2P-018": ["R025", "R25"],
  "F2P-020": ["R005", "R5"],
  "F2P-022": ["R001", "R1"],
  "F2P-012": ["R025", "R25"],
  "F2P-014": ["R002", "R2"],
  "F2P-015": ["R006", "R6"],
  "F2P-017": ["R012", "R12"],
  "F2P-021": ["R001", "R1"],
  "F2P-023": ["R006", "R6"],
  "F2P-024": ["R012", "R12"]
};

const results = [];

for (const observationId of OBSERVATION_ORDER) {
  const [requirementId, predicateName] = OBSERVATIONS[observationId];
  try {
    await assertFrozenTargetTechnology();
    await PREDICATES[predicateName]();
    results.push({
      observation_id: observationId,
      requirement_id: requirementId,
      status: "PASS"
    });
  } catch (error) {
    results.push({
      observation_id: observationId,
      requirement_id: requirementId,
      status: "FAIL",
      error: error instanceof Error ? error.message : String(error)
    });
  }
}

process.stdout.write(
  `${JSON.stringify({ repo_root: REPO_ROOT, side: process.env.SIDE ?? null, results }, null, 2)}\n`
);
