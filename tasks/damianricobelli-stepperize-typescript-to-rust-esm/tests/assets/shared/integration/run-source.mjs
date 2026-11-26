import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { writeReport } from "./runner-lib.mjs";

const assetsRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const reportPath = process.argv[2];

writeReport({ side: "source", assetsRoot, reportPath }).catch((error) => {
	console.error(error);
	process.exitCode = 1;
});
