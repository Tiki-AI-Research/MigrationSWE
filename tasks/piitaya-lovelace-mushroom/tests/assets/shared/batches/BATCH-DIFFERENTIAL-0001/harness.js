"use strict";

// Shared DIFFERENTIAL harness for BATCH-DIFFERENTIAL-0001.
//
// The same normalized projection is applied to the source (Superstruct 2.0.2)
// and target (Zod 3.23.8) probes so that the two sides can be compared with the
// `equal_after_normalization` relation. Normalization keeps only the parts of
// the observable surface that both frameworks are expected to agree on:
//   parse                 -> { decision }
//   handle                -> { decision, entries: sorted "level:keyPath" }
//   handle_native         -> { passthrough, errors }
//   shape                 -> { keys: sorted export keys }
//   capabilities          -> { name: boolean }
//
// `entries` levels are derived from the preserved `ui.errors.config.*`
// localization keys: key_missing -> missing, key_not_expected -> unexpected,
// key_wrong_type -> wrong_type. Union-expansion artifacts (Superstruct reports
// one leaf failure per failed union branch) are collapsed because the preserved
// contract skips union branches.

const fs = require("fs");
const path = require("path");
const ts = require("typescript");

if (typeof globalThis.window === "undefined") {
  globalThis.window = {
    name: "",
    location: { href: "http://localhost/" },
    addEventListener() {},
    removeEventListener() {},
  };
}
if (typeof globalThis.parent === "undefined") globalThis.parent = globalThis.window;
if (typeof globalThis.top === "undefined") globalThis.top = globalThis.window;
if (typeof globalThis.self === "undefined") globalThis.self = globalThis;
if (typeof globalThis.customElements === "undefined") {
  globalThis.customElements = {
    get: () => undefined,
    define() {},
    whenDefined: () => Promise.resolve(),
  };
}
if (typeof globalThis.HTMLElement === "undefined") globalThis.HTMLElement = class {};
if (typeof globalThis.CustomEvent === "undefined") globalThis.CustomEvent = class {};
if (typeof globalThis.requestAnimationFrame === "undefined") {
  globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
}
if (typeof globalThis.cancelAnimationFrame === "undefined") {
  globalThis.cancelAnimationFrame = () => {};
}

require.extensions[".ts"] = function (module, filename) {
  const source = fs.readFileSync(filename, "utf8");
  const out = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2019,
      esModuleInterop: true,
      jsx: ts.JsxEmit.React,
    },
    fileName: filename,
  });
  module._compile(out.outputText, filename);
};

function loadTs(repoRoot, rel) {
  return require(path.join(repoRoot, rel));
}

function makeHass() {
  const calls = [];
  return {
    calls,
    localize: (key, ...args) => {
      calls.push({ key, args });
      return JSON.stringify({ key, args });
    },
  };
}

function normalizeEntry(str) {
  const o = JSON.parse(str);
  let keyPath;
  for (let i = 0; i < o.args.length; i++) {
    if (o.args[i] === "key") {
      keyPath = o.args[i + 1];
      break;
    }
  }
  let level = "other";
  if (o.key.endsWith("key_missing")) level = "missing";
  else if (o.key.endsWith("key_not_expected")) level = "unexpected";
  else if (o.key.endsWith("key_wrong_type")) level = "wrong_type";
  return `${level}:${keyPath}`;
}

function collapseUnionExpansion(err) {
  const failures = err.failures();
  const unionPaths = new Set(
    failures.filter((f) => f.type === "union").map((f) => f.path.join("."))
  );
  const kept = failures.filter(
    (f) => f.type !== "union" && !unionPaths.has(f.path.join("."))
  );
  err.failures = () => kept;
  return err;
}

// adapter = {
//   loadSchemas(repoRoot) -> { [name]: schema },
//   decide(schema, input) -> { accepted: boolean, err: unknown },
//   handle(hass, err, input) -> { warnings: string[], errors?: string[] },
//   schemaKeys(schema) -> string[],
//   hasCapability(schema) -> boolean,
//   handleNative(hass, message) -> { warnings: string[], errors?: string[] },
// }
function runCorpus(corpus, adapter, repoRoot) {
  const schemas = adapter.loadSchemas(repoRoot);
  const results = {};
  for (const c of corpus) {
    results[c.id] = runCase(c, schemas, adapter);
  }
  return results;
}

function runCase(c, schemas, adapter) {
  if (c.op === "shape") {
    return { keys: adapter.schemaKeys(schemas[c.schema]).slice().sort() };
  }
  if (c.op === "capabilities") {
    const out = {};
    for (const name of Object.keys(schemas)) {
      out[name] = adapter.hasCapability(schemas[name]);
    }
    return out;
  }
  if (c.op === "handle_native") {
    const hass = makeHass();
    const res = adapter.handleNative(hass, c.input);
    return { passthrough: res.warnings, errors: res.errors === undefined };
  }
  const { accepted, err } = adapter.decide(schemas[c.schema], c.input);
  const decision = accepted ? "accept" : "reject";
  if (c.op === "parse") return { decision };
  const hass = makeHass();
  const res = accepted
    ? { warnings: [], errors: undefined }
    : adapter.handle(hass, err, c.input);
  const entries = [...(res.errors || []), ...res.warnings]
    .map(normalizeEntry)
    .sort();
  return { decision, entries };
}

module.exports = {
  loadTs,
  makeHass,
  normalizeEntry,
  collapseUnionExpansion,
  runCorpus,
};
