import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { cli, observationIds, runObservations } from "../../../shared/batches/BATCH-F2P-0001/checks.mjs";

export { observationIds };

export async function probeTarget(root = process.cwd()) {
	return runObservations(root);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	cli(process.argv[2] ?? process.cwd())
		.then(({ infra }) => {
			process.exitCode = infra.length > 0 ? 1 : 0;
		})
		.catch((error) => {
			console.error(error);
			process.exitCode = 1;
		});
}
