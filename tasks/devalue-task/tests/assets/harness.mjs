#!/usr/bin/env node
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { ASSET_ROOT, missing_files, read_registry } from './shared/integration/registry.mjs';
import { import_shared } from './shared/integration/shared-imports.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const reportPath = process.argv[2];
const repoRoot = resolve(process.env.MIGRATION_REPO_ROOT ?? process.cwd());
const preflightOnly = process.argv.includes('--preflight');

// Protected evaluation resources must exist before any candidate code runs.
// A missing resource is a delivery defect, never a candidate failure.
async function inspectAssets() {
	const errors = [];
	let registry;
	try {
		registry = read_registry(ASSET_ROOT);
	} catch (error) {
		errors.push(`registry is not readable: ${String((error && error.message) || error)}`);
		return { ok: false, errors, missing: [], shared: [] };
	}

	const missing = missing_files(ASSET_ROOT, registry);
	if (missing.length > 0) {
		errors.push(
			`missing evaluation assets: ${missing
				.map((entry) => `${entry.batch_id}:${entry.file}`)
				.join(', ')}`
		);
	}

	const shared = await import_shared(ASSET_ROOT);
	for (const entry of shared) {
		if (entry.status !== 'ok') {
			const detail = entry.error ?? `missing exports ${(entry.missing_exports ?? []).join(', ')}`;
			errors.push(`shared asset ${entry.path} is not loadable: ${entry.status}: ${detail}`);
		}
	}

	return { ok: errors.length === 0, errors, missing, shared };
}

if (!reportPath && !preflightOnly) {
	process.stderr.write('usage: node harness.mjs <report-path>\n');
	process.exitCode = 2;
} else {
	const assets = await inspectAssets();
	if (!assets.ok) {
		process.stderr.write(
			`MIGRATION_ASSET_INTEGRITY_ERROR: ${assets.errors.join('; ')}\n`
		);
		process.exitCode = 3;
	} else if (preflightOnly) {
		process.stdout.write(
			`${JSON.stringify({ status: 'PASS', asset_root: ASSET_ROOT, assets: assets.shared })}\n`
		);
	} else {
		// Temporal is part of the documented round-trip surface for devalue
		// 6.0.0.  Failing to load it must be observable as a failed Temporal
		// observation, never silently skipped.
		let temporal_error = null;
		try {
			if (!globalThis.Temporal) {
				const require = createRequire(join(repoRoot, 'package.json'));
				const polyfill = require('@js-temporal/polyfill');
				globalThis.Temporal ??= polyfill.Temporal;
			}
			if (!globalThis.Temporal) {
				temporal_error = 'the environment did not provide a Temporal implementation';
			}
		} catch (error) {
			temporal_error = String((error && error.message) || error);
		}

		try {
			const devalue = await import(pathToFileURL(join(repoRoot, 'index.js')).href);
			const { run } = await import(
				pathToFileURL(join(here, 'shared/batches/BATCH-P2P-0001/harness.mjs')).href
			);
			const crossInputsPath = process.env.MIGRATION_CROSS_INPUTS;
			const crossInputs =
				crossInputsPath && crossInputsPath.length > 0
					? JSON.parse(readFileSync(crossInputsPath, 'utf8'))
					: null;
			const payload = await run(devalue, {
				repoRoot,
				side: process.env.MIGRATION_SIDE ?? 'target',
				temporalError: temporal_error,
				crossInputs
			});
			writeFileSync(reportPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
		} catch (error) {
			process.stderr.write(`${error && error.stack ? error.stack : String(error)}\n`);
			process.exitCode = 1;
		}
	}
}

export { here, inspectAssets };
