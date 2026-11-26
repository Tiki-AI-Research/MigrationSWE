#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
const { run } = require(path.resolve(
  __dirname,
  "../../../shared/batches/BATCH-DIFFERENTIAL-0002/probe-core.js",
));

const authoringRepo = "/input/target";
const isRepoRoot = (candidate) =>
  Boolean(candidate) &&
  fs.existsSync(path.join(candidate, "package.json")) &&
  fs.existsSync(path.join(candidate, "lib", "server.js"));

const args = process.argv.slice(2);
const repoArg = args.find((entry) => isRepoRoot(entry));
const reportArg = args.find(
  (entry) => !isRepoRoot(entry) && /\.json$/i.test(entry),
);
const repoRoot =
  process.env.REPO_ROOT ||
  repoArg ||
  (isRepoRoot(authoringRepo) ? authoringRepo : "/workspace/repo");
const reportPath =
  process.env.REPORT_PATH || reportArg || process.env.REPORT || null;

run({ repoRoot, expectedRuntime: "hono", reportPath }).catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
