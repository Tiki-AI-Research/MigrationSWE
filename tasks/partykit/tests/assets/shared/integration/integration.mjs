import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import cp from "node:child_process";
import zlib from "node:zlib";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

export const REPORT_SCHEMA = "migration-observations:v6";

const REPO_ENV_KEYS = ["MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"];
const NODE_FLAGS = ["--preserve-symlinks", "--preserve-symlinks-main"];

export function assetRoot() {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
}

export function resolveRepoRoot(explicit) {
  if (explicit) return path.resolve(explicit);
  for (const key of REPO_ENV_KEYS) {
    if (process.env[key]) return path.resolve(process.env[key]);
  }
  const cwd = process.cwd();
  if (fs.existsSync(path.join(cwd, "packages", "hono-party", "package.json"))) {
    return cwd;
  }
  return "/workspace/repo";
}

function existsSync(target) {
  try {
    fs.accessSync(target);
    return true;
  } catch {
    return false;
  }
}

function readJson(target) {
  return JSON.parse(fs.readFileSync(target, "utf8"));
}

function truncate(value, limit = 1200) {
  const text = String(value ?? "");
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}

function runProcess(command, args, options = {}) {
  return new Promise((resolve) => {
    const child = cp.spawn(command, args, {
      cwd: options.cwd,
      env: options.env || process.env,
      stdio: ["ignore", "pipe", "pipe"]
    });
    const stdout = [];
    const stderr = [];
    let finished = false;
    let timedOut = false;
    let spawnError = null;
    const timer =
      options.timeout === 0
        ? null
        : setTimeout(() => {
            timedOut = true;
            child.kill("SIGKILL");
          }, options.timeout ?? 600000);

    child.stdout.on("data", (chunk) => stdout.push(Buffer.from(chunk)));
    child.stderr.on("data", (chunk) => stderr.push(Buffer.from(chunk)));

    const finish = (code, signal) => {
      if (finished) return;
      finished = true;
      if (timer) clearTimeout(timer);
      const out = Buffer.concat(stdout);
      const err = Buffer.concat(stderr);
      resolve({
        code,
        signal,
        timedOut,
        spawnError,
        stdoutBuffer: out,
        stderrBuffer: err,
        stdout: out.toString("utf8"),
        stderr: err.toString("utf8")
      });
    };

    child.on("error", (error) => {
      spawnError = error.code || error.message;
      finish(null, null);
    });
    child.on("close", (code, signal) => finish(code, signal));
  });
}

function extractJsonObject(text) {
  const source = String(text || "");
  const marker = source.indexOf('"repo_root"');
  const preferred = marker >= 0 ? source.lastIndexOf("{", marker) : -1;
  const candidates = [];
  if (preferred >= 0) candidates.push(preferred);
  for (let index = source.length - 1; index >= 0; index -= 1) {
    if (source[index] === "{") candidates.push(index);
  }
  for (const start of candidates) {
    try {
      return JSON.parse(source.slice(start));
    } catch {}
  }
  return null;
}

async function readJsonOrNull(target) {
  try {
    return await fsp.readFile(target, "utf8").then((text) => JSON.parse(text));
  } catch {
    return null;
  }
}

async function removeQuietly(target) {
  await fsp.rm(target, { recursive: true, force: true });
}

async function makeTempDir(prefix) {
  return fsp.mkdtemp(path.join(os.tmpdir(), prefix));
}

async function makeReportPath() {
  const directory = await makeTempDir("migration-report-");
  return path.join(directory, "report.json");
}

function resolvePackageEntry(repoRoot, name) {
  try {
    const requireFromRepo = createRequire(path.join(repoRoot, "package.json"));
    return requireFromRepo.resolve(name);
  } catch {
    return null;
  }
}

function readPackageVersion(packageRoot) {
  try {
    return readJson(path.join(packageRoot, "package.json")).version || null;
  } catch {
    return null;
  }
}

