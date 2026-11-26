#!/usr/bin/env node
/**
 * Minimal candidate-side protocol runner.
 *
 * The evaluator starts this file as the isolated candidate identity.  It
 * imports the candidate package and returns only serialized operation results;
 * it never receives fixtures, expected values or a report path.
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { decode, encode } from './protocol.mjs';
import { runReplacerScenario, viewBackingOutcomes, captureAsyncOutcome, fixedUnevalValue } from './replacer-cases.mjs';

const repoRoot = resolve(process.env.MIGRATION_REPO_ROOT ?? process.cwd());

const dependencyRoots = [
	repoRoot,
	process.cwd(),
	process.env.MIGRATION_CANDIDATE_NODE_MODULES
].filter(Boolean);

if (!globalThis.Temporal) {
	for (const base of dependencyRoots) {
		try {
			const require = createRequire(join(base, 'package.json'));
			const polyfill = require('@js-temporal/polyfill');
			globalThis.Temporal ??= polyfill.Temporal;
			if (globalThis.Temporal) break;
		} catch {
			// The candidate package may not depend on the polyfill.
		}
	}
}

function errorPayload(error) {
	return {
		name: typeof error?.name === 'string' ? error.name : null,
		message:
			typeof error?.message === 'string' ? error.message : String(error),
		path: typeof error?.path === 'string' ? error.path : null,
		devalue_error:
			Boolean(error) &&
			typeof error === 'object' &&
			error.name === 'DevalueError' &&
			typeof error.message === 'string' &&
			typeof error.path === 'string' &&
			'value' in error &&
			'root' in error,
		has_value: Boolean(error) && typeof error === 'object' && 'value' in error,
		has_root: Boolean(error) && typeof error === 'object' && 'root' in error
	};
}

/**
 * Promise rejections that settle after a `stringifyAsync` call must be
 * observable from the process that owns the call, so the counter lives here.
 * A listener is installed for the lifetime of the process: without one Node
 * would terminate on the first unhandled rejection and the scenario could
 * never be measured.
 */
let unhandledRejections = 0;
process.on('unhandledRejection', () => {
	unhandledRejections += 1;
});

async function asyncRejectionOutcome(devalue, scenario) {
	const before = unhandledRejections;
	const outcome = await captureAsyncOutcome(devalue, scenario);
	// A rejection that nobody awaited is reported on a later microtask turn.
	await new Promise((resolve) => setTimeout(resolve, 80));
	return { ...outcome, unhandled: unhandledRejections - before };
}

/**
 * Values that cannot cross the protocol without losing their identity (boxed
 * primitives) or their prototype chain (inherited array indices) are built
 * here.  Only the emitted source and the marker string cross the boundary.
 */
function fixedUnevalOutcome(devalue, scenario) {
	switch (scenario) {
		case 'view-backings':
		case 'own-indices':
			return { source: devalue.uneval(fixedUnevalValue(scenario)), marker: null };
		case 'boxed-string': {
			const inner = `migration-boxed-${'q'.repeat(160)}`;
			const boxed = new String(inner);
			return {
				source: devalue.uneval({ a: boxed, b: boxed, c: inner, d: inner }),
				marker: inner
			};
		}
		case 'inherited-tail': {
			const prototype = [];
			prototype[2] = 'prototype-value';
			const array = [1, 2];
			array.length = 3;
			Object.setPrototypeOf(array, prototype);
			return { source: devalue.uneval(array), marker: null };
		}
		default:
			throw new Error(`unknown uneval scenario: ${scenario}`);
	}
}

async function main() {
	const request = JSON.parse(readFileSync(0, 'utf8'));
	const operation = String(request.operation ?? '');
	const devalue = await import(
		pathToFileURL(join(repoRoot, 'index.js')).href
	);
	const response = { schema_version: 'migration-candidate-protocol:v1' };

	const call = async () => {
		switch (operation) {
			case 'capabilities':
				return {
					exports: Object.fromEntries(
						[
							'uneval',
							'parse',
							'unflatten',
							'stringify',
							'stringifyAsync',
							'defaultStringifyOperations',
							'defaultParseOperations',
							'DevalueError',
							'filterArrayIndices'
						].map((name) => [name, name in devalue])
					),
					callable: Object.fromEntries(
						[
							'uneval',
							'parse',
							'unflatten',
							'stringify',
							'stringifyAsync',
							'filterArrayIndices'
						].map((name) => [name, typeof devalue[name] === 'function'])
					),
					defaults_frozen: {
						stringify: Object.isFrozen(
							devalue.defaultStringifyOperations
						),
						parse: Object.isFrozen(devalue.defaultParseOperations)
					}
				};
			case 'stringify':
				return { value: devalue.stringify(decode(request.value)) };
			case 'stringifyAsync':
				return {
					value: await devalue.stringifyAsync(decode(request.value))
				};
			case 'parse':
				return { value: encode(devalue.parse(request.payload)) };
			case 'unflatten':
				return { value: encode(devalue.unflatten(decode(request.value))) };
			case 'uneval':
				return { value: devalue.uneval(decode(request.value)) };
			case 'filterArrayIndices':
				return {
					value: encode(devalue.filterArrayIndices(request.value))
				};
			case 'stringify-typeof-override': {
				let called = false;
				const original = devalue.defaultStringifyOperations;
				const operations = {
					typeOf: (value) => {
						called = true;
						return value === null ? 'null' : typeof value;
					}
				};
				const payload = devalue.stringify(
					decode(request.value),
					undefined,
					{ operations }
				);
				return {
					value: payload,
					operations_called: called,
					defaults_unchanged:
						original === devalue.defaultStringifyOperations
				};
			}
			case 'uneval-replacer':
				return {
					value: runReplacerScenario(
						devalue,
						String(request.scenario ?? '')
					)
				};
			case 'view-backing':
				return { value: viewBackingOutcomes(devalue) };
			case 'async-rejection':
				return {
					value: await asyncRejectionOutcome(
						devalue,
						String(request.scenario ?? '')
					)
				};
			case 'uneval-case':
				return {
					value: fixedUnevalOutcome(
						devalue,
						String(request.scenario ?? '')
					)
				};
			case 'custom-type-roundtrip': {
				class Foo {
					constructor(value) {
						this.value = value;
					}
				}
				const value = new Foo({ answer: 42 });
				const serialized = devalue.stringify(value, {
					Foo: (candidate) =>
						candidate instanceof Foo && candidate.value
				});
				const restored = devalue.parse(serialized, {
					Foo: (payload) => new Foo(payload)
				});
				return {
					value:
						restored instanceof Foo &&
						restored.value.answer === 42
				};
			}
			default:
				throw new Error(`unsupported protocol operation: ${operation}`);
		}
	};

	try {
		Object.assign(response, await call());
		response.ok = true;
	} catch (error) {
		response.ok = false;
		response.error = errorPayload(error);
	}
	process.stdout.write(`${JSON.stringify(response)}\n`);
}

if (process.argv.includes('--preflight')) {
	process.stdout.write(
		`${JSON.stringify({
			schema_version: 'migration-candidate-protocol:v1',
			status: 'PASS'
		})}\n`
	);
} else {
	await main();
}
