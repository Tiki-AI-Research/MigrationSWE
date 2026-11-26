#!/usr/bin/env node
/**
 * Evaluator-owned observation runner.
 *
 * This process owns the hidden harness, fixtures and report writer.  Candidate
 * code is only reached through the minimal public protocol runner, which is
 * started under a separate identity and Node permission sandbox.
 */
import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { decode, encode } from '../public/protocol.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const reportPath = process.argv[2];
const preflightOnly = process.argv.includes('--preflight');
const repoRoot = resolve(process.env.MIGRATION_REPO_ROOT ?? process.cwd());
const publicAssets = resolve(process.env.MIGRATION_PUBLIC_ASSETS ?? '.');
const operationTimeout = Number(process.env.MIGRATION_OPERATION_TIMEOUT ?? '60');

if (!reportPath && !preflightOnly) {
	process.stderr.write('usage: node evaluator-runner.mjs <report-path>\n');
	process.exitCode = 2;
} else {
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

	if (preflightOnly) {
		const { run } = await import(
			pathToFileURL(join(here, '../shared/batches/BATCH-P2P-0001/harness.mjs'))
				.href
		);
		if (typeof run !== 'function') process.exitCode = 3;
		else
			process.stdout.write(
				`${JSON.stringify({
					schema_version: 'migration-evaluator-preflight:v1',
					status: 'PASS'
				})}\n`
			);
	} else {
		const runner = join(publicAssets, 'candidate-runner.mjs');
		const uid = process.env.MIGRATION_CANDIDATE_UID;
		const gid = process.env.MIGRATION_CANDIDATE_GID ?? uid;
		const candidateModules =
			process.env.MIGRATION_EVALUATOR_NODE_MODULES ??
			'/opt/migration/dependencies/node_modules';

		function candidateCommand() {
			const node = [
				process.execPath,
				'--permission',
				`--allow-fs-read=${repoRoot}`,
				`--allow-fs-read=${publicAssets}`
			];
			node.push(`--allow-fs-read=${candidateModules}`, runner);
			if (process.geteuid() === 0 && uid) {
				return [
					'setpriv',
					`--reuid=${uid}`,
					`--regid=${gid}`,
					'--clear-groups',
					'--',
					...node
				];
			}
			return node;
		}

		class CandidateCallError extends Error {
			constructor(details) {
				super(details?.message ?? 'candidate protocol call failed');
				this.name = details?.name ?? 'CandidateCallError';
				this.path = details?.path ?? null;
				if (details?.has_value) this.value = undefined;
				if (details?.has_root) this.root = undefined;
			}
		}

		function call(operation, payload = {}) {
			const [file, ...args] = candidateCommand();
			const completed = spawnSync(file, args, {
				cwd: repoRoot,
				input: `${JSON.stringify({ operation, ...payload })}\n`,
				encoding: 'utf8',
				errors: 'replace',
				timeout: operationTimeout * 1000,
				maxBuffer: 256 * 1024 * 1024,
				env: {
					...process.env,
					MIGRATION_REPO_ROOT: repoRoot,
					MIGRATION_CANDIDATE_NODE_MODULES: candidateModules,
					HOME: process.env.MIGRATION_CANDIDATE_HOME ?? '/tmp'
				}
			});
			if (completed.error) throw completed.error;
			if (completed.signal) {
				throw new Error(
					`candidate protocol operation ${operation} terminated by ${completed.signal}`
				);
			}
			const lines = String(completed.stdout ?? '')
				.split(/\r?\n/)
				.filter((line) => line.trim().length > 0);
			let response = null;
			for (let index = lines.length - 1; index >= 0; index -= 1) {
				try {
					const parsed = JSON.parse(lines[index]);
					if (
						parsed?.schema_version === 'migration-candidate-protocol:v1'
					) {
						response = parsed;
						break;
					}
				} catch {
					// Candidate console output is not protocol data.
				}
			}
			if (!response) {
				throw new Error(
					`candidate protocol operation ${operation} produced no response `
						+ `(exit ${completed.status}): `
						+ String(completed.stderr ?? '').slice(-2000)
				);
			}
			if (response.ok !== true) {
				throw new CandidateCallError(response.error);
			}
			return response;
		}

		const capability = call('capabilities');
		const devalue = {};
		for (const [name, exported] of Object.entries(
			capability.exports ?? {}
		)) {
			if (!exported) continue;
			if (name === 'defaultStringifyOperations') {
				devalue[name] = capability.defaults_frozen?.stringify
					? Object.freeze({})
					: {};
			} else if (name === 'defaultParseOperations') {
				devalue[name] = capability.defaults_frozen?.parse
					? Object.freeze({})
					: {};
			} else if (name === 'DevalueError') {
				devalue[name] = CandidateCallError;
			}
		}
		if (capability.callable?.stringify) {
			devalue.stringify = (value) =>
				call('stringify', { value: encode(value) }).value;
		}
		if (capability.callable?.stringifyAsync) {
			devalue.stringifyAsync = async (value) =>
				call('stringifyAsync', { value: encode(value) }).value;
		}
		if (capability.callable?.parse) {
			devalue.parse = (payload) =>
				decode(call('parse', { payload }).value);
		}
		if (capability.callable?.unflatten) {
			devalue.unflatten = (value) =>
				decode(call('unflatten', { value: encode(value) }).value);
		}
		if (capability.callable?.uneval) {
			devalue.uneval = (value) =>
				call('uneval', { value: encode(value) }).value;
			devalue.unevalReplacerCase = (scenario) =>
				call('uneval-replacer', { scenario: String(scenario) }).value;
		}
		devalue.viewBackingCase = () => call('view-backing').value;
		devalue.asyncRejectionCase = (scenario) =>
			call('async-rejection', { scenario: String(scenario) }).value;
		devalue.fixedUnevalCase = (scenario) =>
			call('uneval-case', { scenario: String(scenario) }).value;
		if (capability.callable?.filterArrayIndices) {
			devalue.filterArrayIndices = (value) =>
				decode(
					call('filterArrayIndices', { value }).value
				);
		}
		devalue.stringifyWithTypeofOverride = (value) =>
			call('stringify-typeof-override', {
				value: encode(value)
			});
		devalue.customTypeRoundTrip = () =>
			call('custom-type-roundtrip').value;

		const crossInputsPath = process.env.MIGRATION_CROSS_INPUTS;
		const crossInputs =
			crossInputsPath && crossInputsPath.length > 0
				? JSON.parse(readFileSync(crossInputsPath, 'utf8'))
				: null;
		try {
			const { run } = await import(
				pathToFileURL(
					join(here, '../shared/batches/BATCH-P2P-0001/harness.mjs')
				).href
			);
			const payload = await run(devalue, {
				repoRoot,
				side: 'target',
				temporalError: globalThis.Temporal
					? null
					: 'the environment did not provide a Temporal implementation',
				crossInputs
			});
			writeFileSync(
				reportPath,
				`${JSON.stringify(payload, null, 2)}\n`,
				'utf8'
			);
		} catch (error) {
			process.stderr.write(
				`${error && error.stack ? error.stack : String(error)}\n`
			);
			process.exitCode = 1;
		}
	}
}
