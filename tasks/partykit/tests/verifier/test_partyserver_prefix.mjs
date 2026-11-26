// Run with node --test verifier/test_partyserver_prefix.mjs. TypeScript is
// loaded from the evaluator dependencies, or VERIFIER_NODE_MODULES locally.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { matchesPartyserverPath } from "../assets/shared/batches/partyserver-prefix.mjs";

const require = createRequire(import.meta.url);
const tests = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const assets = path.join(tests, "assets");
function dependency(name) {
  for (const root of [process.env.VERIFIER_NODE_MODULES, "/opt/migration/dependencies/node_modules"]) {
    if (!root) continue;
    try { return createRequire(path.join(root, "noop.js"))(name); } catch {}
  }
  return require(name);
}
const ts = dependency("typescript");

function evaluate(source, modules = {}, globals = {}) {
  const output = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }
  }).outputText;
  const module = { exports: {} };
  const context = {
    module, exports: module.exports,
    require: (name) => name in modules ? modules[name] : require(name),
    Request, Response, Headers, URL, URLSearchParams, TextEncoder, TextDecoder,
    btoa, atob, console, setTimeout, clearTimeout, ...globals
  };
  vm.runInNewContext(output, context);
  return { exports: module.exports, context };
}

let baseline;
if (process.env.PARTYSERVER_BASELINE) {
  // An already extracted baseline also works where child processes are blocked.
  baseline = fs.readFileSync(process.env.PARTYSERVER_BASELINE, "utf8");
} else {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "partyserver-prefix-check-"));
  try {
    execFileSync("git", ["clone", "--bare", path.join(assets, "build-deps/repo/base-repo.bundle"),
      path.join(fixture, "baseline.git")], { stdio: "pipe" });
    baseline = execFileSync("git", ["--git-dir", path.join(fixture, "baseline.git"), "show",
      "HEAD:packages/partyserver/src/index.ts"], { encoding: "utf8" });
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
}
const env = Object.fromEntries(["Chat", "Room", "Extra", "Parties", "Party", "Other", "Api"]
  .map((name) => [name, { idFromName: (value) => value,
    get: () => ({ fetch: async () => new Response("DO") }) }]));
const real = evaluate(baseline, {
  "cloudflare:workers": { DurableObject: class {}, env },
  nanoid: { nanoid: () => "id" }, "./connection": {}, "./transport-errors": {}, "./types": {}
}).exports.routePartykitRequest;

const helper = { matchesPartyserverPath };
const suite = evaluate(fs.readFileSync(path.join(assets, "shared/batches/BATCH-F2P-0002/suite.mjs"), "utf8"), {
  "../partyserver-prefix.mjs": helper,
  "node:module": { createRequire: () => dependency }
}).exports;
// Only evaluate the actual mock initializer: the harness's ESM runtime loader
// uses import.meta and does not belong in this CommonJS routing-only sandbox.
const harnessSource = fs.readFileSync(path.join(assets, "shared/batches/BATCH-F2P-0003/harness.mjs"), "utf8");
const harnessAST = ts.createSourceFile("harness.mjs", harnessSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
let mockInitializer;
function findMock(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(harnessAST) === "MOCK_PARTYSERVER") {
    mockInitializer = node.initializer.getText(harnessAST);
  }
  ts.forEachChild(node, findMock);
}
findMock(harnessAST);
assert.ok(mockInitializer, "the actual F2P-0003 mock must be exercised");
const virtual = evaluate(vm.runInNewContext(mockInitializer, helper)).exports.routePartykitRequest;
const f2p1 = evaluate(fs.readFileSync(path.join(assets, "fixtures/batches/BATCH-F2P-0001/partyserver-mock.mjs"), "utf8"), {
  "../../../shared/batches/partyserver-prefix.mjs": helper
}).exports.routePartykitRequest;
const differential = evaluate(fs.readFileSync(path.join(assets, "fixtures/batches/BATCH-DIFFERENTIAL-0001/route-mock.mjs"), "utf8"), {
  "../../../shared/batches/partyserver-prefix.mjs": helper
}).exports;
differential.reset(({ request, options }) => options.onBeforeRequest(request, {}));
const p2pText = fs.readFileSync(path.join(assets, "target/batches/BATCH-P2P-0002/p2p.test.mjs"), "utf8");
const p2pAST = ts.createSourceFile("p2p.mjs", p2pText, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
let hoisted;
function findHoisted(node) {
  if (ts.isCallExpression(node) && node.expression.getText(p2pAST) === "vi.hoisted") {
    hoisted = node.arguments[0].getText(p2pAST);
  }
  ts.forEachChild(node, findHoisted);
}
findHoisted(p2pAST);
assert.ok(hoisted, "the actual P2P mock must be exercised");
const p2p = vm.runInNewContext(`(${hoisted})()`, { Request, Response, URL });

const routes = {
  real,
  "F2P-0001": f2p1,
  "F2P-0002": suite.makePartyserverRoute({ state: { calls: [], lobby: {} } }),
  "F2P-0003": virtual,
  differential: differential.routePartykitRequest,
  "P2P-0002": p2p.routePartykitRequest
};

test("every prefix mock agrees with the real baseline router", async () => {
  const prefixes = [undefined, null, "", "parties", "party", "/party", "party/", "/party/",
    "api/party", "api//party", "parties/chat"];
  const paths = ["/parties/chat/room", "/party/chat/room", "/party", "/party/chat",
    "/partyman/chat/room", "/api/party/chat/room", "/party//chat//room",
    "/parties/chat/room/extra", "/other", "/"];
  for (const prefix of prefixes) {
    for (const pathname of paths) {
      const expected = matchesPartyserverPath(pathname, prefix);
      for (const [name, route] of Object.entries(routes)) {
        let calls = 0;
        await route(new Request(`http://localhost${pathname}`), env, {
          prefix, onBeforeRequest: () => { calls++; return new Response("hook"); }
        });
        assert.equal(calls, Number(expected), `${name}: ${JSON.stringify({ prefix, pathname })}`);
      }
    }
  }
});

test("prefix does not leak hooks to websocket requests outside its path", async () => {
  for (const [name, route] of Object.entries(routes)) {
    let calls = 0;
    await route(new Request("http://localhost/other", { headers: { Upgrade: "WebSocket" } }), env, {
      prefix: "party", onBeforeConnect: () => { calls++; return new Response("hook"); }
    });
    assert.equal(calls, 0, name);
  }
});

test("changed differential inputs retain strict native option forwarding", async () => {
  const casesFile = path.join(assets, "shared/batches/BATCH-DIFFERENTIAL-0001/cases.mjs");
  const { cases } = await import(casesFile);
  const frozen = JSON.parse(fs.readFileSync(path.join(assets, "source-baseline-outputs.json"), "utf8"));
  for (const id of ["DIFF-001", "DIFF-004", "DIFF-005", "DIFF-012"]) {
    const example = cases.find((item) => item.id === id);
    assert.ok(example.expected.prefix === "party" || example.expected.prefix === "parties");
    const row = frozen.observations.find((item) => item.id === id);
    assert.equal(row.value.value.prefix, example.expected.prefix, id);
  }
  const projections = JSON.parse(fs.readFileSync(path.join(assets, "shared/integration/p2p-projections.json"), "utf8"));
  assert.equal(projections["BATCH-P2P-0002"]["P2P-036"].options.prefix, "party");
  assert.equal(projections["BATCH-P2P-0002"]["P2P-037"].prefix, "party");
});
