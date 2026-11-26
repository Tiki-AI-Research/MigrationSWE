/**
 * Replacer scenarios for the candidate protocol.
 *
 * The scored evaluator drives the candidate out of process, so a replacer
 * callback cannot cross the protocol boundary as a function.  These scenarios
 * live on the candidate side (evaluator-owned, read-only) and return only
 * serialisable data; every assertion and every `eval` of the produced source
 * stays in the hidden harness.
 */

/**
 * @param {{
 *   uneval: (value: unknown, replacer: (value: unknown, js: unknown) => unknown) => unknown
 * }} devalue
 * @param {string} scenario
 */
export function runReplacerScenario(devalue, scenario) {
	switch (scenario) {
		case 'tagged':
			return taggedTemplateReplacer(devalue);
		case 'scoped-shared':
			return scopedSharedFragments(devalue);
		case 'invalid-result':
			return replacerResultValidation(devalue);
		case 'reserved-identifiers':
			return reservedTemplateIdentifiers(devalue);
		case 'cyclic-ordering':
			return cyclicCustomMapOrdering(devalue);
		case 'inline-single':
			return singleOccurrenceInlining(devalue);
		default:
			throw new Error(`unknown replacer scenario: ${scenario}`);
	}
}

/** Build values whose backing bytes or property descriptors the JSON protocol loses. */
export function fixedUnevalValue(scenario) {
	if (scenario === 'view-backings') {
		const buffer = (length) => Uint8Array.from({ length }, (_, i) => i + 1).buffer;
		return [
			new Uint16Array(buffer(5), 2, 1),
			new Float64Array(buffer(9), 0, 1),
			new DataView(buffer(7), 1, 3),
			new Uint8Array(Buffer.alloc(16, 23).buffer, 3, 2)
		];
	}
	if (scenario === 'own-indices') {
		const sparse = new Array(1000);
		Object.defineProperty(sparse, '7', { value: { label: 'own' }, enumerable: false });
		sparse[50] = 'visible';
		const cyclic = new Array(4);
		Object.defineProperty(cyclic, '2', { value: cyclic, enumerable: false });
		return [sparse, cyclic];
	}
	throw new Error(`unknown fixed uneval value: ${scenario}`);
}

/** @param {unknown} error */
function errorMessage(error) {
	return String((error && /** @type {any} */ (error).message) || error);
}

/**
 * The 6.0.0 replacer receives a `js` tag and returns a fragment whose holes are
 * serialized recursively.  Identity must survive across replaced and plain
 * values, so a shared object reachable from both must revive as one instance.
 *
 * @param {any} devalue
 */
function taggedTemplateReplacer(devalue) {
	const shared = { n: 7 };
	const first = { x: shared, y: 'a' };
	const second = { x: shared, y: 'b' };
	const value = { first, second, list: [shared, shared] };
	let sawTag = false;
	let source = null;
	let error = null;
	try {
		source = devalue.uneval(value, (thing, js) => {
			if (typeof js === 'function') sawTag = true;
			if (thing === value) {
				return js`(()=>{class Vector{constructor(x,y){this.x=x;this.y=y;this.kind='vector';}}return {first:${value.first},second:${value.second},list:${value.list}};})()`;
			}
			if (thing === first) return js`new Vector(${js`${first.x}`},${first.y})`;
			if (thing === second) return js`new Vector(${second.x},${second.y})`;
		});
	} catch (caught) {
		error = errorMessage(caught);
	}
	return { source, sawTag, error };
}

/** Shared initializers must run where fragment-local bindings are visible. */
function scopedSharedFragments(devalue) {
	const shared = { n: 11 };
	const nested = { n: 13 };
	const child = { n: 17 };
	const holder = { child };
	const scope = { shared, nested, holder };
	const value = [scope, shared];
	let source = null;
	let error = null;
	try {
		source = devalue.uneval(value, (thing, js) => {
			if (thing === scope) {
				return js`(()=>{let calls=0;class Local{constructor(n){this.n=n;calls++;}}return {list:[${shared},${shared}],nested:${js`(()=>{function build(n){return {n,kind:'nested'};}return [${nested},${nested}];})()`},holders:[${holder},${holder}],calls};})()`;
			}
			if (thing === shared) return js`new Local(${shared.n})`;
			if (thing === nested) return js`build(${nested.n})`;
			if (thing === holder) {
				return js`(()=>{class Inner{constructor(n){this.n=n;}}return [${child},${child}];})()`;
			}
			if (thing === child) return js`new Inner(${child.n})`;
		});
	} catch (caught) {
		error = errorMessage(caught);
	}
	return { source, error };
}

/**
 * Only a `js` fragment or the documented fallbacks (`undefined`, `null`,
 * `false`) may come back from a replacer.  Every other return value - notably
 * the 5.x string form - must be a TypeError, not a silently ignored result.
 *
 * @param {any} devalue
 */
