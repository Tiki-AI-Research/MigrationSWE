#!/usr/bin/env node
/**
 * Evaluator-owned source consumer for target -> source format compatibility.
 *
 * This process never imports candidate code.  It reads the payloads produced
 * by the candidate report and replays them through the protected 4.3.3 source
 * runtime, writing only canonical revival evidence.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import {
	canonicalValue,
	errorDetails
} from '../shared/batches/BATCH-P2P-0002/observations.js';
import { decode } from '../public/protocol.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const [inputPath, outputPath] = process.argv.slice(2);

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

function evalJavaScript(source) {
	const completed = spawnSync(
		process.execPath,
		[join(here, 'eval-expression.mjs')],
		{
			input: `${JSON.stringify({ source })}\n`,
			encoding: 'utf8',
			errors: 'replace',
			timeout: 10000,
			env: {
				...process.env,
				MIGRATION_EVALUATOR_NODE_MODULES:
					process.env.MIGRATION_EVALUATOR_NODE_MODULES ??
					'/opt/migration/dependencies/node_modules'
			}
		}
	);
	if (completed.error) throw completed.error;
	if (completed.signal) {
		throw new Error(
			`candidate uneval expression terminated by ${completed.signal}`
		);
	}
	const lines = String(completed.stdout ?? '')
		.split(/\r?\n/)
		.filter(Boolean);
	let response = null;
	for (const line of lines.reverse()) {
		try {
			response = JSON.parse(line);
			break;
		} catch {
			// Ignore non-protocol output.
		}
	}
	if (!response?.ok) {
		throw new Error(
			`candidate uneval expression could not be evaluated: ${
				response?.error?.message ?? completed.stderr ?? 'no result'
			}`
		);
	}
	return decode(response.value);
}

function captureError(fn) {
	try {
		fn();
		return { threw: false };
	} catch (error) {
		return { threw: true, ...errorDetails(error) };
	}
}

const source = await import(
	pathToFileURL(join(here, '..', 'source-runtime', 'index.js')).href
);
const request = JSON.parse(readFileSync(inputPath, 'utf8'));
const cases = [];
for (const entry of request.cases ?? []) {
	const record = {
		case_id: entry.case_id,
		observation_id: entry.observation_id,
		kind: entry.kind
	};
	try {
		if (entry.kind === 'value') {
			record.parse = canonicalValue(
				source.parse(JSON.stringify(entry.payload))
			);
			record.unflatten = canonicalValue(source.unflatten(entry.payload));
			record.async_parse =
				entry.async_payload === null
					? null
					: canonicalValue(
							source.parse(JSON.stringify(entry.async_payload))
						);
			record.uneval = canonicalValue(
				evalJavaScript(entry.uneval_source)
			);
		} else if (entry.kind === 'error') {
			record.error = captureError(() => {
				const value = entry.input === 'symbol'
					? Symbol('x')
					: entry.input === 'function'
						? function nope() {}
						: { thenable: Promise.resolve(1) };
				source.stringify(entry.wrapped ? { symbol: value } : value);
			});
		} else if (entry.kind === 'malformed-parse') {
			record.error = captureError(() => source.parse(entry.parse_payload));
		} else if (entry.kind === 'malformed-unflatten') {
			record.error = captureError(() =>
				source.unflatten(entry.unflatten_value)
			);
		} else {
			record.error = { stage: 'unsupported-case', kind: entry.kind };
		}
	} catch (error) {
		record.error = {
			stage: 'source-consumption',
			message: String((error && error.message) || error)
		};
	}
	cases.push(record);
}
writeFileSync(
	outputPath,
	`${JSON.stringify({
		schema_version: 'migration-source-consume:v1',
		source_commit: request.source_commit ?? null,
		cases
	})}\n`,
	'utf8'
);
