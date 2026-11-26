#!/usr/bin/env node
// Shared P2P executor for BATCH-P2P-0001.
//
// Runs the repository's own native test suites on one side (source or target),
// maps every P2P observation of this batch to the concrete tests that exercise
// it, and emits the raw per-side PASS/FAIL together with the normalised
// status-wrapper projection used by the P2P gate.
//
// Usage: node runner.mjs <repoRoot> <reportPath> <side>
// Any argument may be omitted; the report path is detected by its ".json"
// suffix and the side by the literal "source" / "target".
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const manifest = JSON.parse(readFileSync(join(here, "observations.json"), "utf8"));

function looksLikeRepo(root) {
  if (!root) return false;
  try {
    return (
      existsSync(join(root, "pnpm-workspace.yaml")) &&
      existsSync(join(root, "javascript-app", "package.json"))
    );
  } catch {
    return false;
  }
}

function pickRepoRoot(explicit) {
  const candidates = [
    explicit,
    process.env.REPO_ROOT,
    process.env.P2P_REPO_ROOT,
    "/workspace/repo",
    "/input/source",
    "/input/target",
  ];
  for (const candidate of candidates) {
    if (looksLikeRepo(candidate)) return resolve(candidate);
  }
  return resolve(explicit ?? process.env.REPO_ROOT ?? process.cwd());
}

const argv = process.argv.slice(2);
let repoRootArg = null;
let reportPathArg = null;
let sideArg = null;
for (const arg of argv) {
  if (arg.endsWith(".json")) reportPathArg = arg;
  else if (arg === "source" || arg === "target") sideArg = arg;
  else repoRootArg = arg;
}

const repoRoot = pickRepoRoot(repoRootArg);
const reportPath = reportPathArg ?? process.env.P2P_REPORT ?? "/logs/result.json";
const side = sideArg ?? process.env.P2P_SIDE ?? "source";

// Extracts the first balanced JSON object from a stream that may contain
// progress logging around the reporter payload.
function extractJson(text) {
  const start = text.indexOf("{");
  if (start === -1) return null;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') inString = true;
    else if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        try {
          return JSON.parse(text.slice(start, i + 1));
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

function normaliseFile(name) {
  let file = name ?? "";
  file = file.replace(`${repoRoot}/`, "").replace(/^\.\//, "");
  return file;
}

function parseVitest(parsed) {
  const tests = new Map();
  if (!parsed) return tests;
  for (const fileResult of parsed.testResults ?? []) {
    const file = normaliseFile(fileResult.name ?? fileResult.file ?? "");
    for (const assertion of fileResult.assertionResults ?? []) {
      const title = assertion.title ?? (assertion.fullName ?? "").split(" > ").pop();
      tests.set(`${file}::${title}`, assertion.status === "passed" ? "PASS" : "FAIL");
    }
  }
  return tests;
}

// Minimal JUnit XML reader: returns [{ name, status }] for every <testcase>.
function parseJUnit(xml) {
  const cases = [];
  if (!xml) return cases;
  const blocks = xml.split(/<testcase\b/);
  blocks.shift();
  for (const raw of blocks) {
    const header = raw.slice(0, raw.indexOf(">"));
    const nameMatch = /\bname="([^"]*)"/.exec(header);
    if (!nameMatch) continue;
    let body = raw;
    const close = raw.indexOf("</testcase>");
    if (close !== -1) body = raw.slice(0, close);
    let status = "PASS";
    if (/<failure\b/.test(body)) status = "FAIL";
    else if (/<error\b/.test(body)) status = "FAIL";
    else if (/<skipped\b/.test(body)) status = "FAIL";
    cases.push({ name: decodeXml(nameMatch[1]), status });
  }
  return cases;
}

function decodeXml(value) {
  return value
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function runCommand(spec, extraArg) {
  const spawn = (base) => {
    const args = extraArg ? [...base, extraArg] : [...base];
    return spawnSync(args[0], args.slice(1), {
      cwd: join(repoRoot, spec.cwd ?? "."),
      encoding: "utf8",
      env: { ...process.env, CI: "1" },
      maxBuffer: 256 * 1024 * 1024,
    });
  };
  const result = spawn(spec.command);
  if (result.error && result.error.code === "ENOENT" && Array.isArray(spec.fallback)) {
    return spawn(spec.fallback);
  }
  return result;
}

function runSuite(spec) {
  const workDir = mkdtempSync(join(tmpdir(), "p2p-report-"));
  const outputFile = join(workDir, "report.out");
  let result;
  let tests = new Map();
  let pytestCases = [];
  try {
    if (spec.reporter === "vitest-json") {
      result = runCommand(spec, `--outputFile=${outputFile}`);
      let parsed = null;
      if (existsSync(outputFile)) {
        try {
          parsed = JSON.parse(readFileSync(outputFile, "utf8"));
        } catch {
          parsed = null;
        }
      }
      if (!parsed) parsed = extractJson(result.stdout ?? "");
      tests = parseVitest(parsed);
    } else if (spec.reporter === "pytest-junit") {
      result = runCommand(spec, `--junitxml=${outputFile}`);
      if (existsSync(outputFile)) {
        pytestCases = parseJUnit(readFileSync(outputFile, "utf8"));
      }
    } else {
      result = runCommand(spec, null);
    }
  } finally {
    try {
      rmSync(workDir, { force: true, recursive: true });
    } catch {
      /* ignore cleanup failures */
    }
  }
  const exitCode = result?.status ?? -1;
  const stderrTail = (result?.stderr ?? "").split("\n").slice(-5).join("\n");
  return { exitCode, stderrTail, tests, pytestCases };
}

const suiteResults = {};
for (const [name, spec] of Object.entries(manifest.suites)) {
  suiteResults[name] = runSuite(spec);
}

function locate(check) {
  const suite = suiteResults[check.suite];
  if (!suite) return "FAIL";
  if (check.scope === "suite") {
    if (suite.tests.size > 0) {
      return [...suite.tests.values()].every((status) => status === "PASS") ? "PASS" : "FAIL";
    }
    if (suite.pytestCases.length > 0) {
      return suite.pytestCases.every((entry) => entry.status === "PASS") ? "PASS" : "FAIL";
    }
    return suite.exitCode === 0 ? "PASS" : "FAIL";
  }
  if (check.name) {
    const matched = suite.pytestCases.filter((entry) => entry.name.includes(check.name));
    if (matched.length === 0) return "FAIL";
    return matched.every((entry) => entry.status === "PASS") ? "PASS" : "FAIL";
  }
  const suffix = `/${check.file}::${check.title}`;
  for (const [key, status] of suite.tests) {
    if (`/${key}`.endsWith(suffix)) return status;
  }
  return "FAIL";
}

const observations = {};
const raw = {};
const suites = {};
for (const [name, result] of Object.entries(suiteResults)) {
  suites[name] = {
    exitCode: result.exitCode,
    located: result.tests.size || result.pytestCases.length,
    stderrTail: result.stderrTail,
  };
}
for (const observation of manifest.observations) {
  const statuses = observation.checks.map((check) => ({ check, status: locate(check) }));
  raw[observation.id] = statuses;
  observations[observation.id] = {
    result: statuses.every((entry) => entry.status === "PASS") ? "PASS" : "FAIL",
  };
}

const payload = { batch_id: manifest.batch_id, observations, raw, side, suites };
writeFileSync(reportPath, `${JSON.stringify(payload, null, 2)}\n`);
process.stdout.write(`${JSON.stringify(payload)}\n`);
process.exit(Object.values(observations).every((entry) => entry.result === "PASS") ? 0 : 1);