function dependencyRoots(repoRoot) {
  const candidates = [
    process.env.MIGRATION_DEPENDENCIES_NODE_MODULES,
    process.env.F2P_DEPENDENCIES_NODE_MODULES,
    path.join(repoRoot, "node_modules"),
    "/opt/migration/dependencies/node_modules",
    path.join(assetRoot(), "dependencies", "node_modules")
  ].filter(Boolean);
  const seen = new Set();
  return candidates.filter((candidate) => {
    const resolved = path.resolve(candidate);
    if (seen.has(resolved) || !existsSync(resolved)) return false;
    seen.add(resolved);
    return true;
  });
}

function resolveVitest(repoRoot) {
  for (const root of dependencyRoots(repoRoot)) {
    const localBin = path.join(root, ".bin", "vitest");
    if (existsSync(localBin)) return { command: localBin, prefixArgs: [] };
    const localModule = path.join(root, "vitest", "vitest.mjs");
    if (existsSync(localModule)) {
      return { command: process.execPath, prefixArgs: [localModule] };
    }
  }
  return null;
}

function resolveAdapterEntry(repoRoot) {
  const dist = path.join(repoRoot, "packages", "hono-party", "dist", "index.js");
  if (existsSync(dist)) return dist;
  return path.join(repoRoot, "packages", "hono-party", "src", "index.ts");
}

function elysiaCandidates(nodeModulesRoot) {
  return [
    path.join(nodeModulesRoot, "elysia", "dist", "index.mjs"),
    path.join(nodeModulesRoot, "elysia", "dist", "index.js"),
    path.join(nodeModulesRoot, "elysia", "dist", "cjs", "index.js")
  ];
}

function resolveElysiaEntry(repoRoot) {
  let fallback = null;
  for (const root of dependencyRoots(repoRoot)) {
    const candidate = elysiaCandidates(root).find((entry) => existsSync(entry));
    if (!candidate) continue;
    if (!fallback) fallback = candidate;
    if (readPackageVersion(path.join(root, "elysia")) === "1.3.0") {
      return candidate;
    }
  }
  return fallback || resolvePackageEntry(repoRoot, "elysia");
}

function resolveDependencyRoot(repoRoot) {
  for (const root of dependencyRoots(repoRoot)) {
    if (readPackageVersion(path.join(root, "elysia")) === "1.3.0") return root;
  }
  return dependencyRoots(repoRoot)[0] || path.join(repoRoot, "node_modules");
}

function resolveTsxImport(repoRoot) {
  for (const root of dependencyRoots(repoRoot)) {
    try {
      const requireFromRoot = createRequire(path.join(root, "noop.js"));
      return requireFromRoot.resolve("tsx");
    } catch {}
  }
  return "tsx";
}

function flattenVitest(report) {
  const rows = [];
  const suites = report?.testResults || report?.testSuites || [];
  for (const suite of suites) {
    for (const assertion of suite.assertionResults || []) {
      rows.push({
        title: assertion.title || assertion.fullName || "",
        fullName: assertion.fullName || assertion.title || "",
        status: assertion.status || suite.status || "failed",
        messages: assertion.failureMessages || []
      });
    }
  }
  return rows;
}

function idsFromTitle(title) {
  return [...String(title || "").matchAll(/\b(P2P-\d{3})\b/g)].map((match) => match[1]);
}

function record(results, id, status, value) {
  results.set(id, { status, value });
}

function recordPassFail(results, id, passed) {
  record(results, id, passed ? "PASS" : "FAIL", {
    result: passed ? "PASS" : "FAIL"
  });
}

function applyRows(results, ids, rows) {
  const allowed = new Set(ids);
  const seen = new Map();
  for (const row of rows) {
    const titleIds = [
      ...idsFromTitle(row.title),
      ...idsFromTitle(row.fullName),
      ...idsFromTitle(row.messages?.join(" "))
    ];
    for (const id of titleIds) {
      if (!allowed.has(id)) continue;
      if (!seen.has(id)) seen.set(id, { passed: true });
      if (row.status !== "passed") seen.get(id).passed = false;
    }
  }
  for (const [id, state] of seen) recordPassFail(results, id, state.passed);
  return seen;
}

