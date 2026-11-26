import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { writeReport } from "./shared/integration/runner-lib.mjs";

const assetsRoot = dirname(fileURLToPath(import.meta.url));
const reportPath = process.argv[2];

writeReport({ side: "target", assetsRoot, reportPath }).catch((error) => {
	console.error(error);
	process.exitCode = 1;
});