function replacerResultValidation(devalue) {
	const invalid = ['new Date()', 1, true, {}, [], () => 'x', Symbol('s'), 1n];
	const rejections = invalid.map((fallback, index) => {
		let name = null;
		let message = null;
		try {
			devalue.uneval({ a: 1 }, () => fallback);
		} catch (caught) {
			name = typeof caught?.name === 'string' ? caught.name : typeof caught;
			message = errorMessage(caught);
		}
		return { index, type: typeof fallback, name, message };
	});
	const fallbacks = [undefined, null, false].map((fallback, index) => {
		let source = null;
		let error = null;
		try {
			source = devalue.uneval({ a: 1 }, () => fallback);
		} catch (caught) {
			error = errorMessage(caught);
		}
		return { index, label: String(fallback), source, error };
	});
	return { rejections, fallbacks };
}

/**
 * Identifiers written in tagged-template literals - including nested templates -
 * are reserved, so hoisted generated names cannot shadow fragment locals.
 *
 * @param {any} devalue
 */
function reservedTemplateIdentifiers(devalue) {
	const text = 'x'.repeat(300);
	const value = { one: text, two: text };
	let source = null;
	let error = null;
	try {
		source = devalue.uneval(value, (thing, js) => {
			if (thing !== value) return undefined;
			const nested = js`((c)=>{const keep='nested';return {value:${value.one},keep};})(0)`;
			return js`(()=>{const a='shadow',b='shadow';return {one:${nested},two:${value.two},total:a+b};})()`;
		});
	} catch (caught) {
		error = errorMessage(caught);
	}
	return { source, error };
}

/**
 * A replaced value that cycles back into a Map must be constructed once, after
 * the Map's earlier entries exist, so its construction-time snapshot and the
 * insertion order both survive.
 *
 * @param {any} devalue
 */
function cyclicCustomMapOrdering(devalue) {
	const map = new Map([['before', 42]]);
	const wrapper = { container: map };
	map.set('owner', wrapper);
	map.set('tail', 43);
	const value = [wrapper, map];
	let source = null;
	let error = null;
	try {
		source = devalue.uneval(value, (thing, js) => {
			if (thing === value) {
				return js`(()=>{const list=[${wrapper},${map}];return {list,calls:list[1].__dvCalls??0};})()`;
			}
			if (thing === wrapper) {
				return js`(()=>{class Wrapper{constructor(container){this.container=container;this.prefix=Array.from(container.keys());container.__dvCalls=(container.__dvCalls??0)+1;}}return new Wrapper(${wrapper.container});})()`;
			}
		});
	} catch (caught) {
		error = errorMessage(caught);
	}
	return { source, error };
}

/**
 * Every value in this graph occurs exactly once, so the source must place them
 * inline where they occur instead of lifting them into generated bindings.
 * The nested fragment also has to stay inside the enclosing fragment's scope.
 *
 * @param {any} devalue
 */
function singleOccurrenceInlining(devalue) {
	const value = { outer: { inner: { n: 1 }, tag: 'x' }, list: [1, 2, 3] };
	let source = null;
	let error = null;
	try {
		source = devalue.uneval(value, (thing, js) => {
			if (thing === value) {
				return js`{outer:${js`{inner:${value.outer.inner},tag:"x"}`},list:${value.list}}`;
			}
		});
	} catch (caught) {
		error = errorMessage(caught);
	}
	return { source, error };
}


/** Exercise view revivers using the candidate's own wire format. */
export function viewBackingOutcomes(devalue) {
	const outcomes = {};
	const bytes = Uint8Array.from({ length: 32 }, () => Math.floor(Math.random() * 256));
	const makeBuffer = (shared = false) => {
		const buffer = shared ? new SharedArrayBuffer(bytes.length) : new ArrayBuffer(bytes.length);
		new Uint8Array(buffer).set(bytes);
		return buffer;
	};
	const throws = (fn) => {
		try { fn(); return false; } catch { return true; }
	};
	for (const tag of ['Uint8Array', 'Int32Array', 'Float64Array', 'DataView']) {
		const Constructor = globalThis[tag];
		const stride = Constructor.BYTES_PER_ELEMENT ?? 1;
		for (const partial of [false, true]) {
			const offset = partial ? stride : 0;
			const length = partial ? 2 : bytes.length / stride;
			const original = new Constructor(makeBuffer(), offset, length);
			let serialized;
			try { serialized = devalue.stringify(original); } catch { serialized = null; }
			for (const method of ['parse', 'unflatten']) {
				const key = `${tag}_${partial ? 'partial' : 'full'}_${method}`;
				const revive = (backing) => {
					if (typeof serialized !== 'string') throw new Error('view serialization failed');
					const input = method === 'parse' ? serialized : JSON.parse(serialized);
					return devalue[method](input, { ArrayBuffer: () => backing });
				};
				for (const [label, invalid] of [
					['number', 4], ['array_like', { length: 4 }], ['plain_object', { buffer: makeBuffer() }]
				]) outcomes[`rejects_${label}_${key}`] = throws(() => revive(invalid));
				for (const shared of typeof SharedArrayBuffer === 'function' ? [false, true] : [false]) {
					let valid = false;
					try {
						const backing = makeBuffer(shared);
						const view = revive(backing);
						valid = view instanceof Constructor && view.buffer === backing &&
							view.byteOffset === original.byteOffset && view.byteLength === original.byteLength &&
							Array.from(new Uint8Array(view.buffer, view.byteOffset, view.byteLength))
								.every((byte, i) => byte === bytes[offset + i]);
					} catch {}
					outcomes[`valid_${shared ? 'shared' : 'array'}_${key}`] = valid;
				}
				try {
					const plain = devalue[method](method === 'parse' ? serialized : JSON.parse(serialized));
					outcomes[`default_${key}`] = plain instanceof Constructor &&
						plain.byteOffset === original.byteOffset && plain.byteLength === original.byteLength &&
						plain.buffer.byteLength === bytes.length &&
						Array.from(new Uint8Array(plain.buffer)).every((byte, i) => byte === bytes[i]);
				} catch { outcomes[`default_${key}`] = false; }
			}
		}
	}
	return outcomes;
}

