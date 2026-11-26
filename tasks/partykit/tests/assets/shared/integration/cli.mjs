import { run } from "./integration.mjs";

function parseArgs(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--repo") {
      options.repoRoot = argv[index + 1];
      index += 1;
    } else if (arg === "--report") {
      options.reportPath = argv[index + 1];
      index += 1;
    } else if (!arg.startsWith("-") && !options.reportPath) {
      options.reportPath = arg;
    }
  }
  return options;
}

export async function runCli(side) {
  const options = parseArgs(process.argv.slice(2));
  await run({
    side,
    repoRoot: options.repoRoot,
    reportPath: options.reportPath
  });
}