function loadP2PProjections(assets, batch) {
  const projections = {};
  const planPath = path.join(
    assets,
    "shared",
    "batches",
    batch,
    "observation-plan.json"
  );
  const plan = (() => {
    try {
      return readJson(planPath);
    } catch {
      return null;
    }
  })();
  if (plan) {
    for (const item of plan.native_tests || []) {
      if (item.observation_id && item.normalized_value) {
        projections[item.observation_id] = item.normalized_value;
      }
    }
    for (const item of plan.probes || []) {
      if (item.observation_id && item.normalized_value) {
        projections[item.observation_id] = item.normalized_value;
      }
    }
  }
  const sharedPath = path.join(
    assets,
    "shared",
    "integration",
    "p2p-projections.json"
  );
  try {
    const shared = readJson(sharedPath);
    Object.assign(projections, shared?.[batch] || {});
  } catch {}
  return projections;
}

function extractProjectionLogs(text) {
  const projections = {};
  for (const line of String(text || "").split("\n")) {
    if (!line.startsWith("P2P_PROJECTION ")) continue;
    try {
      const entry = JSON.parse(line.slice("P2P_PROJECTION ".length));
      if (entry?.observation_id) projections[entry.observation_id] = entry.value;
    } catch {}
  }
  return projections;
}

function applyProjections(results, ids, projections) {
  for (const id of ids) {
    const result = results.get(id);
    if (result?.status !== "PASS") continue;
    if (projections[id] !== undefined) {
      record(results, id, "PASS", projections[id]);
    }
  }
}

function fillMissing(results, ids) {
  for (const id of ids) {
    if (!results.has(id)) {
      record(results, id, "INCONCLUSIVE", { result: "INCONCLUSIVE" });
    }
  }
}

async function ensureBuild(repoRoot) {
  if (!existsSync(path.join(repoRoot, "node_modules"))) return;
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const targets = [
    ["partyserver", path.join(repoRoot, "packages", "partyserver", "dist", "index.js")],
    ["hono-party", path.join(repoRoot, "packages", "hono-party", "dist", "index.js")]
  ];
  for (const [workspace, dist] of targets) {
    if (existsSync(dist)) continue;
    await runProcess(npm, ["run", "build", "-w", workspace], {
      cwd: repoRoot,
      timeout: 900000
    });
  }
}

function readGitObject(gitDir, sha) {
  const objectPath = path.join(gitDir, "objects", sha.slice(0, 2), sha.slice(2));
  const raw = zlib.inflateSync(fs.readFileSync(objectPath));
  const separator = raw.indexOf(0);
  if (separator < 0) throw new Error("invalid git object");
  const header = raw.subarray(0, separator).toString("utf8");
  const space = header.indexOf(" ");
  return {
    type: header.slice(0, space),
    body: raw.subarray(separator + 1)
  };
}

function resolveGitHead(gitDir) {
  const head = fs.readFileSync(path.join(gitDir, "HEAD"), "utf8").trim();
  if (!head.startsWith("ref: ")) return head;
  const ref = head.slice(5).trim();
  const refPath = path.join(gitDir, ref);
  if (existsSync(refPath)) return fs.readFileSync(refPath, "utf8").trim();
  const packed = path.join(gitDir, "packed-refs");
  if (existsSync(packed)) {
    for (const line of fs.readFileSync(packed, "utf8").split("\n")) {
      if (!line || line.startsWith("#") || line.startsWith("^")) continue;
      const [sha, name] = line.trim().split(" ");
      if (name === ref) return sha;
    }
  }
  throw new Error(`unresolved git ref ${ref}`);
}

