import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createEnv } from "../../../shared/batches/BATCH-F2P-0003/harness.mjs";
import { observations, runAll } from "../../../shared/batches/BATCH-F2P-0003/observations.mjs";

export const observationIds = Object.freeze(Object.keys(observations));
export const expectedStatus = "PASS";

export async function probeTarget(root = process.cwd()) {
	const env = await createEnv(root, { requireNative: true });
	try {
		return await runAll(env);
	} finally {
		env.restore();
	}
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	probeTarget(process.argv[2] ?? process.cwd())
		.then((results) => {
			process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);
			const unexpected = results.filter((result) => result.status !== expectedStatus);
			process.exitCode = unexpected.length === 0 ? 0 : 1;
		})
		.catch((error) => {
			console.error(error);
			process.exitCode = 1;
		});
}
