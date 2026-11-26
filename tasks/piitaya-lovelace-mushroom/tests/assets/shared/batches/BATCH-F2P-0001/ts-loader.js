"use strict";

/*
 * Shared on-the-fly TypeScript loader for the BATCH-F2P-0001 probes.
 *
 * Both the source and the target probe drivers use this exact loader, so the
 * semantic predicates run against the real repository sources with one shared
 * execution mechanism.
 *
 * The loader:
 *   - transpiles the repository .ts modules with the repository's own
 *     `typescript` dependency (resolved from the repository node_modules),
 *   - resolves extensionless relative/absolute .ts imports,
 *   - redirects the frontend-only `src/ha/index.ts` barrel to a generated stub,
 *     so schema modules can be imported in Node without booting the whole
 *     browser/Lit graph. The stub lazily resolves the real requested exports.
 *
 * This is an environment shim only: every module under test is executed from
 * its real repository file. No module under test is replaced.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const Module = require("module");

function resolveDep(name, repoRoot) {
  const candidates = [
    path.join(repoRoot, "node_modules", name),
    path.join(__dirname, "node_modules", name),
  ];
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (err) {
      if (err && err.code === "MODULE_NOT_FOUND" && err.message.includes(candidate)) {
        continue;
      }
      throw err;
    }
  }
  return require(name);
}

function createRepo(repoRoot) {
  const root = path.resolve(repoRoot);
  const ts = resolveDep("typescript", root);

  const stubDir = path.join(
    os.tmpdir(),
    "migration-f2p-probe-" +
      crypto.createHash("sha1").update(root).digest("hex")
  );
  fs.mkdirSync(stubDir, { recursive: true });
  const haStub = path.join(stubDir, "ha-stub.cjs");
  fs.writeFileSync(
    haStub,
    "module.exports = require(" + JSON.stringify(path.resolve(
      __dirname, "../../integration/config-validation.cjs"
    )) + ").createHaBarrelStub(" + JSON.stringify(root) + ", require(" +
      JSON.stringify(__filename) + ").resolveDep('typescript', " +
      JSON.stringify(root) + "));\n"
  );

  const compile = (filename, source) => {
    const src = source != null ? source : fs.readFileSync(filename, "utf8");
    return ts.transpileModule(src, {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2020,
        esModuleInterop: true,
        experimentalDecorators: true,
        jsx: ts.JsxEmit.React,
        resolveJsonModule: true,
      },
      fileName: filename,
    }).outputText;
  };

  const withExt = (base) => {
    const candidates = [base, base + ".ts", path.join(base, "index.ts")];
    for (const candidate of candidates) {
      try {
        if (fs.statSync(candidate).isFile()) {
          return candidate;
        }
      } catch (_) {
        /* keep looking */
      }
    }
    return undefined;
  };

  const origResolve = Module._resolveFilename;
  const localResolve = function (request, parent, isMain, options) {
    if (parent && parent.filename && request.startsWith(".")) {
      const resolved = withExt(path.resolve(path.dirname(parent.filename), request));
      if (resolved) {
        return resolved;
      }
    }
    if (path.isAbsolute(request)) {
      const resolved = withExt(request);
      if (resolved) {
        return resolved;
      }
    }
    return origResolve.call(this, request, parent, isMain, options);
  };
  localResolve.__f2p = true;

  const origExtensions = Module._extensions[".ts"];
  Module._extensions[".ts"] = function (module, filename) {
    module._compile(compile(filename), filename);
  };

  const redir = (filename) =>
    filename.replace(/\\/g, "/").endsWith("/src/ha/index.ts")
      ? haStub
      : filename;

  const prev = Module._resolveFilename;
  Module._resolveFilename = function (request, parent, isMain, options) {
    const resolved = localResolve.call(this, request, parent, isMain, options);
    return redir(resolved);
  };

  const req = (rel) => require(path.join(root, rel));

  const restore = () => {
    Module._resolveFilename = prev;
    if (origExtensions) {
      Module._extensions[".ts"] = origExtensions;
    } else {
      delete Module._extensions[".ts"];
    }
  };

  return { root, ts, req, compile, haStub, resolveDep: (n) => resolveDep(n, root), restore };
}

module.exports = { createRepo, resolveDep };
