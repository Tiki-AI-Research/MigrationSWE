#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const readArg = (name, fallback) => {
  const index = args.indexOf(name);
  if (index === -1) return fallback;
  return args[index + 1];
};

const side = readArg("--side", "unknown");
const repoRoot = path.resolve(readArg("--repo", process.cwd()));
const outPath = readArg("--out", "");
const manifest = JSON.parse(
  fs.readFileSync(path.join(here, "observations.json"), "utf8"),
);

const probeSource = path.join(here, "p2p-probes.test.js");
const probeTarget = path.join(repoRoot, manifest.probe_test_file);
const probeDirectory = path.dirname(probeTarget);
fs.mkdirSync(probeDirectory, { recursive: true });
const previousProbe = fs.existsSync(probeTarget)
  ? fs.readFileSync(probeTarget)
  : null;
fs.copyFileSync(probeSource, probeTarget);

const testFiles = [
  ...new Set(
    manifest.observations.flatMap((observation) =>
      observation.tests.map((test) => test.file),
    ),
  ),
];

const reportFile = path.join(
  os.tmpdir(),
  `alphaclaw-p2p-${side}-${process.pid}.json`,
);
const vitestPath = path.join(repoRoot, "node_modules", "vitest", "vitest.mjs");
let rawReport = null;
try {
  const result = spawnSync(
    process.execPath,
    [
      vitestPath,
      "run",
      "--reporter=json",
      `--outputFile=${reportFile}`,
      ...testFiles,
    ],
    {
      cwd: repoRoot,
      env: { ...process.env, P2P_REPO_ROOT: repoRoot },
      stdio: "inherit",
    },
  );
  if (result.error) throw result.error;
  if (fs.existsSync(reportFile)) {
    rawReport = JSON.parse(fs.readFileSync(reportFile, "utf8"));
  }
} finally {
  if (previousProbe === null) {
    fs.rmSync(probeTarget, { force: true });
  } else {
    fs.writeFileSync(probeTarget, previousProbe);
  }
}

const results = new Map();
const fileErrors = new Map();
for (const fileResult of rawReport?.testResults || []) {
  const relativeFile = path.relative(repoRoot, fileResult.name);
  if (fileResult.message) fileErrors.set(relativeFile, String(fileResult.message));
  for (const assertion of fileResult.assertionResults || []) {
    const title = assertion.title || "";
    const fullName = assertion.fullName || title;
    const key = `${relativeFile}#${title}`;
    results.set(key, {
      status: assertion.status === "passed" ? "PASS" : "FAIL",
      fullName,
      failureMessages: (assertion.failureMessages || []).map(String),
    });
  }
}

const observations = manifest.observations.map((observation) => {
  const tests = observation.tests.map((test) => {
    const key = `${test.file}#${test.name}`;
    const match = results.get(key);
    return {
      file: test.file,
      name: test.name,
      status: match?.status || "MISSING",
      failureMessages: match?.failureMessages || [
        fileErrors.get(test.file) || "test result missing from Vitest report",
      ],
    };
  });
  const status = tests.every((test) => test.status === "PASS") ? "PASS" : "FAIL";
  return {
    observation_id: observation.id,
    status,
    value: { result: status },
    tests,
  };
});

const report = {
  schema_version: "migration-p2p-side-report:v1",
  batch_id: manifest.batch_id,
  side,
  repo_root: repoRoot,
  projection: manifest.projection,
  observations,
  summary: {
    total: observations.length,
    passed: observations.filter((item) => item.status === "PASS").length,
    failed: observations.filter((item) => item.status === "FAIL").length,
  },
};

const serialized = `${JSON.stringify(report, null, 2)}\n`;
if (outPath) {
  fs.writeFileSync(outPath, serialized);
} else {
  process.stdout.write(serialized);
}
process.exitCode = report.summary.failed === 0 ? 0 : 1;
