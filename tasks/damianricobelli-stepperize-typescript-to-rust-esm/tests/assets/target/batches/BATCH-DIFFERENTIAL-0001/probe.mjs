import assert from "node:assert/strict";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
	collectRuntimeObservations,
	collectStructuralObservations,
} from "../../../shared/batches/BATCH-DIFFERENTIAL-0001/observations.mjs";
import { importRustCore, rustRuntime } from "../../../shared/native-layout.mjs";

export async function probeTarget(root = process.cwd()) {
	// The public contract permits both Node-API and synchronous WebAssembly.
	// Prove compiled execution during these observations, independent of suffix.
	const binding = rustRuntime(root);
	binding.reinstall();
	const core = await importRustCore(root);
	const runtime = await collectRuntimeObservations(core, root);
	assert.ok(binding.nativeCodeCount > 0 && binding.total() > 0,
		"target package must load and execute compiled Rust");
	const structural = await collectStructuralObservations(root, "target");
	return { ...runtime, ...structural };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	probeTarget(process.argv[2] ?? process.cwd())
		.then((observations) => {
			process.stdout.write(`${JSON.stringify(observations)}\n`);
		})
		.catch((error) => {
			console.error(error);
			process.exitCode = 1;
		});
}