function extractGitTree(gitDir, sha, destination) {
  const tree = readGitObject(gitDir, sha);
  if (tree.type !== "tree") throw new Error("expected git tree object");
  let offset = 0;
  while (offset < tree.body.length) {
    const space = tree.body.indexOf(0x20, offset);
    const nul = tree.body.indexOf(0, space + 1);
    const mode = tree.body.subarray(offset, space).toString("utf8");
    const name = tree.body.subarray(space + 1, nul).toString("utf8");
    const child = tree.body.subarray(nul + 1, nul + 21).toString("hex");
    offset = nul + 21;
    const target = path.join(destination, name);
    if (mode === "40000" || mode === "040000") {
      fs.mkdirSync(target, { recursive: true });
      extractGitTree(gitDir, child, target);
    } else if (mode === "160000") {
      continue;
    } else {
      const blob = readGitObject(gitDir, child);
      if (blob.type !== "blob") throw new Error("expected git blob object");
      if (mode === "120000") {
        fs.symlinkSync(blob.body.toString("utf8"), target);
      } else {
        fs.writeFileSync(target, blob.body, { mode: mode === "100755" ? 0o755 : 0o644 });
      }
    }
  }
}

function extractHeadWithoutGit(repoRoot) {
  const gitDir = path.join(repoRoot, ".git");
  const head = resolveGitHead(gitDir);
  const commit = readGitObject(gitDir, head);
  if (commit.type !== "commit") throw new Error("expected git commit object");
  const tree = commit.body
    .toString("utf8")
    .split("\n")
    .find((line) => line.startsWith("tree "));
  if (!tree) throw new Error("git commit has no tree");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "migration-baseline-"));
  const destination = path.join(root, "repo");
  fs.mkdirSync(destination, { recursive: true });
  extractGitTree(gitDir, tree.slice(5).trim(), destination);
  return destination;
}

async function prepareBaseline(repoRoot) {
  if (!existsSync(path.join(repoRoot, ".git"))) return null;
  const root = await makeTempDir("migration-baseline-");
  const tarPath = path.join(root, "baseline.tar");
  const archive = await runProcess("git", ["-C", repoRoot, "archive", "--format=tar", "HEAD"], {
    timeout: 300000
  });
  if (archive.code !== 0 || !archive.stdoutBuffer.length) {
    await removeQuietly(root);
    try {
      return extractHeadWithoutGit(repoRoot);
    } catch {
      return null;
    }
  }
  await fsp.writeFile(tarPath, archive.stdoutBuffer);
  const extract = path.join(root, "repo");
  await fsp.mkdir(extract, { recursive: true });
  const untar = await runProcess("tar", ["-xf", tarPath, "-C", extract], {
    timeout: 300000
  });
  await fsp.rm(tarPath, { force: true });
  if (untar.code !== 0) {
    await removeQuietly(root);
    try {
      return extractHeadWithoutGit(repoRoot);
    } catch {
      return null;
    }
  }
  return extract;
}

async function createP2PStage(repoRoot, assets, side, batches) {
  const root = await fsp.mkdtemp(path.join(repoRoot, ".migration-p2p-"));
  const config = path.join(root, "vitest.config.mjs");
  await fsp.writeFile(
    config,
    [
      "export default {",
      `  root: ${JSON.stringify(root)},`,
      "  resolve: { preserveSymlinks: true },",
      "  test: {",
      '    environment: "node",',
      "    watch: false,",
      '    include: ["batches/**/*.test.{js,mjs,cjs,ts,mts,cts,jsx,tsx}"]',
      "  },",
      `  server: { fs: { allow: [${JSON.stringify(root)}, ${JSON.stringify(repoRoot)}, ${JSON.stringify(assets)}] } }`,
      "};",
      ""
    ].join("\n")
  );
  for (const batch of batches) {
    const source = path.join(assets, side, "batches", batch);
    const destination = path.join(root, "batches", batch);
    await fsp.mkdir(path.dirname(destination), { recursive: true });
    await fsp.symlink(source, destination, "dir");
  }
  return { root, config };
}

