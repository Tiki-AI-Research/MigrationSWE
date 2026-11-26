import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { binaryValues, customTypeRoundTrip, primitives, structured, temporalValue } from '../../../fixtures/batches/BATCH-P2P-0001/matrix.mjs';
import {
	canonicalValue,
	createObservations,
	equivalent,
	errorDetails,
	isDevalueError,
	jsonPayload
} from '../BATCH-P2P-0002/observations.js';
import { decode } from '../../../public/protocol.mjs';
import { runReplacerScenario, viewBackingOutcomes, captureAsyncOutcome, fixedUnevalValue } from '../../../public/replacer-cases.mjs';
import { typeSurfaceGate } from '../BATCH-F2P-0002/run.mjs';
import { primitiveEmissionCount } from '../../primitive-encoding.mjs';

const P2P_IDS = Array.from({ length: 51 }, (_, index) => `P2P-${String(index + 1).padStart(3, '0')}`);
// Observations whose behavior did not exist in 4.3.3 are still executed by the
// P2P scenario table, but they are reported as F2P so the protected source
// baseline can prove every P2P case already passed and every F2P case failed.
const RECLASSIFIED_P2P_IDS = new Set([
	'P2P-001',
	'P2P-003',
	'P2P-004',
	'P2P-005',
	'P2P-006',
	'P2P-009',
	'P2P-010',
	'P2P-012',
	'P2P-014',
	'P2P-015',
	'P2P-016',
	'P2P-017',
	'P2P-020',
	'P2P-021',
	'P2P-022',
	'P2P-025',
	'P2P-026',
	'P2P-029',
	'P2P-030',
	'P2P-032',
	'P2P-034',
	'P2P-035',
	'P2P-036',
	'P2P-037',
	'P2P-039',
	'P2P-045',
	'P2P-046',
	'P2P-048',
	'P2P-051'
]);
const F2P_IDS = [
	'F2P-001',
	'F2P-005',
	'F2P-006',
	'F2P-007',
	'F2P-008',
	'F2P-009',
	'F2P-010',
	'F2P-011',
	'F2P-012',
	'F2P-013',
	'F2P-014',
	'F2P-015',
	'F2P-016',
	'F2P-017',
	'F2P-018',
	'F2P-019',
	'F2P-020',
	'F2P-021',
	'F2P-022',
	'F2P-023',
	'F2P-024',
	'F2P-025',
	'F2P-026',
	'F2P-027',
	'F2P-028',
	'F2P-029',
	'F2P-030',
	'F2P-031',
	'F2P-032',
	'F2P-033',
	'F2P-034',
	'F2P-035'
];
const FORMAT_CROSS_INPUTS_SCHEMA = 'migration-format-cross-inputs:v1';
// A generated helper binding looks like `let a=...`, `const a=...` or `var a=...`.
// Trusted fragment text in the scored scenarios contains no declarations.
const GENERATED_BINDING = /\b(?:let|const|var)\s+[A-Za-z_$][\w$]*\s*=/;

function fail(message) {
	throw new Error(message);
}

function assert(condition, message) {
	if (!condition) fail(message);
}

function roundTrip(devalue, value) {
	return devalue.parse(devalue.stringify(value));
}

function evalJavaScript(source) {
	// `uneval` is documented to return a JavaScript expression.  Evaluating it
	// through Function keeps the check honest: a string that merely looks like
	// JavaScript but cannot run is a regression.
	const sandbox = process.env.MIGRATION_EVAL_SANDBOX;
	if (sandbox) {
		const completed = spawnSync(process.execPath, [sandbox], {
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
		});
		const lines = String(completed.stdout ?? '')
			.split(/\r?\n/)
			.filter(Boolean);
		const response = lines.length ? JSON.parse(lines.at(-1)) : null;
		if (!response?.ok) {
			throw new Error(
				`candidate uneval expression could not be evaluated: ${
					response?.error?.message ?? completed.stderr ?? 'no result'
				}`
			);
		}
		return decode(response.value);
	}
	return new Function(`return (${source});`)();
}

/**
 * Run an evaluator-owned replacer scenario.  The scored evaluator drives the
 * candidate across an out-of-process protocol that cannot carry callbacks, so
 * the scenario itself runs on the candidate side and this harness only sees
 * serialisable observations.  The direct (in-process) runner has no protocol
 * proxy, so it executes the identical scenario locally.
 */
function replacerScenario(devalue, name) {
	if (typeof devalue.unevalReplacerCase === 'function') {
		return devalue.unevalReplacerCase(name);
	}
	return runReplacerScenario(devalue, name);
}

function throwsDevalueError(fn, expectedPath) {
	try {
		fn();
		return false;
	} catch (error) {
		return isDevalueError(error) && (expectedPath === undefined || error.path === expectedPath);
	}
}

function captureError(fn) {
	try {
		fn();
		return { threw: false };
	} catch (error) {
		return { threw: true, ...errorDetails(error) };
	}
}

function repeatedValue() {
	const shared = { id: 'shared' };
	return { first: shared, second: shared };
}

function cyclicValue() {
	const value = { name: 'cycle' };
	value.self = value;
	return value;
}

function sparseValue() {
	const value = [];
	value.length = 1000000;
	value[13] = 'present';
	return value;
}

// P2P-008 claims only the special numeric and undefined values.  Keeping the
// case to exactly those values makes the wire evidence reproducible for both
// the 4.3.3 source and the 6.0.0 target: the source cannot serialize the
// target-only value shapes (Temporal and friends), and any case that bundled
// them would record a rejection on one side and a payload on the other.
function specialValue() {
	return {
		undefined: undefined,
		nan: NaN,
		infinity: Infinity,
		negative_infinity: -Infinity,
		negative_zero: -0
	};
}

function syncAsyncValue() {
	return { label: 'payload', count: 2, entries: new Map([['key', 'value']]) };
}

const FORMAT_CASES = [
	{
		id: 'repeat',
		observation_id: 'P2P-011',
		kind: 'value',
		build: repeatedValue
	},
	{
		id: 'cycle',
		observation_id: 'P2P-038',
		kind: 'value',
		build: cyclicValue
	},
	{
		id: 'special',
		observation_id: 'P2P-008',
		kind: 'value',
		build: specialValue
	},
	{
		id: 'sync-async',
		observation_id: 'P2P-051',
		kind: 'value',
		build: syncAsyncValue
	},
	{
		id: 'uneval-cycle',
		observation_id: 'P2P-044',
		kind: 'value',
		build: cyclicValue,
		async: false
	},
	{
		id: 'error-symbol',
		observation_id: 'P2P-014',
		kind: 'error',
		input: 'symbol',
		wrapped: true,
		path: '.symbol'
	},
	{
		id: 'error-function',
		observation_id: 'P2P-021',
		kind: 'error',
		input: 'function',
		path: ''
	},
	{
		id: 'error-thenable',
		observation_id: 'P2P-022',
		kind: 'error',
		input: 'thenable',
		path: '.thenable'
	},
	{
		id: 'malformed-unflatten',
		observation_id: 'P2P-007',
		kind: 'malformed-unflatten',
		unflatten_value: [['Uint8Array', 99]]
	}
];

function errorInput(name) {
	switch (name) {
		case 'symbol':
			return Symbol('x');
		case 'function':
			return function nope() {};
		case 'thenable':
			return { thenable: Promise.resolve(1) };
		default:
			fail(`unknown format error input: ${name}`);
	}
}

function safeParse(devalue, payload) {
	return canonicalValue(devalue.parse(JSON.stringify(payload)));
}

function safeUnflatten(devalue, payload) {
	return canonicalValue(devalue.unflatten(JSON.parse(JSON.stringify(payload))));
}

