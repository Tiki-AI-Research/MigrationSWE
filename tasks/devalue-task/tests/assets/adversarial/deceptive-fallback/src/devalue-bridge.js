// Adversarial fixture: bridges to the old implementation through a subprocess,
// a module loader escape hatch and a worker RPC channel.
import { execFileSync, spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { Worker } from 'node:worker_threads';

const require = createRequire(import.meta.url);

export function legacy_runner() {
	return require('node_modules/devalue/dist/index.cjs');
}

export function legacy_cli(payload) {
	return execFileSync('node', ['legacy-devalue-cli.js', payload]);
}

export function legacy_spawn(payload) {
	return spawnSync('node', ['legacy-devalue-cli.js', payload]);
}

export function legacy_worker() {
	return new Worker(new URL('./legacy-worker.js', import.meta.url));
}