async function createRunStage(repoRoot, assets, side, batches) {
  const root = await makeTempDir("migration-run-");
  await fsp.symlink(path.join(assets, "shared"), path.join(root, "shared"), "dir");
  await fsp.symlink(path.join(assets, "fixtures"), path.join(root, "fixtures"), "dir");
  for (const batch of batches) {
    const destination = path.join(root, side, "batches", batch);
    await fsp.mkdir(path.dirname(destination), { recursive: true });
    await fsp.symlink(path.join(assets, side, "batches", batch), destination, "dir");
  }
  return root;
}

async function createRepoView(repoRoot, side) {
  const root = await makeTempDir("migration-repo-view-");
  const entries = await fsp.readdir(repoRoot, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.name === "node_modules" || entry.name === ".git") continue;
    await fsp.symlink(
      path.join(repoRoot, entry.name),
      path.join(root, entry.name),
      entry.isDirectory() ? "dir" : "file"
    );
  }
  const repoModules = path.join(repoRoot, "node_modules");
  const dependencyRoot = resolveDependencyRoot(repoRoot);
  let modules = existsSync(repoModules) ? repoModules : dependencyRoot;
  if (
    side === "target" &&
    readPackageVersion(path.join(dependencyRoot, "elysia")) === "1.3.0" &&
    readPackageVersion(path.join(repoModules, "elysia")) !== "1.3.0"
  ) {
    modules = dependencyRoot;
  }
  if (existsSync(modules)) {
    await fsp.symlink(modules, path.join(root, "node_modules"), "dir");
  }
  return root;
}

async function runVitest(repoRoot, args, options = {}) {
  const runner = resolveVitest(repoRoot);
  if (!runner) return { rows: [], spawned: false, result: null };
  const output = await makeReportPath();
  const fullArgs = runner.prefixArgs.concat(
    args,
    ["--reporter=json", `--outputFile=${output}`]
  );
  const result = await runProcess(runner.command, fullArgs, {
    cwd: options.cwd || repoRoot,
    timeout: options.timeout ?? 900000,
    env: {
      ...process.env,
      ...(options.env || {}),
      REPO_ROOT: repoRoot,
      MIGRATION_REPO_ROOT: repoRoot
    }
  });
  const report = await readJsonOrNull(output);
  await removeQuietly(path.dirname(output));
  return {
    rows: report ? flattenVitest(report) : [],
    spawned: !result.spawnError,
    result,
    report
  };
}

function exportedGenericTypeNames(repoRoot, dependencyRoot) {
  const ts = createRequire(path.join(dependencyRoot, "noop.js"))("typescript");
  const entry = path.join(repoRoot, "packages/hono-party/src/index.ts");
  const program = ts.createProgram([entry], {
    target: ts.ScriptTarget.ESNext,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    skipLibCheck: true,
    noEmit: true
  });
  const checker = program.getTypeChecker();
  const source = program.getSourceFile(entry);
  const module = source && checker.getSymbolAtLocation(source);
  if (!module) return [];
  return checker.getExportsOfModule(module).filter((symbol) => {
    const target = symbol.flags & ts.SymbolFlags.Alias
      ? checker.getAliasedSymbol(symbol) : symbol;
    return symbol.name !== "default" && target.declarations?.some((declaration) =>
      (ts.isTypeAliasDeclaration(declaration) || ts.isInterfaceDeclaration(declaration)) &&
      declaration.typeParameters?.length > 0
    );
  }).map((symbol) => symbol.name);
}