async function buildFormatEvidence(devalue, crossInputs) {
	const provided = new Map(
		(crossInputs?.cases ?? []).map((entry) => [String(entry.id), entry])
	);
	const records = {};
	for (const spec of FORMAT_CASES) {
		const input = provided.get(spec.id) ?? null;
		// A single unsupported value shape must not erase the evidence for the
		// cases the runtime does understand.  Each case is therefore recorded
		// independently and degrades to an explicit rejection record.
		try {
			if (spec.kind === 'value') {
				const value = spec.build();
				const payload = jsonPayload(devalue.stringify(value));
				// Older runtimes predate stringifyAsync; the sync payload is
				// still valid evidence, so only the async fields are absent.
				const asyncPayload =
					spec.async === false ||
					typeof devalue.stringifyAsync !== 'function'
						? null
						: jsonPayload(await devalue.stringifyAsync(value));
				const unevalSource = devalue.uneval(value);
				const sourcePayload = input?.payload ?? null;
				const sourceAsyncPayload = input?.async_payload ?? null;
				const sourceUneval = input?.uneval_source ?? null;
				records[spec.observation_id] = {
					case_id: spec.id,
					kind: spec.kind,
					payload,
					async_payload: asyncPayload,
					uneval_source: unevalSource,
					parse: canonicalValue(devalue.parse(JSON.stringify(payload))),
					unflatten: canonicalValue(devalue.unflatten(payload)),
					async_parse:
						asyncPayload === null
							? null
							: canonicalValue(
									devalue.parse(JSON.stringify(asyncPayload))
								),
					uneval_value: canonicalValue(evalJavaScript(unevalSource)),
					cross:
						sourcePayload === null
							? null
							: {
									parse: safeParse(devalue, sourcePayload),
									unflatten: safeUnflatten(devalue, sourcePayload),
									async_parse:
										sourceAsyncPayload === null
											? null
											: safeParse(devalue, sourceAsyncPayload),
									uneval:
										sourceUneval === null
											? null
											: canonicalValue(
													evalJavaScript(sourceUneval)
												)
								}
				};
				continue;
			}

			if (spec.kind === 'error') {
				const observed = captureError(() =>
					devalue.stringify(
						spec.wrapped
							? { symbol: errorInput(spec.input) }
							: errorInput(spec.input)
					)
				);
				records[spec.observation_id] = {
					case_id: spec.id,
					kind: spec.kind,
					input: spec.input,
					wrapped: Boolean(spec.wrapped),
					error: observed,
					path_matches:
						observed.threw &&
						typeof observed.path === 'string' &&
						observed.path === spec.path
				};
				continue;
			}

			if (spec.kind === 'malformed-parse') {
				const payload = input?.parse_payload ?? spec.parse_payload;
				records[spec.observation_id] = {
					case_id: spec.id,
					kind: spec.kind,
					parse_payload: payload,
					error: captureError(() => devalue.parse(payload))
				};
				continue;
			}

			if (spec.kind === 'malformed-unflatten') {
				const value = input?.unflatten_value ?? spec.unflatten_value;
				records[spec.observation_id] = {
					case_id: spec.id,
					kind: spec.kind,
					unflatten_value: value,
					error: captureError(() => devalue.unflatten(value))
				};
			}
		} catch (error) {
			records[spec.observation_id] = {
				case_id: spec.id,
				kind: spec.kind,
				error: { threw: true, ...errorDetails(error) }
			};
		}
	}
	return { records, digest: crossInputs?.digest ?? null };
}

function versionTriple(text) {
	const match = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?/.exec(String(text ?? '').trim());
	return match ? [Number(match[1]), Number(match[2] ?? 0), Number(match[3] ?? 0)] : null;
}

function compareVersions(left, right) {
	for (let index = 0; index < 3; index += 1) {
		const difference = (left[index] ?? 0) - (right[index] ?? 0);
		if (difference !== 0) return difference;
	}
	return 0;
}

function nodeEngineFloor(raw) {
	if (typeof raw !== 'string' || !raw.trim()) return null;
	const alternatives = raw.split('||').map((part) => part.trim()).filter(Boolean);
	if (!alternatives.length) return null;
	const floors = [];
	for (const alternative of alternatives) {
		// Accept the lower-bound shapes used by real manifests (`>=22.17`,
		// `^22.17.0`, `~22.17.0`, or an exact version).  A union is only
		// sufficient when every alternative already starts at or above the
		// target floor.
		const match = /^(>=|\^|~)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?/.exec(alternative);
		if (!match) return null;
		floors.push([
			Number(match[2]),
			Number(match[3] ?? 0),
			Number(match[4] ?? 0)
		]);
	}
	return floors.reduce((lowest, current) =>
		compareVersions(current, lowest) < 0 ? current : lowest
	);
}

function nodeEngineMeetsTarget(manifest) {
	const floor = nodeEngineFloor(manifest?.engines?.node);
	return floor !== null && compareVersions(floor, [22, 17, 0]) >= 0;
}

function rejectsKey(fn, payload) {
	try {
		fn(payload);
		return false;
	} catch {
		return true;
	}
}

function acceptsKey(fn, payload, key) {
	try {
		const value = fn(payload);
		// The public candidate protocol intentionally serializes plain JSON
		// values, so a null prototype is not observable across that boundary.
		// The contract only requires string keys to be accepted and restored.
		return Boolean(value) && typeof value === 'object' && value[key] === 'x';
	} catch {
		return false;
	}
}

export function nullPrototypeRejectsNonStringKey(devalue) {
	if (typeof devalue.unflatten !== 'function' || typeof devalue.parse !== 'function') {
		return false;
	}
	// Reject every JSON non-string key shape, not just the values used by a
	// particular probe.  Dynamic values make a hard-coded whitelist ineffective
	// while still asserting the same contract for all JSON key types.
	const nonce = Math.random().toString(36).slice(2);
	const dynamicNumber = 1_000_000 + Math.floor(Math.random() * 1_000_000);
	const invalidKeys = [
		0,
		1,
		-1,
		1.5,
		dynamicNumber,
		true,
		false,
		null,
		[],
		[0],
		[nonce],
		['__proto__'],
		['__proto__', 'x'],
		{},
		{ x: 1 },
		{ [nonce]: true },
		JSON.parse('{"__proto__":1}')
	];
	for (const key of invalidKeys) {
		const payload = [['null', key, 1], 'x'];
		const serialized = JSON.stringify(payload);
		if (!rejectsKey(devalue.unflatten, payload)) return false;
		if (!rejectsKey(devalue.parse, serialized)) return false;
	}
	// Use a valid value reference: an out-of-range reference would let a
	// decoder pass this check without ever rejecting the forbidden key.
	for (const payload of [
		[['null', '__proto__', 1], 'x'],
		[['null', '__proto__', -1]],
		[{ nested: 1 }, ['null', '__proto__', 2], 'x']
	]) {
		if (!rejectsKey(devalue.unflatten, payload)) return false;
		if (!rejectsKey(devalue.parse, JSON.stringify(payload))) return false;
	}
	// String keys, including keys that look like inherited properties, must
	// still decode after the non-string rejection is enforced.
	for (const key of ['', 'key', '0', 'constructor', 'toString']) {
		const payload = [['null', key, 1], 'x'];
		const serialized = JSON.stringify(payload);
		if (!acceptsKey(devalue.unflatten, payload, key)) return false;
		if (!acceptsKey(devalue.parse, serialized, key)) return false;
	}
	return true;
}

function packageState(repoRoot) {
	const read = (name) => {
		try {
			return readFileSync(join(repoRoot, name), 'utf8');
		} catch {
			return '';
		}
	};
	let manifest = {};
	try {
		manifest = JSON.parse(read('package.json'));
	} catch {
		manifest = {};
	}
	const lockfile = read('pnpm-lock.yaml');
	const declared = [];
	for (const field of ['dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies']) {
		const entries = manifest[field];
		if (entries && typeof entries === 'object' && Object.hasOwn(entries, 'devalue')) {
			declared.push(String(entries.devalue));
		}
	}
	return {
		manifest,
		lockfile,
		declared,
		hasOldDependency:
			declared.some((specifier) => specifier.includes('4.3.3')) ||
			/devalue@5\.9\.2(?![0-9])/.test(lockfile),
		hasTargetVersion: String(manifest.version) === '6.0.0',
		lockfileHasTarget: /devalue@6\.0\.0(?![0-9])/.test(lockfile)
	};
}

function visibleBytes(value) {
	return value instanceof Uint8Array ? [...value] : null;
}

function sameBytes(value, expected) {
	const bytes = visibleBytes(value);
	return (
		bytes !== null &&
		bytes.length === expected.length &&
		bytes.every((byte, index) => byte === expected[index])
	);
}

/**
 * devalue 6.0.0 serializes Node.js Buffers as Uint8Arrays holding only the
 * Buffer's visible bytes.  Small Buffers share a larger allocation pool, so
 * emitting the backing ArrayBuffer would disclose unrelated memory.
 */
