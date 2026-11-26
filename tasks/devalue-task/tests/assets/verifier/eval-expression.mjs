#!/usr/bin/env node
/** Evaluate one candidate-provided JavaScript expression in a fresh VM. */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import vm from 'node:vm';
import { encode } from '../public/protocol.mjs';

if (!globalThis.Temporal) {
	for (const directory of [
		process.env.MIGRATION_EVALUATOR_NODE_MODULES,
		'/opt/migration/dependencies/node_modules'
	]) {
		if (!directory) continue;
		try {
			const require = createRequire(join(directory, 'index.js'));
			const polyfill = require(
				join(directory, '@js-temporal/polyfill')
			);
			globalThis.Temporal = polyfill.Temporal;
			break;
		} catch {
			// Try the next evaluator-owned module directory.
		}
	}
}

const request = JSON.parse(readFileSync(0, 'utf8'));
const source = String(request.source ?? '');
try {
	const context = vm.createContext(
		Object.assign(Object.create(null), {
			Temporal: globalThis.Temporal,
			// `uneval` can emit these web-platform constructors.  A fresh VM
			// context only carries ECMAScript built-ins, so expose the
			// evaluator runtime's implementations rather than making a valid
			// source expression look like a format failure.
			URL: globalThis.URL,
			URLSearchParams: globalThis.URLSearchParams,
			atob: globalThis.atob,
			btoa: globalThis.btoa,
			Buffer: globalThis.Buffer
		})
	);
	const value = new vm.Script(`(${source})`, {
		filename: 'candidate-uneval.js'
	}).runInContext(context, { timeout: 2000 });
	process.stdout.write(
		`${JSON.stringify({ ok: true, value: encode(value) })}\n`
	);
} catch (error) {
	process.stdout.write(
		`${JSON.stringify({
			ok: false,
			error: {
				name: typeof error?.name === 'string' ? error.name : null,
				message:
					typeof error?.message === 'string'
						? error.message
						: String(error)
			}
		})}\n`
	);
}