async function runP2PBatch0001(context) {
  const { repoRoot, assets, results, ids } = context;
  const batch = "BATCH-P2P-0001";
  const projections = loadP2PProjections(assets, batch);
  const stage = await createP2PStage(repoRoot, assets, context.side, [batch]);
  try {
    const adapter = path.join(stage.root, "batches", batch, "adapter.p2p.test.ts");
    const exportsTest = path.join(stage.root, "batches", batch, "exports.p2p.test.ts");
    const vitest = await runVitest(
      repoRoot,
      ["run", "--config", stage.config, adapter, exportsTest],
      { cwd: repoRoot }
    );
    applyRows(results, ids, vitest.rows);

    // Batch paths are symlinks into immutable evaluator assets. Generate the
    // per-export variant in the writable stage, never through those links.
    const typecheckTemplate = path.join(stage.root, "batches", batch, "generics.typecheck.ts");
    const typecheckFile = path.join(stage.root, "generics.typecheck.ts");
    const dependencyRoot = resolveDependencyRoot(repoRoot);
    const tsc = path.join(dependencyRoot, ".bin", "tsc");
    if (existsSync(tsc)) {
      // The instruction preserves an exported options type and its generic
      // contract, but only fixes the runtime export's name. Compile the same
      // positive and negative assertions against each exported generic type;
      // an equivalent renamed options type must also be accepted.
      const template = fs.readFileSync(typecheckTemplate, "utf8");
      const names = exportedGenericTypeNames(repoRoot, dependencyRoot).sort((a, b) =>
        Number(b === "HonoPartyServerOptions") - Number(a === "HonoPartyServerOptions")
      );
      // The public contract permits typing bindings directly or typing the
      // complete callback context, as well as the former Bindings envelope.
      // Compile the same positive and negative assertions for every shape.
      const variants = [template, template
        .replace("type Bindings = { Bindings: { JWT_SECRET: string } };",
          "type Bindings = { JWT_SECRET: string };")
        .replace("<{ Bindings: { OTHER: string } }>", "<{ OTHER: string }>"),
        'import type { Context } from "elysia";\n' + template
          .replace("type Bindings = { Bindings: { JWT_SECRET: string } };",
            "type Bindings = Context & { env: { JWT_SECRET: string } };")
          .replace("<{ Bindings: { OTHER: string } }>",
            "<Context & { env: { OTHER: string } }>")];
      let check = { code: 1, spawnError: null, timedOut: false };
      typeVariants: for (const name of names) {
        const imported = name === "HonoPartyServerOptions"
          ? name : `${name} as HonoPartyServerOptions`;
        for (const variant of variants) {
          fs.writeFileSync(typecheckFile, variant.replace(
            "type HonoPartyServerOptions", `type ${imported}`
          ));
          check = await runProcess(
            tsc,
            [
              "--noEmit",
              "--strict",
              "--skipLibCheck",
              "--target",
              "ESNext",
              "--module",
              "ESNext",
              "--moduleResolution",
              "Bundler",
              "--allowImportingTsExtensions",
              "--lib",
              "ESNext,DOM",
              "--types",
              "@cloudflare/workers-types",
              typecheckFile
            ],
            { cwd: repoRoot, timeout: 300000 }
          );
          if (check.code === 0 || check.spawnError || check.timedOut) break typeVariants;
        }
      }
      if (check.spawnError || check.timedOut) {
        record(results, "P2P-022", "INCONCLUSIVE", { result: "INCONCLUSIVE" });
      } else {
        if (check.code === 0) recordPassFail(results, "P2P-022", true);
        else record(results, "P2P-022", "FAIL", {
          result: "FAIL", diagnostic: truncate(check.stderr || check.stdout)
        });
      }
    } else {
      record(results, "P2P-022", "INCONCLUSIVE", { result: "INCONCLUSIVE" });
    }
  } finally {
    await removeQuietly(stage.root);
  }

  fillMissing(results, ids);
  applyProjections(results, ids, projections);
}

async function runP2PBatch0002(context) {
  const { repoRoot, assets, results, ids } = context;
  const batch = "BATCH-P2P-0002";
  const projections = loadP2PProjections(assets, batch);
  const stage = await createP2PStage(repoRoot, assets, context.side, [batch]);
  try {
    const testFile = path.join(stage.root, "batches", batch, "p2p.test.mjs");
    const vitest = await runVitest(repoRoot, ["run", "--config", stage.config, testFile], {
      cwd: repoRoot
    });
    applyRows(results, ids, vitest.rows);
    Object.assign(projections, extractProjectionLogs(vitest.result?.stdout));
  } finally {
    await removeQuietly(stage.root);
  }

  fillMissing(results, ids);
  applyProjections(results, ids, projections);
}