async function bufferVisibleBytes(devalue) {
	const outcomes = {};
	// A payload that copies only the visible bytes stays tiny for these small
	// inputs.  A leaked allocation pool is thousands of bytes, so an explicit
	// size ceiling is a stable discriminator that does not depend on the exact
	// wire encoding.
	const compact = (payload, expected) =>
		typeof payload === 'string' && payload.length <= 128 + expected.length * 8;

	const checkSerialized = (label, payload, expected) => {
		let revived = null;
		try {
			revived = devalue.parse(payload);
		} catch {
			revived = null;
		}
		outcomes[label] =
			compact(payload, expected) &&
			revived !== null &&
			sameBytes(revived, expected) &&
			revived.byteLength === expected.length;
	};

	const pooled = Buffer.from([1, 2, 3]);
	const subarray = Buffer.from([255, 1, 2, 3, 255]).subarray(1, 4);
	const secret = Buffer.from('SECRET');
	const empty = secret.subarray(3, 3);

	checkSerialized('stringify', devalue.stringify(pooled), [1, 2, 3]);
	checkSerialized(
		'stringify_async',
		await devalue.stringifyAsync(pooled),
		[1, 2, 3]
	);
	checkSerialized('subarray', devalue.stringify(subarray), [1, 2, 3]);
	checkSerialized('empty_subarray', devalue.stringify(empty), []);

	const source = devalue.uneval(pooled);
	if (source.length <= 128) {
		const evaluated = evalJavaScript(source);
		outcomes.uneval =
			sameBytes(evaluated, [1, 2, 3]) && evaluated.byteLength === 3;
	} else {
		outcomes.uneval = false;
	}

	// A leaked pool also shows up as an oversized payload, independent of parse.
	outcomes.compact_payload = compact(devalue.stringify(pooled), [1, 2, 3]);

	const allocation = Buffer.allocUnsafe(128).fill(255);
	const first = allocation.subarray(8, 11);
	const second = allocation.subarray(24, 26);
	first.set([1, 2, 3]);
	second.set([4, 5]);

	const value = { first, again: first, second };
	value.self = value;
	const payload = devalue.stringify(value);
	let revived = null;
	try {
		revived = devalue.parse(payload);
	} catch {
		revived = null;
	}
	outcomes.shared_pool = Boolean(
		compact(payload, [1, 2, 3, 4, 5]) &&
			revived !== null &&
			revived.self === revived &&
			revived.first === revived.again &&
			sameBytes(revived.first, [1, 2, 3]) &&
			sameBytes(revived.second, [4, 5]) &&
			revived.first.byteLength === 3 &&
			revived.second.byteLength === 2
	);

	// Build ordinary views on the candidate side so the protocol cannot
	// normalize their offsets or discard bytes outside the visible window.
	try {
		const scenario = fixedUnevalScenario(devalue, 'view-backings');
		const inspect = (views) => views.map((view) => ({
			type: Object.prototype.toString.call(view),
			offset: view.byteOffset,
			length: view.byteLength,
			bytes: Array.from(new Uint8Array(view.buffer))
		}));
		// Project inside the evaluation sandbox, before its result is encoded.
		const actual = evalJavaScript(`(${inspect.toString()})((${scenario.source}))`);
		const expected = inspect(fixedUnevalValue('view-backings'));
		outcomes.ordinary_view_backings = JSON.stringify(actual) === JSON.stringify(expected);
	} catch {
		outcomes.ordinary_view_backings = false;
	}

	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

function wellFormedString(value) {
	if (typeof value.isWellFormed === 'function') return value.isWellFormed();
	return !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
}

function utf8Transport(value) {
	return Buffer.from(value, 'utf8').toString('utf8');
}

function unpairedSurrogateSamples() {
	return ['\ud800', '\udc00', 'a\ud800b', '\ud800\ud800', '\udc00\ud800'];
}

/**
 * The 6.0.0 replacer receives a `js` tag and returns a fragment whose holes are
 * serialized recursively.  Identity must survive across replaced and plain
 * values, so a shared object reachable from both must revive as one instance.
 */
function taggedTemplateReplacer(devalue) {
	const outcomes = {};
	const raw = replacerScenario(devalue, 'tagged');
	outcomes.tag_received = raw?.sawTag === true;
	if (raw?.error) outcomes.error = raw.error;
	let revived = null;
	if (typeof raw?.source === 'string') {
		try {
			revived = evalJavaScript(raw.source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	outcomes.rebuilt =
		revived !== null &&
		revived.first !== null &&
		revived.second !== null &&
		revived.first.kind === 'vector' &&
		revived.second.kind === 'vector' &&
		revived.first !== revived.second &&
		revived.first.y === 'a' &&
		revived.second.y === 'b';
	outcomes.shared_identity =
		revived !== null &&
		revived.first.x === revived.second.x &&
		revived.first.x === revived.list[0] &&
		revived.list[0] === revived.list[1] &&
		revived.first.x.n === 7;
	Object.assign(outcomes, scopedFragmentOutcomes(replacerScenario(devalue, 'scoped-shared')));
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

/** Evaluate candidate source outside its process, preserving shared identity. */
function scopedFragmentOutcomes(scoped) {
	const outcomes = {};
	let scopedValue = null;
	try {
		if (scoped?.error) throw new Error(scoped.error);
		scopedValue = evalJavaScript(scoped.source);
	} catch (error) {
		outcomes.scoped_error = String((error && error.message) || error);
	}
	const local = scopedValue?.[0];
	outcomes.scoped_shared_identity =
		local?.list?.[0]?.n === 11 && local.list[0] === local.list[1] &&
		local.list[0] === scopedValue[1] && local.calls === 1;
	outcomes.nested_scoped_identity =
		local?.nested?.[0]?.n === 13 && local.nested[0].kind === 'nested' &&
		local.nested[0] === local.nested[1];
	outcomes.shared_fragment_scope =
		local?.holders?.[0]?.[0]?.n === 17 && local.holders[0] === local.holders[1] &&
		local.holders[0][0] === local.holders[0][1];
	return outcomes;
}

/**
 * Only a `js` fragment or the documented fallbacks (`undefined`, `null`,
 * `false`) may come back from a replacer.  Every other return value - notably
 * the 5.x string form - must be a TypeError, not a silently ignored result.
 */
function replacerResultValidation(devalue) {
	const outcomes = {};
	const raw = replacerScenario(devalue, 'invalid-result');
	for (const row of raw?.rejections ?? []) {
		outcomes[`rejects:${row.index}:${row.type}`] = row.name === 'TypeError';
	}
	for (const row of raw?.fallbacks ?? []) {
		let revived = null;
		try {
			revived = evalJavaScript(row.source);
		} catch {
			revived = null;
		}
		outcomes[`falls_back:${row.index}:${row.label}`] =
			revived !== null && revived.a === 1;
	}
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

/**
 * Identifiers written in tagged-template literals - including nested templates -
 * are reserved, so hoisted generated names cannot shadow fragment locals.
 */
function reservedTemplateIdentifiers(devalue) {
	const outcomes = {};
	const text = 'x'.repeat(300);
	const raw = replacerScenario(devalue, 'reserved-identifiers');
	if (raw?.error) outcomes.error = raw.error;
	let revived = null;
	if (typeof raw?.source === 'string') {
		try {
			revived = evalJavaScript(raw.source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	outcomes.values =
		revived !== null && revived.one?.value === text && revived.two === text;
	outcomes.nested_local = revived !== null && revived.one?.keep === 'nested';
	outcomes.fragment_locals = revived !== null && revived.total === 'shadowshadow';
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

/**
 * A replaced value that cycles back into a Map must be constructed once, after
 * the Map's earlier entries exist, so its construction-time snapshot and the
 * insertion order both survive.
 */
function cyclicCustomMapOrdering(devalue) {
	const outcomes = {};
	const raw = replacerScenario(devalue, 'cyclic-ordering');
	if (raw?.error) outcomes.error = raw.error;
	let revived = null;
	if (typeof raw?.source === 'string') {
		try {
			revived = evalJavaScript(raw.source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	const list = revived?.list;
	const revivedWrapper = Array.isArray(list) ? list[0] : null;
	const revivedMap = Array.isArray(list) ? list[1] : null;
	outcomes.constructed_once = revived?.calls === 1;
	outcomes.prefix =
		Array.isArray(revivedWrapper?.prefix) &&
		revivedWrapper.prefix.length === 1 &&
		revivedWrapper.prefix[0] === 'before';
	outcomes.identity =
		revivedWrapper !== null &&
		revivedMap instanceof Map &&
		revivedWrapper.container === revivedMap &&
		revivedMap.get('owner') === revivedWrapper &&
		revivedMap.get('tail') === 43 &&
		revivedMap.get('before') === 42;
	outcomes.order =
		revivedMap instanceof Map &&
		Array.from(revivedMap.keys()).join('|') === 'before|owner|tail';
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

/**
 * Nothing in this graph is reachable twice, so every value belongs inline at
 * its occurrence.  A generated binding here is unnecessary, and a nested
 * fragment must stay inside the enclosing fragment's scope.
 */
function singleOccurrenceInlining(devalue) {
	const outcomes = {};
	const raw = replacerScenario(devalue, 'inline-single');
	if (raw?.error) outcomes.error = raw.error;
	const source = typeof raw?.source === 'string' ? raw.source : null;
	outcomes.no_generated_binding = source !== null && !GENERATED_BINDING.test(source);
	let revived = null;
	if (source !== null) {
		try {
			revived = evalJavaScript(source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	outcomes.rebuilt =
		revived !== null &&
		revived.outer?.inner?.n === 1 &&
		revived.outer?.tag === 'x' &&
		Array.isArray(revived.list) &&
		revived.list.length === 3 &&
		revived.list[0] === 1 &&
		revived.list[1] === 2 &&
		revived.list[2] === 3;
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

/**
 * A reviver can replace the serialized ArrayBuffer that a typed array or
 * DataView refers to.  Only a real ArrayBuffer/SharedArrayBuffer may back a
 * view; anything else must be rejected instead of coerced into an allocation.
 * Legitimate backings must keep their byteOffset, byteLength and bytes.
 */
function viewBackingBufferValidation(devalue) {
	// The scored evaluator drives the candidate through an out-of-process
	// protocol that cannot carry reviver callbacks, so the fixed scenario runs
	// on the candidate side and only its boolean outcomes cross the boundary.
	const outcomes =
		typeof devalue.viewBackingCase === 'function'
			? devalue.viewBackingCase()
			: localViewBackingBufferValidation(devalue);
	if (!outcomes || typeof outcomes !== 'object') return { ok: false, outcomes: {} };
	return { ok: Object.values(outcomes).every((value) => value === true), outcomes };
}

const localViewBackingBufferValidation = viewBackingOutcomes;

const DEDUP_LONG_STRING = `migration-long-${'x'.repeat(150)}`;
const DEDUP_LONG_BIGINT = 123456789012345678901234567890n;
const BOXED_LONG_STRING = `migration-boxed-${'q'.repeat(160)}`;

/**
 * Numeric diagnostics (for example an unhandled-rejection count) are reported
 * next to the booleans, so a passing gate must assert every non-count entry is
 * literally `true`.
 */
function outcomesPass(outcomes) {
	return Object.entries(outcomes).every(([key, value]) =>
		key.endsWith('_count') ? typeof value === 'number' : value === true
	);
}

async function localAsyncRejectionOutcome(devalue, scenario) {
	let unhandled = 0;
	const listener = () => {
		unhandled += 1;
	};
	process.on('unhandledRejection', listener);
	try {
		const outcome = await captureAsyncOutcome(devalue, scenario);
		// An unobserved rejection is reported on a later microtask turn.
		await new Promise((resolve) => setTimeout(resolve, 80));
		return { ...outcome, unhandled };
	} finally {
		process.removeListener('unhandledRejection', listener);
	}
}

const asyncRejectionReaders = new WeakMap();

/**
 * The scored evaluator reaches the candidate through an out-of-process
 * protocol, so the rejection can only be observed by the process that owns the
 * call.  The direct runner has no proxy and executes the same scenario
 * in-process.  Results are memoised so the control scenarios run once.
 */
function asyncRejectionReader(devalue) {
	const cached = asyncRejectionReaders.get(devalue);
	if (cached) return cached;
	const outcomes = new Map();
	const reader = async (scenario) => {
		if (!outcomes.has(scenario)) {
			outcomes.set(
				scenario,
				typeof devalue.asyncRejectionCase === 'function'
					? await devalue.asyncRejectionCase(scenario)
					: await localAsyncRejectionOutcome(devalue, scenario)
			);
		}
		return outcomes.get(scenario);
	};
	asyncRejectionReaders.set(devalue, reader);
	return reader;
}

async function asyncRejectionSettling(devalue, scenario, expectation) {
	const outcomes = {};
	let result = null;
	try {
		result = await asyncRejectionReader(devalue)(scenario);
	} catch (error) {
		outcomes.error = String((error && error.message) || error);
	}
	const settled = result !== null && typeof result === 'object';
	outcomes.settled = settled && typeof result.status === 'string';
	outcomes.rejected = settled && result.status === 'rejected';
	outcomes.unhandled_count =
		settled && typeof result.unhandled === 'number' ? result.unhandled : -1;
	outcomes.no_unhandled_rejection = settled && result.unhandled === 0;
	// Traversal may observe either the rejected thenable or the unsupported
	// sibling first. The instruction specifies the actual failure and error
	// fields, not a traversal algorithm, message wording or error priority.
	const paths = {
		'symbol-sibling': '.bad', 'nested-function': '.nest.deep', 'map-value': '.bad',
		'array-element': '[1]', 'late-rejection': '.bad', 'symbol-before': '.bad',
		'array-before': '[0]', 'nested-before': '.bad', 'late-before': '.bad'
	};
	const markers = scenario === 'two-rejections' ? ['first', 'second']
		: scenario === 'getter-failure' ? [scenario, 'getter']
		: scenario === 'reducer-failure' ? [scenario, 'reducer'] : [scenario];
	const failureKind = ['nested-function', 'array-element', 'array-before', 'late-before'].includes(scenario)
		? 'function' : 'symbol';
	const validDevalueFailure = result?.devalue_error === true && result?.root_matches === true &&
		result?.value_kind === failureKind && (scenario === 'map-before'
			? typeof result.path === 'string' && result.path.startsWith('.m.get(')
			: result?.path === paths[scenario]);
	outcomes.error_matches = settled && (markers.includes(result.marker) || validDevalueFailure);

	const single = await asyncRejectionReader(devalue)('solo-rejected');
	outcomes.control_single_rejection =
		single?.status === 'rejected' &&
		single?.marker === 'solo' &&
		single?.unhandled === 0;
	const resolved = await asyncRejectionReader(devalue)('solo-resolved');
	outcomes.control_resolved =
		resolved?.status === 'resolved' && resolved?.value_matches === true && resolved?.unhandled === 0;
	if (scenario === 'symbol-sibling') {
		for (const extra of ['symbol-before', 'array-before', 'nested-before', 'map-before', 'late-before', 'getter-failure', 'reducer-failure']) {
			const checked = await asyncRejectionSettling(devalue, extra, expectation);
			outcomes[`handles_${extra}`] = checked.ok;
		}
	}
	return { ok: outcomesPass(outcomes), outcomes };
}

function fixedUnevalScenario(devalue, scenario) {
	if (typeof devalue.fixedUnevalCase === 'function') {
		return devalue.fixedUnevalCase(scenario);
	}
	return localFixedUnevalScenario(devalue, scenario);
}

function localFixedUnevalScenario(devalue, scenario) {
	if (scenario === 'view-backings' || scenario === 'own-indices') {
		return { source: devalue.uneval(fixedUnevalValue(scenario)), marker: null };
	}
	if (scenario === 'boxed-string') {
		const boxed = new String(BOXED_LONG_STRING);
		return {
			source: devalue.uneval({
				a: boxed,
				b: boxed,
				c: BOXED_LONG_STRING,
				d: BOXED_LONG_STRING
			}),
			marker: BOXED_LONG_STRING
		};
	}
	if (scenario === 'inherited-tail') {
		const prototype = [];
		prototype[2] = 'prototype-value';
		const array = [1, 2];
		array.length = 3;
		Object.setPrototypeOf(array, prototype);
		return { source: devalue.uneval(array), marker: null };
	}
	throw new Error(`unknown uneval scenario: ${scenario}`);
}

/**
 * A repeated long string must be emitted once and referenced from every
 * position where it occurs.
 */
function repeatedLongStringEncoding(devalue) {
	const outcomes = {};
	const value = {
		a: { b: [DEDUP_LONG_STRING, DEDUP_LONG_STRING] },
		c: DEDUP_LONG_STRING
	};
	let source = null;
	try {
		source = devalue.uneval(value);
	} catch (error) {
		outcomes.error = String((error && error.message) || error);
	}
	outcomes.emitted_once = primitiveEmissionCount(source, DEDUP_LONG_STRING) === 1;
	let revived = null;
	if (typeof source === 'string') {
		try {
			revived = evalJavaScript(source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	outcomes.revived_direct = revived?.c === DEDUP_LONG_STRING;
	outcomes.revived_nested =
		Array.isArray(revived?.a?.b) &&
		revived.a.b.length === 2 &&
		revived.a.b[0] === DEDUP_LONG_STRING &&
		revived.a.b[1] === DEDUP_LONG_STRING;
	return { ok: outcomesPass(outcomes), outcomes };
}

/**
 * The same deduplication must reach Map keys, Map values and Set members while
 * preserving entry order and member structure.
 */
function repeatedLongStringInCollections(devalue) {
	const outcomes = {};
	const value = {
		map: new Map([
			[DEDUP_LONG_STRING, 1],
			['k', DEDUP_LONG_STRING]
		]),
		set: new Set([{ v: DEDUP_LONG_STRING }, { v: DEDUP_LONG_STRING }])
	};
	let source = null;
	try {
		source = devalue.uneval(value);
	} catch (error) {
		outcomes.error = String((error && error.message) || error);
	}
	outcomes.emitted_once = primitiveEmissionCount(source, DEDUP_LONG_STRING) === 1;
	let revived = null;
	if (typeof source === 'string') {
		try {
			revived = evalJavaScript(source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	const map = revived?.map;
	const keys = map instanceof Map ? [...map.keys()] : [];
	outcomes.map_entries =
		map instanceof Map &&
		map.size === 2 &&
		keys[0] === DEDUP_LONG_STRING &&
		keys[1] === 'k' &&
		map.get(DEDUP_LONG_STRING) === 1 &&
		map.get('k') === DEDUP_LONG_STRING;
	const members = revived?.set instanceof Set ? [...revived.set] : [];
	outcomes.set_members =
		members.length === 2 &&
		members[0] !== members[1] &&
		members.every((member) => member && member.v === DEDUP_LONG_STRING);
	return { ok: outcomesPass(outcomes), outcomes };
}

function repeatedBigIntEncoding(devalue) {
	const outcomes = {};
	const value = [
		DEDUP_LONG_BIGINT,
		DEDUP_LONG_BIGINT,
		{ x: DEDUP_LONG_BIGINT },
		DEDUP_LONG_BIGINT
	];
	let source = null;
	try {
		source = devalue.uneval(value);
	} catch (error) {
		outcomes.error = String((error && error.message) || error);
	}
	outcomes.emitted_once =
		primitiveEmissionCount(source, DEDUP_LONG_BIGINT) === 1;
	let revived = null;
	if (typeof source === 'string') {
		try {
			revived = evalJavaScript(source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	outcomes.values_identical =
		Array.isArray(revived) &&
		revived.length === 4 &&
		revived[0] === DEDUP_LONG_BIGINT &&
		revived[1] === DEDUP_LONG_BIGINT &&
		revived[2]?.x === DEDUP_LONG_BIGINT &&
		revived[3] === DEDUP_LONG_BIGINT;
	for (const integer of [7n, 123456789012345678901234567890n]) {
		try {
			const pair = devalue.uneval([integer, integer]);
			const result = evalJavaScript(pair);
			outcomes[`pair_${integer}`] = primitiveEmissionCount(pair, integer) === 1 &&
				result[0] === integer && result[1] === integer;
		} catch { outcomes[`pair_${integer}`] = false; }
	}

	return { ok: outcomesPass(outcomes), outcomes };
}

function repeatedBigIntInCollections(devalue) {
	const outcomes = {};
	const value = {
		map: new Map([[DEDUP_LONG_BIGINT, DEDUP_LONG_BIGINT]]),
		set: new Set([{ v: DEDUP_LONG_BIGINT }, { v: DEDUP_LONG_BIGINT }])
	};
	let source = null;
	try {
		source = devalue.uneval(value);
	} catch (error) {
		outcomes.error = String((error && error.message) || error);
	}
	outcomes.emitted_once =
		primitiveEmissionCount(source, DEDUP_LONG_BIGINT) === 1;
	let revived = null;
	if (typeof source === 'string') {
		try {
			revived = evalJavaScript(source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	const map = revived?.map;
	outcomes.map_entries =
		map instanceof Map &&
		map.size === 1 &&
		map.get(DEDUP_LONG_BIGINT) === DEDUP_LONG_BIGINT;
	const members = revived?.set instanceof Set ? [...revived.set] : [];
	outcomes.set_members =
		members.length === 2 &&
		members[0] !== members[1] &&
		members.every((member) => member && member.v === DEDUP_LONG_BIGINT);
	return { ok: outcomesPass(outcomes), outcomes };
}

/**
 * A boxed primitive shared across positions must revive as one wrapper, and
 * the long primitive it wraps must still be emitted once even though the same
 * string also appears unwrapped.
 */
function boxedPrimitiveDeduplication(devalue) {
	const outcomes = {};
	const scenario = fixedUnevalScenario(devalue, 'boxed-string');
	const source = typeof scenario?.source === 'string' ? scenario.source : null;
	const marker =
		typeof scenario?.marker === 'string' ? scenario.marker : BOXED_LONG_STRING;
	outcomes.emitted_once = primitiveEmissionCount(source, marker) === 1;
	let revived = null;
	if (source !== null) {
		try {
			revived = evalJavaScript(source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	outcomes.wrapper_identity =
		revived?.a === revived?.b &&
		revived?.a instanceof String &&
		String(revived.a) === marker;
	outcomes.unwrapped_positions = revived?.c === marker && revived?.d === marker;
	return { ok: outcomesPass(outcomes), outcomes };
}

/**
 * An array may inherit an index from its prototype.  Inherited elements are
 * not own elements and must not shorten the revived array or leak a value.
 */
function inheritedTrailingIndex(devalue) {
	const outcomes = {};
	const scenario = fixedUnevalScenario(devalue, 'inherited-tail');
	const source = typeof scenario?.source === 'string' ? scenario.source : null;
	outcomes.source_is_string = source !== null;
	let revived = null;
	if (source !== null) {
		try {
			revived = evalJavaScript(source);
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	outcomes.is_array = Array.isArray(revived);
	outcomes.length_preserved = Array.isArray(revived) && revived.length === 3;
	outcomes.hole_preserved = Array.isArray(revived) && !Object.hasOwn(revived, 2);
	outcomes.inherited_value_absent = Array.isArray(revived) && revived[2] === undefined;
	outcomes.own_values =
		Array.isArray(revived) && revived[0] === 1 && revived[1] === 2;
	outcomes.own_keys =
		Array.isArray(revived) && Object.keys(revived).join(',') === '0,1';
	try {
		const own = fixedUnevalScenario(devalue, 'own-indices');
		const inspect = (arrays) => Array.isArray(arrays) &&
			arrays[0]?.length === 1000 && Object.hasOwn(arrays[0], 7) &&
			arrays[0][7]?.label === 'own' && arrays[0][50] === 'visible' &&
			Object.getOwnPropertyNames(arrays[0]).filter(key => key !== 'length').join(',') === '7,50' &&
			arrays[1]?.length === 4 && arrays[1][2] === arrays[1] &&
			Object.getOwnPropertyNames(arrays[1]).filter(key => key !== 'length').join(',') === '2';
		// Check ownership and identity before the transport drops descriptors.
		outcomes.non_enumerable_own_indices =
			evalJavaScript(`(${inspect.toString()})((${own.source}))`) === true;
	} catch {
		outcomes.non_enumerable_own_indices = false;
	}
	return { ok: outcomesPass(outcomes), outcomes };
}

function unpairedSurrogateStringify(devalue) {
	const outcomes = {};
	for (const [index, value] of unpairedSurrogateSamples().entries()) {
		try {
			const payload = devalue.stringify(value);
			const transported = utf8Transport(payload);
			outcomes[`stringify:${index}`] =
				wellFormedString(payload) &&
				transported === payload &&
				devalue.parse(transported) === value;
		} catch {
			outcomes[`stringify:${index}`] = false;
		}
	}
	const pair = 'a\ud83d\ude00b';
	try {
		const payload = devalue.stringify(pair);
		outcomes.well_formed_pair =
			wellFormedString(payload) &&
			payload.includes('\ud83d\ude00') &&
			devalue.parse(payload) === pair;
	} catch {
		outcomes.well_formed_pair = false;
	}
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

function unpairedSurrogateUneval(devalue) {
	const outcomes = {};
	for (const [index, value] of unpairedSurrogateSamples().entries()) {
		try {
			const source = devalue.uneval(value);
			const transported = utf8Transport(source);
			outcomes[`uneval:${index}`] =
				wellFormedString(source) &&
				transported === source &&
				evalJavaScript(transported) === value;
		} catch {
			outcomes[`uneval:${index}`] = false;
		}
	}
	const pair = 'a\ud83d\ude00b';
	try {
		outcomes.well_formed_pair =
			evalJavaScript(utf8Transport(devalue.uneval(pair))) === pair;
	} catch {
		outcomes.well_formed_pair = false;
	}
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

function nestedSurrogateValue() {
	const key = 'k\ud800';
	return {
		[key]: ['a\udc00b', { deep: '\ud800\ud800' }],
		map: new Map([['m\udc00', 'v\ud800']]),
		set: new Set(['s\ud800'])
	};
}

function nestedSurrogatesMatch(revived) {
	if (revived === null || typeof revived !== 'object') return false;
	const key = 'k\ud800';
	if (!Object.hasOwn(revived, key)) return false;
	if (revived[key]?.[0] !== 'a\udc00b') return false;
	if (revived[key]?.[1]?.deep !== '\ud800\ud800') return false;
	if (!(revived.map instanceof Map)) return false;
	if (revived.map.get('m\udc00') !== 'v\ud800') return false;
	if (!(revived.set instanceof Set)) return false;
	if (!revived.set.has('s\ud800')) return false;
	return true;
}

async function nestedSurrogateStringify(devalue) {
	const outcomes = {};
	const value = nestedSurrogateValue();
	const inspect = (label, payload) => {
		outcomes[label] =
			typeof payload === 'string' &&
			wellFormedString(payload) &&
			utf8Transport(payload) === payload &&
			nestedSurrogatesMatch(devalue.parse(utf8Transport(payload)));
	};
	try {
		inspect('stringify', devalue.stringify(value));
	} catch {
		outcomes.stringify = false;
	}
	try {
		inspect('stringify_async', await devalue.stringifyAsync(value));
	} catch {
		outcomes.stringify_async = false;
	}
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

function nestedSurrogateUneval(devalue) {
	const outcomes = {};
	let source = null;
	try {
		source = devalue.uneval(nestedSurrogateValue());
	} catch (error) {
		outcomes.error = String((error && error.message) || error);
	}
	outcomes.well_formed = typeof source === 'string' && wellFormedString(source);
	outcomes.transport =
		typeof source === 'string' && utf8Transport(source) === source;
	let revived = null;
	if (typeof source === 'string') {
		try {
			revived = evalJavaScript(utf8Transport(source));
		} catch (error) {
			outcomes.error = String((error && error.message) || error);
		}
	}
	outcomes.matches = nestedSurrogatesMatch(revived);
	return { ok: Object.values(outcomes).every(Boolean), outcomes };
}

async function runF2P(id, repoRoot, devalue) {
	const state = packageState(repoRoot);
	let ok = false;
	let behavior = null;
	switch (id) {
		case 'F2P-001':
			// One dependency-policy gate: target manifest/lock state plus the
			// absence of every 4.3.3 declaration.
			ok =
				state.hasTargetVersion &&
				!state.hasOldDependency &&
				!state.declared.some((specifier) => specifier.includes('4.3.3'));
			break;
		case 'F2P-005':
			ok = nodeEngineMeetsTarget(state.manifest);
			break;
		case 'F2P-006':
			ok = nullPrototypeRejectsNonStringKey(devalue);
			break;
		case 'F2P-007':
			behavior = await bufferVisibleBytes(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-008':
			behavior = taggedTemplateReplacer(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-009':
			behavior = replacerResultValidation(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-010':
			behavior = reservedTemplateIdentifiers(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-011':
			behavior = cyclicCustomMapOrdering(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-012':
			behavior = unpairedSurrogateStringify(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-013':
			behavior = unpairedSurrogateUneval(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-014':
			behavior = await nestedSurrogateStringify(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-015':
			behavior = nestedSurrogateUneval(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-016':
			behavior = singleOccurrenceInlining(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-017':
			behavior = viewBackingBufferValidation(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-018':
			behavior = await asyncRejectionSettling(devalue, 'symbol-sibling', {
				kind: 'devalue',
				token: 'Symbol'
			});
			ok = behavior.ok;
			break;
		case 'F2P-019':
			behavior = await asyncRejectionSettling(devalue, 'nested-function', {
				kind: 'devalue',
				token: 'function'
			});
			ok = behavior.ok;
			break;
		case 'F2P-020':
			behavior = await asyncRejectionSettling(devalue, 'two-rejections', {
				kind: 'rejection',
				markers: ['first', 'second']
			});
			ok = behavior.ok;
			break;
		case 'F2P-021':
			behavior = await asyncRejectionSettling(devalue, 'map-value', {
				kind: 'devalue',
				token: 'Symbol'
			});
			ok = behavior.ok;
			break;
		case 'F2P-022':
			behavior = await asyncRejectionSettling(devalue, 'array-element', {
				kind: 'devalue',
				token: 'function'
			});
			ok = behavior.ok;
			break;
		case 'F2P-023':
			behavior = await asyncRejectionSettling(devalue, 'late-rejection', {
				kind: 'devalue',
				token: 'Symbol'
			});
			ok = behavior.ok;
			break;
		case 'F2P-024':
			behavior = repeatedLongStringEncoding(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-025':
			behavior = repeatedLongStringInCollections(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-026':
			behavior = repeatedBigIntEncoding(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-027':
			behavior = boxedPrimitiveDeduplication(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-028':
			behavior = inheritedTrailingIndex(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-029':
			behavior = repeatedBigIntInCollections(devalue);
			ok = behavior.ok;
			break;
		case 'F2P-030':
		case 'F2P-031':
		case 'F2P-032':
		case 'F2P-033':
		case 'F2P-034':
		case 'F2P-035':
			behavior = typeSurfaceGate(repoRoot, id);
			ok = behavior.ok;
			break;
		default:
			fail(`unknown F2P observation ${id}`);
	}
	return {
		checks: {
			target_version: state.hasTargetVersion,
			no_old_dependency: !state.hasOldDependency,
			declared_dependency_count: state.declared.length,
			node_engine: state.manifest?.engines?.node ?? null,
			node_engine_at_least_22_17: nodeEngineMeetsTarget(state.manifest),
			null_prototype_non_string_key_rejected: id === 'F2P-006' ? ok : undefined,
			buffer_visible_bytes: id === 'F2P-007' && behavior ? behavior.outcomes : undefined,
			behavior: behavior && id !== 'F2P-007' ? behavior.outcomes : undefined
		},
		ok
	};
}

function publicExportCheck(devalue) {
	const names = [
		'uneval',
		'parse',
		'unflatten',
		'stringify',
		'stringifyAsync',
		'defaultStringifyOperations',
		'defaultParseOperations',
		'DevalueError',
		'filterArrayIndices'
	];
	return names.every((name) => name in devalue);
}

function makeChecks(devalue, repoRoot) {
	const shared = createObservations(devalue);
	const binary = binaryValues();

	const checks = {
		'P2P-001': () => {
			const values = [structured, ...primitives, binary.buffer, binary.typed];
			const ok = values.every((value) => equivalent(value, roundTrip(devalue, value)));
			return { ok, checks: { values: values.length } };
		},
		'P2P-002': () => {
			const value = { a: { b: [1, { c: 'd' }] }, e: null, f: true };
			return { ok: equivalent(value, roundTrip(devalue, value)), checks: { equivalent: true } };
		},
		'P2P-003': () => {
			const value = binary.view;
			const revived = roundTrip(devalue, value);
			const evaluated = evalJavaScript(devalue.uneval(value));
			return {
				ok: equivalent(value, revived) && equivalent(value, evaluated),
				checks: { parse: equivalent(value, revived), uneval: equivalent(value, evaluated) }
			};
		},
		'P2P-004': () => {
			const values = [structured, ...primitives, binary.buffer, binary.typed, binary.view];
			const results = values.map((value) => equivalent(value, roundTrip(devalue, value)));
			return { ok: results.every(Boolean), checks: { values: values.length } };
		},
		'P2P-005': () => {
			const revived = roundTrip(devalue, binary.buffer);
			return {
				ok: equivalent(binary.buffer, revived),
				checks: { type: revived instanceof ArrayBuffer }
			};
		},
		'P2P-006': () => {
			const temporal = temporalValue();
			const values = [
				42n,
				new Date('2024-01-02T03:04:05.000Z'),
				/ab+c/gi,
				new Map([['a', 1]]),
				new Set([1, 2]),
				binary.buffer,
				binary.typed,
				binary.view,
				new URL('https://example.com/a?b=1'),
				new URLSearchParams([
					['a', '1'],
					['b', '2']
				]),
				temporal
			];
			const results = values.map((value) => equivalent(value, roundTrip(devalue, value)));
			return { ok: results.every(Boolean), checks: { values: values.length, temporal: true } };
		},
		'P2P-007': () => {
			const parseRejected = shared.malformedParse('[["Uint8Array",99]]');
			const unflattenRejected = shared.malformedUnflatten([['Uint8Array', 99]]);
			return { ok: parseRejected && unflattenRejected, checks: { parse: parseRejected, unflatten: unflattenRejected } };
		},
		'P2P-008': () => {
			const values = [undefined, NaN, Infinity, -Infinity, -0];
			const ok = values.every((value) => {
				const revived = roundTrip(devalue, value);
				if (typeof value === 'number' && Number.isNaN(value)) return Number.isNaN(revived);
				return Object.is(value, revived);
			});
			return { ok, checks: { values: values.length } };
		},
		'P2P-009': () => {
			let captured = null;
			try {
				devalue.stringify({ bad: () => 1 });
			} catch (error) {
				captured = error;
			}
			return {
				ok: isDevalueError(captured) && typeof devalue.filterArrayIndices === 'function',
				checks: { error: isDevalueError(captured), filter: typeof devalue.filterArrayIndices === 'function' }
			};
		},
		'P2P-010': () => {
			const state = packageState(repoRoot);
			const manifest = state.manifest;
			const testScript = String(manifest.scripts?.test ?? '');
			let binaryRoundTrip = false;
			try {
				const bytes = new Uint8Array([0, 1, 2, 127, 128, 255]);
				const revived = roundTrip(devalue, bytes);
				binaryRoundTrip =
					revived instanceof Uint8Array &&
					revived.length === bytes.length &&
					bytes.every((value, index) => revived[index] === value);
			} catch {
				binaryRoundTrip = false;
			}
			return {
				ok: Boolean(testScript) && binaryRoundTrip,
				checks: {
					test_script: Boolean(testScript),
					binary_round_trip: binaryRoundTrip
				}
			};
		},
		'P2P-011': () => {
			const sharedValue = { id: 'shared' };
			const value = { first: sharedValue, second: sharedValue };
			const revived = roundTrip(devalue, value);
			return { ok: revived.first === revived.second, checks: { same_instance: revived.first === revived.second } };
		},
		'P2P-012': () => {
			const value = [];
			value.length = 1000000;
			value[13] = 'present';
			const serialized = devalue.stringify(value);
			const revived = devalue.parse(serialized);
			return {
				ok: serialized.length < 1000 && revived.length === 1000000 && revived[13] === 'present',
				checks: { payload_small: serialized.length < 1000, length: revived.length, value: revived[13] === 'present' }
			};
		},
		'P2P-013': () => {
			const stringified = devalue.stringify(
				{ custom: 1 },
				undefined,
				{ operations: { typeOf: (value) => (value === null ? 'null' : typeof value) } }
			);
			const revived = devalue.parse(stringified);
			return {
				ok: revived.custom === 1 && Object.isFrozen(devalue.defaultParseOperations),
				checks: { operations_usable: revived.custom === 1, parse_frozen: Object.isFrozen(devalue.defaultParseOperations) }
			};
		},
		'P2P-014': () => {
			let captured = null;
			try {
				devalue.stringify({ symbol: Symbol('x') });
			} catch (error) {
				captured = error;
			}
			return {
				ok: isDevalueError(captured) && captured.path === '.symbol',
				checks: { devalue_error: isDevalueError(captured), path: captured?.path === '.symbol' }
			};
		},
		'P2P-015': () => {
			const values = [binary.buffer, binary.typed, binary.view];
			const ok = values.every((value) => equivalent(value, roundTrip(devalue, value)));
			return { ok, checks: { values: values.length } };
		},
		'P2P-016': () => {
			return {
				ok:
					'defaultStringifyOperations' in devalue &&
					Object.isFrozen(devalue.defaultStringifyOperations),
				checks: { exported: 'defaultStringifyOperations' in devalue, frozen: Object.isFrozen(devalue.defaultStringifyOperations) }
			};
		},
		'P2P-017': () => {
			// The instruction requires this new export, without prescribing its input signature.
			const exported = typeof devalue.filterArrayIndices === 'function';
			return { ok: exported, checks: { exported } };
		},
		'P2P-018': () => {
			const values = [
				new Date('2020-02-02T00:00:00.000Z'),
				/xyz/gi,
				new Map([[1, 2]]),
				new Set(['x'])
			];
			return { ok: values.every((value) => equivalent(value, roundTrip(devalue, value))), checks: { values: values.length } };
		},
		'P2P-019': () => {
			const value = 123456789012345678901234567890n;
			return {
				ok: roundTrip(devalue, value) === value,
				checks: { same: roundTrip(devalue, value) === value }
			};
		},
		'P2P-020': () => {
			return {
				ok: typeof devalue.filterArrayIndices === 'function',
				checks: { exported: typeof devalue.filterArrayIndices === 'function' }
			};
		},
		'P2P-021': () => {
			const results = [
				throwsDevalueError(() => devalue.stringify(function nope() {}), ''),
				throwsDevalueError(() => devalue.stringify(Symbol('nope')), ''),
				throwsDevalueError(() => devalue.stringify(Promise.resolve(1)), '')
			];
			return { ok: results.every(Boolean), checks: { cases: results.length } };
		},
		'P2P-022': () => {
			const results = [
				throwsDevalueError(() => devalue.stringify({ a: function nope() {} }, undefined), '.a'),
				throwsDevalueError(() => devalue.stringify([Symbol('nope')]), '[0]'),
				throwsDevalueError(() => devalue.stringify({ thenable: Promise.resolve(1) }), '.thenable')
			];
			return { ok: results.every(Boolean), checks: { cases: results.length } };
		},
		'P2P-023': () => {
			const date = new Date('2024-06-07T08:09:10.000Z');
			const regexp = /a+b/gi;
			const revivedDate = roundTrip(devalue, date);
			const revivedRegExp = roundTrip(devalue, regexp);
			return {
				ok:
					revivedDate instanceof Date &&
					revivedDate.getTime() === date.getTime() &&
					revivedRegExp instanceof RegExp &&
					revivedRegExp.source === regexp.source &&
					revivedRegExp.flags === regexp.flags,
				checks: { date: revivedDate.getTime() === date.getTime(), regexp: String(revivedRegExp) === String(regexp) }
			};
		},
		'P2P-024': () => {
			const manifest = packageState(repoRoot).manifest;
			const root = manifest.exports && manifest.exports['.'];
			const expected = {
				types: './types/index.d.ts',
				import: './index.js',
				default: './index.js'
			};
			const targets = root && typeof root === 'object'
				? {
					types: root.types,
					import: root.import,
					default: root.default
				}
				: {};
			const ok =
				root &&
				['types', 'import', 'default'].every(
					(key) =>
						typeof root[key] === 'string' &&
						root[key] === expected[key]
				);
			return {
				ok: Boolean(ok),
				checks: {
					conditions: Boolean(
						root &&
						['types', 'import', 'default'].every(
							(key) => typeof root[key] === 'string'
						)
					),
					types: targets.types ?? null,
					import: targets.import ?? null,
					default: targets.default ?? null
				}
			};
		},
		'P2P-025': () => {
			const values = [binary.buffer, binary.typed, binary.view];
			const ok = values.every((value) => {
				const revived = roundTrip(devalue, value);
				return equivalent(value, revived) && revived.constructor === value.constructor;
			});
			return { ok, checks: { values: values.length } };
		},
		'P2P-026': () => {
			return {
				ok: typeof devalue.filterArrayIndices === 'function',
				checks: { exported: typeof devalue.filterArrayIndices === 'function' }
			};
		},
		'P2P-027': () => {
			const values = [42, 'text', true, { a: [1, 2] }];
			const ok = values.every((value) => equivalent(value, evalJavaScript(devalue.uneval(value))));
			return { ok, checks: { values: values.length } };
		},
		'P2P-028': () => {
			const value = { parse: ['same', 1], nested: new Map([['x', 2]]) };
			const serialized = devalue.stringify(value);
			const parsed = devalue.parse(serialized);
			const flattened = devalue.unflatten(JSON.parse(serialized));
			return { ok: equivalent(parsed, flattened), checks: { equivalent: true } };
		},
		'P2P-029': () => {
			const url = new URL('https://example.com/path?a=1&b=two');
			const params = new URLSearchParams([
				['a', '1'],
				['b', 'two']
			]);
			const revivedUrl = roundTrip(devalue, url);
			const revivedParams = roundTrip(devalue, params);
			return {
				ok:
					revivedUrl instanceof URL &&
					revivedUrl.href === url.href &&
					revivedParams instanceof URLSearchParams &&
					revivedParams.toString() === params.toString(),
				checks: { url: revivedUrl.href === url.href, params: revivedParams.toString() === params.toString() }
			};
		},
		'P2P-030': () => {
			const values = [
				new Uint8Array(8),
				new Uint8Array(256).map((_, index) => index),
				new Uint8Array(65536).map((_, index) => index % 251)
			];
			const ok = values.every((value) => equivalent(value, roundTrip(devalue, value)));
			return { ok, checks: { sizes: values.map((value) => value.length) } };
		},
		'P2P-031': () => {
			const values = [NaN, Infinity, -Infinity, -0];
			const ok = values.every((value) => Object.is(value, roundTrip(devalue, value)));
			return { ok, checks: { values: values.length } };
		},
		'P2P-032': async () => {
			const value = { label: 'payload', map: new Map([['key', 'value']]) };
			const syncPayload = devalue.stringify(value);
			const asyncPayload = await devalue.stringifyAsync(value);
			const fromSync = devalue.parse(syncPayload);
			const fromAsync = devalue.parse(asyncPayload);
			return {
				ok: equivalent(fromSync, value) && equivalent(fromAsync, value),
				checks: { sync: equivalent(fromSync, value), async: equivalent(fromAsync, value) }
			};
		},
		'P2P-033': () => {
			const values = [undefined, NaN, Infinity, -Infinity, -0, 1n];
			const ok = values.every((value) => {
				const revived = roundTrip(devalue, value);
				return typeof value === 'number' && Number.isNaN(value) ? Number.isNaN(revived) : Object.is(value, revived);
			});
			return { ok, checks: { values: values.length } };
		},
		'P2P-034': () => {
			const value = [];
			value.length = 100000000;
			value[99999999] = 'last';
			const serialized = devalue.stringify(value);
			const revived = devalue.parse(serialized);
			return {
				ok: serialized.length < 1000 && revived.length === value.length && revived[99999999] === 'last',
				checks: { payload_small: serialized.length < 1000, length: revived.length }
			};
		},
		'P2P-035': () => {
			const ok = publicExportCheck(devalue) &&
				typeof devalue.stringify === 'function' &&
				typeof devalue.stringifyAsync === 'function' &&
				typeof devalue.parse === 'function' &&
				typeof devalue.unflatten === 'function' &&
				typeof devalue.uneval === 'function';
			return { ok, checks: { callable: ok } };
		},
		'P2P-036': () => {
			const original = devalue.defaultStringifyOperations;
			const observed = devalue.stringifyWithTypeofOverride
				? devalue.stringifyWithTypeofOverride({ a: 1 })
				: null;
			const called = Boolean(observed?.operations_called);
			const serialized = observed?.value;
			const revived = devalue.parse(serialized);
			return {
				ok: revived.a === 1 && original === devalue.defaultStringifyOperations,
				checks: { override_called: called, parse: revived.a === 1, defaults_unchanged: original === devalue.defaultStringifyOperations }
			};
		},
		'P2P-037': () => {
			const ok = publicExportCheck(devalue);
			return { ok, checks: { exports: 9 } };
		},
		'P2P-038': () => {
			const shared = { id: 1 };
			const value = { left: shared, right: shared };
			value.self = value;
			const revived = roundTrip(devalue, value);
			return {
				ok: revived.self === revived && revived.left === revived.right,
				checks: { cycle: revived.self === revived, shared: revived.left === revived.right }
			};
		},
		'P2P-039': async () => {
			const value = { a: 1, b: [2, 3], c: new Map([['x', 4]]) };
			const sync = devalue.parse(devalue.stringify(value));
			const async = devalue.parse(await devalue.stringifyAsync(value));
			return { ok: equivalent(sync, async), checks: { equivalent: equivalent(sync, async) } };
		},
		'P2P-040': () => {
			const available =
				typeof Uint8Array.fromBase64 === 'function' &&
				typeof Uint8Array.prototype.toBase64 === 'function';
			if (!available) {
				return { ok: true, checks: { platform_base64: false, skipped: true } };
			}
			const bytes = new Uint8Array([0, 1, 2, 250, 255]);
			const encoded = bytes.toBase64();
			const decoded = Uint8Array.fromBase64(encoded);
			return {
				ok: equivalent(bytes, decoded),
				checks: { platform_base64: true }
			};
		},
		'P2P-041': () => {
			const cases = ['definitely not json', '{}', '[["NotAType",0]]'];
			const ok = cases.every((payload) => shared.malformedParse(payload));
			return { ok, checks: { cases: cases.length } };
		},
		'P2P-042': () => {
			const value = { name: 'roundtrip', nested: { values: [1, 2, 3] } };
			return { ok: equivalent(value, roundTrip(devalue, value)), checks: { equivalent: true } };
		},
		'P2P-043': () => {
			return { ok: customTypeRoundTrip(devalue), checks: { class_identity: true } };
		},
		'P2P-044': () => {
			const value = { name: 'cycle' };
			value.self = value;
			const source = devalue.uneval(value);
			const revived = evalJavaScript(source);
			return {
				ok: revived !== value && revived.self === revived,
				checks: { cycle: revived.self === revived, executable: true }
			};
		},
		'P2P-045': () => {
			const cases = [
				'definitely not json', '[["Uint8Array",99]]',
				'[["null","__proto__",1]]'
			];
			const malformed = [
				[{ bad: -7 }], [{ bad: -99 }], [{ bad: 1.5 }], [{ bad: 99 }],
				[['Map', 1]], [['null', 'key']], [[-7, 2, 0]],
				[['Date']], [['RegExp']], [['ArrayBuffer', '!']]
			];
			const parse = cases.every((payload) => shared.malformedParse(payload)) &&
				malformed.every((payload) => shared.malformedParse(JSON.stringify(payload)));
			const unflatten = [...malformed, [{ bad: NaN }], [{ bad: Infinity }]]
				.every((payload) => shared.malformedUnflatten(payload));
			return { ok: parse && unflatten, checks: { parse, unflatten, cases: cases.length + malformed.length } };
		},
		'P2P-046': () => {
			const value = [];
			value.length = 50000000;
			value[17] = 'present';
			const serialized = devalue.stringify(value);
			const revived = devalue.parse(serialized);
			return {
				ok: serialized.length < 1000 && revived.length === value.length && revived[17] === 'present',
				checks: { payload_small: serialized.length < 1000, length: revived.length }
			};
		},
		'P2P-047': () => {
			const values = [undefined, NaN, Infinity, -Infinity, -0, 42n];
			const ok = values.every((value) => {
				const revived = roundTrip(devalue, value);
				return typeof value === 'number' && Number.isNaN(value) ? Number.isNaN(revived) : Object.is(value, revived);
			});
			return { ok, checks: { values: values.length } };
		},
		'P2P-048': () => {
			const bytes = new Uint8Array([0, 1, 2, 127, 128, 255]);
			const revived = roundTrip(devalue, bytes);
			return { ok: equivalent(bytes, revived), checks: { bytes: bytes.length } };
		},
		'P2P-049': () => {
			const map = new Map([
				['alpha', 1],
				['beta', { nested: true }]
			]);
			const set = new Set([1, 'two', { three: 3 }]);
			const revivedMap = roundTrip(devalue, map);
			const revivedSet = roundTrip(devalue, set);
			return {
				ok:
					revivedMap instanceof Map &&
					revivedSet instanceof Set &&
					equivalent(map, revivedMap) &&
					equivalent(set, revivedSet),
				checks: { map: revivedMap instanceof Map, set: revivedSet instanceof Set, equivalent: equivalent(map, revivedMap) && equivalent(set, revivedSet) }
			};
		},
		'P2P-050': () => {
			const sharedValue = { id: 'shared' };
			const value = { first: sharedValue, second: sharedValue };
			value.self = value;
			const revived = roundTrip(devalue, value);
			return {
				ok: revived.self === revived && revived.first === revived.second,
				checks: { cycle: revived.self === revived, shared: revived.first === revived.second }
			};
		},
		'P2P-051': async () => {
			const value = { label: 'payload', count: 2, entries: new Map([['key', 'value']]) };
			const syncPayload = devalue.stringify(value);
			const asyncPayload = await devalue.stringifyAsync(value);
			const fromSync = devalue.parse(syncPayload);
			const fromAsync = devalue.parse(asyncPayload);
			return {
				ok: equivalent(fromSync, value) && equivalent(fromAsync, value),
				checks: { sync: equivalent(fromSync, value), async: equivalent(fromAsync, value) }
			};
		}
	};

	return {
		P2P_IDS,
		F2P_IDS,
		async run(crossInputs = null) {
			const rows = [];
			let format = { records: {}, digest: crossInputs?.digest ?? null };
			try {
				format = await buildFormatEvidence(devalue, crossInputs);
			} catch (error) {
				const detail = String((error && error.message) || error);
				for (const spec of FORMAT_CASES) {
					format.records[spec.observation_id] = {
						case_id: spec.id,
						error: { stage: 'format-evidence', message: detail }
					};
				}
			}
			for (const id of P2P_IDS) {
				try {
					const result = await checks[id]();
					const checksValue = { ...(result.checks ?? {}) };
					if (Object.hasOwn(format.records, id)) {
						checksValue.format = format.records[id];
					}
					rows.push({
						id,
						kind: RECLASSIFIED_P2P_IDS.has(id) ? 'F2P' : 'P2P',
						status: result.ok ? 'PASS' : 'FAIL',
						value: { type: 'json', value: checksValue },
						evidence: {
							source: 'shared-batch-harness',
							check: id,
							format_cross_inputs_sha256: format.digest
						}
					});
				} catch (error) {
					const failureValue = {
						error: String((error && error.message) || error)
					};
					if (Object.hasOwn(format.records, id)) {
						failureValue.format = format.records[id];
					}
					rows.push({
						id,
						kind: RECLASSIFIED_P2P_IDS.has(id) ? 'F2P' : 'P2P',
						status: 'FAIL',
						value: { type: 'json', value: failureValue },
						evidence: {
							source: 'shared-batch-harness',
							check: id,
							format_cross_inputs_sha256: format.digest
						}
					});
				}
			}
			for (const id of F2P_IDS) {
				const source =
					id === 'F2P-001' || id === 'F2P-005'
						? 'dependency-resolution-check'
						: 'shared-batch-harness';
				try {
					const result = await runF2P(id, repoRoot, devalue);
					rows.push({
						id,
						kind: 'F2P',
						status: result.ok ? 'PASS' : 'FAIL',
						value: { type: 'json', value: result.checks },
						evidence: { source, check: id }
					});
				} catch (error) {
					rows.push({
						id,
						kind: 'F2P',
						status: 'FAIL',
						value: { type: 'json', value: { error: String((error && error.message) || error) } },
						evidence: { source, check: id }
					});
				}
			}
			return rows;
		}
	};
}

export async function run(
	devalue,
	{ repoRoot, temporalError = null, crossInputs = null }
) {
	const suite = makeChecks(devalue, repoRoot);
	const observations = await suite.run(crossInputs);
	if (temporalError) {
		const row = observations.find((observation) => observation.id === 'P2P-006');
		if (row) {
			row.status = 'FAIL';
			row.value = {
				type: 'json',
				value: { error: `Temporal unavailable: ${temporalError}` }
			};
		}
	}
	return {
		schema_version: 'migration-observations:v6',
		observations
	};
}

export {
	canonicalValue, P2P_IDS, F2P_IDS, repeatedBigIntEncoding,
	repeatedBigIntInCollections, repeatedLongStringEncoding,
	repeatedLongStringInCollections, boxedPrimitiveDeduplication, scopedFragmentOutcomes
};
