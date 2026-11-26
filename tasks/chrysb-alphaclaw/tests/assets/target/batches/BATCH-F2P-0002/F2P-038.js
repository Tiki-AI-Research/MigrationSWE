#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

const SIDE = "target";

const findProbeLib = (startDirectory) => {
  let directory = startDirectory;
  while (true) {
    const candidate = path.join(
      directory,
      "shared",
      "batches",
      "BATCH-F2P-0002",
      "probe-lib.js",
    );
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(directory);
    if (parent === directory) return null;
    directory = parent;
  }
};

const probeLibPath = findProbeLib(__dirname);
if (!probeLibPath) {
  console.log(
    JSON.stringify(
      {
        observation_id: "F2P-038",
        side: SIDE,
        status: "INCONCLUSIVE",
        reason: "shared probe-lib.js could not be located",
      },
      null,
      2,
    ),
  );
  process.exit(2);
}

const { runObservation } = require(probeLibPath);

(async () => {
  try {
    const result = await runObservation("F2P-038", {
      side: SIDE,
      repoRoot:
        process.env.ALPHACLAW_REPO_ROOT || process.argv[2] || process.cwd(),
    });
    console.log(JSON.stringify({ ...result, observation_id: "F2P-038", side: SIDE }, null, 2));
    process.exit(result.status === "PASS" ? 0 : result.status === "FAIL" ? 1 : 2);
  } catch (error) {
    console.log(
      JSON.stringify(
        {
          observation_id: "F2P-038",
          side: SIDE,
          status: "INCONCLUSIVE",
          reason: error.message,
          stack: error.stack,
        },
        null,
        2,
      ),
    );
    process.exit(2);
  }
})();