function applyReport(results, ids, report) {
  const list = report?.results || report?.observations || [];
  for (const item of list) {
    const id = item.observation_id || item.id;
    if (!ids.includes(id)) continue;
    if (item.actual !== undefined) {
      record(results, id, item.status === "PASS" ? "PASS" : "FAIL", item.actual);
    } else {
      recordPassFail(results, id, item.status === "PASS");
    }
  }
}

function fillFromFailure(results, ids, outcome) {
  const status = outcome?.spawnError || outcome?.timedOut ? "INCONCLUSIVE" : "FAIL";
  for (const id of ids) {
    if (!results.has(id)) record(results, id, status, { result: status });
  }
}

async function runF2PBatch0001(context) {
  const { repoRoot, assets, results, ids, baseline, side } = context;
  const batch = "BATCH-F2P-0001";
  const probe = path.join(assets, side, "batches", batch, "probe.mjs");
  const fixtures = path.join(assets, "fixtures", "batches", batch);
  const entry = resolveAdapterEntry(repoRoot);
  const args = [];
  if (entry.endsWith(".ts")) args.push("--import", resolveTsxImport(repoRoot));
  args.push("--import", path.join(fixtures, "register.mjs"), probe);
  const elysia = resolveElysiaEntry(repoRoot);
  const run = await runProcess(process.execPath, args, {
    cwd: repoRoot,
    timeout: 900000,
    env: {
      ...process.env,
      REPO_ROOT: repoRoot,
      SIDE: side,
      PACKAGE_ENTRY: entry,
      BASELINE_ROOT: side === "target" ? baseline || repoRoot : repoRoot,
      MODULE_LOADER_URL: path.join(assets, "shared", "integration", "f2p-loader.mjs"),
      PARTYSERVER_MOCK_URL: path.join(fixtures, "partyserver-mock.mjs"),
      CLOUDFLARE_STUB_URL: path.join(fixtures, "cloudflare-workers-stub.mjs"),
      ...(elysia ? { ELYSIA_ENTRY: elysia } : {})
    }
  });
  const report = extractJsonObject(run.stdout);
  if (report) applyReport(results, ids, report);
  else fillFromFailure(results, ids, run);
}

async function runF2PBatch0002(context) {
  const { repoRoot, assets, results, ids, side } = context;
  const batch = "BATCH-F2P-0002";
  const stage = await createRunStage(repoRoot, assets, side, [batch]);
  const reportPath = await makeReportPath();
  const repoView = await createRepoView(repoRoot, side);
  try {
    const script = path.join(stage, side, "batches", batch, "run.mjs");
    const run = await runProcess(
      process.execPath,
      NODE_FLAGS.concat([script, reportPath]),
      {
        cwd: repoView,
        timeout: 900000,
        env: {
          ...process.env,
          REPO_ROOT: repoView,
          MIGRATION_REPO_ROOT: repoView
        }
      }
    );
    const report = (await readJsonOrNull(reportPath)) || extractJsonObject(run.stdout);
    if (report) applyReport(results, ids, report);
    else fillFromFailure(results, ids, run);
  } finally {
    await removeQuietly(stage);
    await removeQuietly(repoView);
    await removeQuietly(path.dirname(reportPath));
  }
}

async function runF2PBatch0003(context) {
  const { repoRoot, assets, results, ids, side } = context;
  const batch = "BATCH-F2P-0003";
  const stage = await createRunStage(repoRoot, assets, side, [batch]);
  const reportPath = await makeReportPath();
  const dependencyRoot = resolveDependencyRoot(repoRoot);
  try {
    const script = path.join(stage, side, "batches", batch, "probe.mjs");
    const run = await runProcess(
      process.execPath,
      NODE_FLAGS.concat([script, "--repo", repoRoot, "--report", reportPath]),
      {
        cwd: repoRoot,
        timeout: 1200000,
        env: {
          ...process.env,
          REPO_ROOT: repoRoot,
          F2P_DEPENDENCIES_NODE_MODULES: dependencyRoot
        }
      }
    );
    const report = (await readJsonOrNull(reportPath)) || extractJsonObject(run.stdout);
    if (report) applyReport(results, ids, report);
    else fillFromFailure(results, ids, run);
  } finally {
    await removeQuietly(stage);
    await removeQuietly(path.dirname(reportPath));
  }
}