function markedFailure(marker) {
	return Object.assign(new Error(marker), { marker });
}

/** Both the isolated protocol and direct harness use the same scenario values. */
export function asyncRejectionValue(scenario) {
	const rejected = (marker) => Promise.reject(markedFailure(marker));
	switch (scenario) {
		case 'symbol-sibling': return { p: rejected(scenario), bad: Symbol('bad') };
		case 'nested-function': return { p: rejected(scenario), nest: { deep: () => {} } };
		case 'two-rejections': return { first: rejected('first'), second: rejected('second') };
		case 'map-value': return { m: new Map([['k', rejected(scenario)]]), bad: Symbol('bad') };
		case 'array-element': return [rejected(scenario), () => {}];
		case 'late-rejection': return {
			p: Promise.resolve().then(() => { throw markedFailure(scenario); }), bad: Symbol('bad')
		};
		case 'symbol-before': return { bad: Symbol('bad'), p: rejected(scenario) };
		case 'array-before': return [() => {}, rejected(scenario)];
		case 'nested-before': return { bad: Symbol('bad'), nested: { p: rejected(scenario) } };
		case 'map-before': return { m: new Map([[Symbol('bad'), rejected(scenario)]]) };
		case 'late-before': return {
			bad: () => {}, p: new Promise((resolve, reject) => setTimeout(() => reject(markedFailure(scenario)), 20))
		};
		case 'getter-failure': return {
			p: rejected(scenario), get bad() { throw markedFailure('getter'); }
		};
		// A root reducer can throw before traversal reaches p. Create its
		// thenable on access so only a promise actually reached by the call is
		// subject to the rejection-handling contract. Once read, it still
		// rejects and must be handled even if the reducer subsequently fails.
		case 'reducer-failure': {
			let promise;
			return { get p() { return promise ??= rejected(scenario); } };
		}
		case 'solo-rejected': return { p: rejected('solo') };
		case 'solo-resolved': {
			const shared = { label: 'shared' };
			shared.self = shared;
			return { p: Promise.resolve(41), direct: shared,
				first: Promise.resolve(shared), second: Promise.resolve(shared),
				specials: [undefined, NaN, Infinity, -Infinity, -0].map((item) => Promise.resolve(item)) };
		}
		default: throw new Error(`unknown async rejection scenario: ${scenario}`);
	}
}

export async function captureAsyncOutcome(devalue, scenario) {
	const value = asyncRejectionValue(scenario);
	try {
		const reducers = scenario === 'reducer-failure'
			? { Broken: () => { throw markedFailure('reducer'); } } : undefined;
		const serialized = await devalue.stringifyAsync(value, reducers);
		let value_matches = true;
		if (scenario === 'solo-resolved') {
			const revived = devalue.parse(serialized);
			value_matches = revived.p === 41 && revived.first === revived.direct &&
				revived.second === revived.direct && revived.direct.self === revived.direct &&
				[undefined, NaN, Infinity, -Infinity, -0].every((item, i) => Object.is(item, revived.specials[i]));
		}
		return { status: 'resolved', value_matches };
	} catch (error) {
		return {
			status: 'rejected', name: error?.name ?? null,
			message: String(error?.message ?? error).slice(0, 200),
			marker: error?.marker ?? null, path: error?.path ?? null,
			devalue_error: error?.name === 'DevalueError' && typeof error?.path === 'string' &&
				'value' in error && 'root' in error,
			root_matches: error?.root === value,
			value_kind: typeof error?.value
		};
	}
}
