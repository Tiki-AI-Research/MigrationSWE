import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
	observationIds,
	reportResults,
	runChecks,
} from "../../../shared/batches/BATCH-F2P-0002/checks.mjs";

export { observationIds };

export async function probeTarget(root = process.cwd()) {
	return reportResults(await runChecks(root));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	probeTarget(process.argv[2] ?? process.cwd())
		.then((results) => {
			console.log(JSON.stringify(results, null, 2));
			const unexpected = Object.values(results).filter((result) => result.status !== "PASS");
			if (unexpected.length > 0) process.exitCode = 1;
		})
		.catch((error) => {
			console.error(error);
			process.exitCode = 1;
		});
}