async function runDifferential(context) {
  const { repoRoot, assets, results, ids, side } = context;
  const batch = "BATCH-DIFFERENTIAL-0001";
  const reportPath = await makeReportPath();
  try {
    const script = path.join(assets, side, "batches", batch, "run.mjs");
    const entry = resolveAdapterEntry(repoRoot);
    const args = [];
    if (entry.endsWith(".ts")) args.push("--import", resolveTsxImport(repoRoot));
    args.push("--import", path.join(assets, "shared", "integration", "register.mjs"), script, reportPath);
    const run = await runProcess(process.execPath, args, {
      cwd: repoRoot,
      timeout: 900000,
      env: {
        ...process.env,
        REPO_ROOT: repoRoot,
        HONO_PARTY_ENTRY: entry,
        // Select the package's ESM entry, preserving its default export.
        ...(resolveElysiaEntry(repoRoot) ? { ELYSIA_ENTRY: resolveElysiaEntry(repoRoot) } : {}),
        DIFFERENTIAL_ROUTE_MOCK_URL: path.join(
          assets,
          "fixtures",
          "batches",
          batch,
          "route-mock.mjs"
        ),
        DIFFERENTIAL_CLOUDFLARE_STUB_URL: path.join(
          assets,
          "fixtures",
          "batches",
          "BATCH-F2P-0001",
          "cloudflare-workers-stub.mjs"
        )
      }
    });
    const report = (await readJsonOrNull(reportPath)) || extractJsonObject(run.stdout);
    if (report) applyReport(results, ids, report);
    else fillFromFailure(results, ids, run);
  } finally {
    await removeQuietly(path.dirname(reportPath));
  }
}

function batchIds(inventory, batchId) {
  return inventory.filter((item) => item.batch_id === batchId).map((item) => item.id);
}

export async function run({ side, repoRoot, reportPath } = {}) {
  const assets = assetRoot();
  const repo = resolveRepoRoot(repoRoot);
  const inventory = readJson(path.join(assets, "shared", "integration", "observations.json"));
  const results = new Map();
  const baseline = side === "target" ? await prepareBaseline(repo) : null;

  await ensureBuild(repo);

  const common = { side, repoRoot: repo, assets, results, baseline };
  const runners = [
    [runP2PBatch0001, "BATCH-P2P-0001"],
    [runP2PBatch0002, "BATCH-P2P-0002"],
    [runF2PBatch0001, "BATCH-F2P-0001"],
    [runF2PBatch0002, "BATCH-F2P-0002"],
    [runF2PBatch0003, "BATCH-F2P-0003"],
    [runDifferential, "BATCH-DIFFERENTIAL-0001"]
  ];

  for (const [runner, batch] of runners) {
    const ids = batchIds(inventory, batch);
    try {
      await runner({ ...common, ids });
    } catch {
      fillMissing(results, ids);
    }
  }

  const observations = inventory.map((item) => {
    const result = results.get(item.id);
    const status = result?.status || "INCONCLUSIVE";
    const value = result?.value || { result: status };
    return {
      id: item.id,
      kind: item.kind,
      status,
      value: { type: "json", value }
    };
  });

  const report = {
    schema_version: REPORT_SCHEMA,
    observations
  };

  const serialized = `${JSON.stringify(report, null, 2)}\n`;
  if (reportPath) {
    await fsp.mkdir(path.dirname(path.resolve(reportPath)), { recursive: true });
    await fsp.writeFile(reportPath, serialized);
  }
  process.stdout.write(serialized);
  if (baseline) await removeQuietly(path.dirname(baseline));
  return report;
}
