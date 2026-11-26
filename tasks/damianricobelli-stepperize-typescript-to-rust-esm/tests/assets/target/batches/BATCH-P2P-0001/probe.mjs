import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { runP2PChecks } from "../../../shared/batches/BATCH-P2P-0001/checks.mjs";

export async function probeTarget(root = process.cwd()) {
	return runP2PChecks(root);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	probeTarget(process.argv[2] ?? process.cwd())
		.then((result) => {
			process.stdout.write(`${JSON.stringify(result)}\n`);
		})
		.catch((error) => {
			console.error(error);
			process.exitCode = 1;
		});
}
