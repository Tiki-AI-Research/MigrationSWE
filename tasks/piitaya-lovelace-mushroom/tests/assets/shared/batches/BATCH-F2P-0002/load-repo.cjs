"use strict";

// Repo module loader for BATCH-F2P-0002.
//
// Loads the migrated configuration modules from a repository working tree by
// bundling them with the repository's own bundler (rolldown, a transitive
// dependency of vite) and executing the resulting CommonJS bundle in-process.
//
// The reference target is only ever read while authoring; this helper never
// copies repository sources into the public asset tree.

const fs = require("fs");
const os = require("os");
const path = require("path");
const { createRequire } = require("module");
const { createValidationResult, editorAssertionBinding } = require("../../integration/config-validation.cjs");

const SYMBOL_IMPORTS = [
  ["entitySharedConfigStruct", "src/shared/config/entity-config.ts"],
  ["entityNameStruct", "src/shared/config/entity-config.ts"],
  ["appearanceSharedConfigStruct", "src/shared/config/appearance-config.ts"],
  ["lovelaceCardConfigStruct", "src/shared/config/lovelace-card-config.ts"],
  ["lovelaceBadgeConfigStruct", "src/shared/config/lovelace-badge-config.ts"],
  ["actionsSharedConfigStruct", "src/shared/config/actions-config.ts"],
  ["layoutStruct", "src/utils/layout.ts"],
  ["actionConfigStruct", "src/ha/panels/lovelace/editor/structs/action-struct.ts"],
  ["entityCardConfigStruct", "src/cards/entity-card/entity-card-config.ts"],
  ["handleStructError", "src/ha/common/structs/handle-errors.ts"],
];

function depDirs(repoRoot) {
  const dirs = [path.join(repoRoot, "node_modules")];
  if (process.env.DEPS_NODE_MODULES) {
    dirs.push(process.env.DEPS_NODE_MODULES);
  }
  dirs.push("/opt/migration/dependencies/node_modules");
  return dirs.filter((d) => {
    try {
      return fs.statSync(d).isDirectory();
    } catch {
      return false;
    }
  });
}

function makeResolver(dirs) {
  const requires = dirs.map((d) => createRequire(path.join(d, "noop.js")));
  return function resolve(spec) {
    for (const r of requires) {
      try {
        return r.resolve(spec);
      } catch {
        /* try next */
      }
    }
    return null;
  };
}

async function loadSymbols(repoRoot) {
  const dirs = depDirs(repoRoot);
  const resolve = makeResolver(dirs);
  const rolldownPath = resolve("rolldown");
  if (!rolldownPath) {
    throw new Error("rolldown is not resolvable from " + dirs.join(", "));
  }
  const mod = await import(rolldownPath);
  const rolldown = mod.rolldown || mod.build;
  if (typeof rolldown !== "function") {
    throw new Error("rolldown module does not export a build function");
  }

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "batch-f2p-0002-"));
  const entry = path.join(tmp, "entry.ts");
  const outFile = path.join(tmp, "bundle.cjs");

  // The generated bundle keeps zod/superstruct external so the dependency
  // policy predicate can observe which framework runtime is actually loaded.
  // Link a node_modules directory next to the bundle so those externals
  // resolve at require time.
  const linked = dirs.find((d) => {
    try {
      return fs.existsSync(path.join(d, "zod")) || fs.existsSync(path.join(d, "superstruct"));
    } catch {
      return false;
    }
  });
  if (linked) {
    try {
      fs.symlinkSync(linked, path.join(tmp, "node_modules"), "dir");
    } catch {
      /* resolution may still succeed via other means */
    }
  }

  const lines = [];
  for (const [name, rel] of SYMBOL_IMPORTS) {
    lines.push(
      'import { ' + name + ' } from ' + JSON.stringify(path.join(repoRoot, rel)) + ";"
    );
  }
  lines.push('import * as configErrorHandling from ' + JSON.stringify(path.join(
    repoRoot, "src/ha/common/structs/handle-errors.ts"
  )) + ";");
  lines.push("export { configErrorHandling };");
  const ts = require(resolve("typescript"));
  const assertionBinding = editorAssertionBinding(repoRoot, ts);
  if (assertionBinding) {
    const specifier = assertionBinding.source.startsWith(".") ? path.resolve(
      repoRoot, "src/cards/entity-card", assertionBinding.source
    ) : assertionBinding.source;
    lines.push("export { " + JSON.stringify(assertionBinding.name) +
      " as candidateEditorAssertion } from " + JSON.stringify(specifier) + ";");
  }
  lines.push(
    "export { " + SYMBOL_IMPORTS.map(([n]) => n).join(", ") + " };"
  );
  fs.writeFileSync(entry, lines.join("\n") + "\n");

  const plugin = {
    name: "resolve-repo-deps",
    resolveId(source) {
      if (source.startsWith(".") || source.startsWith("/")) {
        return null;
      }
      if (source === "zod" || source === "superstruct") {
        return { id: source, external: true };
      }
      const resolved = resolve(source);
      return resolved ? { id: resolved } : { id: source, external: true };
    },
  };

  const bundle = await rolldown({
    input: entry,
    platform: "node",
    treeshake: { moduleSideEffects: false },
    plugins: [plugin],
  });
  await bundle.write({ file: outFile, format: "cjs" });

  const symbols = require(outFile);
  symbols.validateConfig = createValidationResult(
    repoRoot, symbols.configErrorHandling, ts, symbols.candidateEditorAssertion || null
  );
  const missing = SYMBOL_IMPORTS.map(([n]) => n).filter((n) => !(n in symbols));
  if (missing.length) {
    throw new Error("migrated modules did not export: " + missing.join(", "));
  }
  return { ...symbols, handleStructError: symbols.validateConfig.handleStructError };
}

module.exports = { loadSymbols, SYMBOL_IMPORTS };
