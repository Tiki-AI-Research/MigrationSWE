import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
	OBSERVATION_IDS,
	projections,
	runObservation,
	runProjections,
} from "../../../shared/batches/BATCH-P2P-0002/runner.mjs";

function defaultRoot() {
	const fromFile = fileURLToPath(new URL("../../../", import.meta.url));
	return existsSync(resolve(fromFile, "packages/core")) ? fromFile : process.cwd();
}

export async function probeTarget(root = defaultRoot()) {
	return runProjections(root);
}

export async function probe(root = defaultRoot()) {
	return runProjections(root);
}

export { OBSERVATION_IDS, projections, runObservation, runProjections };

export default probeTarget;

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	probeTarget(process.argv[2] ?? defaultRoot())
		.then((result) => {
			process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
		})
		.catch((error) => {
			console.error(error);
			process.exitCode = 1;
		});
}
