/**
 * Public candidate protocol.
 *
 * This module only encodes values that cross the evaluator/candidate process
 * boundary.  It deliberately contains no fixtures, expected observations or
 * evaluator policy.
 */

import { types } from 'node:util';

const TYPED_ARRAYS = new Set([
	'Int8Array',
	'Uint8Array',
	'Uint8ClampedArray',
	'Int16Array',
	'Uint16Array',
	'Int32Array',
	'Uint32Array',
	'Float16Array',
	'Float32Array',
	'Float64Array',
	'BigInt64Array',
	'BigUint64Array'
]);

function numberValue(value) {
	if (Number.isNaN(value)) return { t: 'number', v: 'NaN' };
	if (value === Infinity) return { t: 'number', v: 'Infinity' };
	if (value === -Infinity) return { t: 'number', v: '-Infinity' };
	if (Object.is(value, -0)) return { t: 'number', v: '-0' };
	return { t: 'number', v: value };
}

function encodeBytes(bytes) {
	return [...bytes];
}

export function encode(value, seen = new Map()) {
	if (value === undefined) return { t: 'undefined' };
	if (value === null) return { t: 'null' };
	if (typeof value === 'number') return numberValue(value);
	if (typeof value === 'string' || typeof value === 'boolean') {
		return { t: typeof value, v: value };
	}
	if (typeof value === 'bigint') return { t: 'bigint', v: value.toString() };
	if (typeof value === 'symbol') {
		return { t: 'symbol', v: value.description ?? null };
	}
	if (typeof value === 'function') {
		return { t: 'function', v: value.name || '' };
	}
	if (typeof value !== 'object') {
		throw new TypeError(`cannot encode value of type ${typeof value}`);
	}
	if (seen.has(value)) return { t: 'ref', id: seen.get(value) };
	const id = seen.size;
	seen.set(value, id);
	if (types.isDate(value)) {
		return { t: 'Date', id, v: value.toISOString() };
	}
	if (types.isRegExp(value)) {
		return { t: 'RegExp', id, source: value.source, flags: value.flags };
	}
	if (types.isMap(value)) {
		return {
			t: 'Map',
			id,
			v: [...value.entries()].map(([key, item]) => [
				encode(key, seen),
				encode(item, seen)
			])
		};
	}
	if (types.isSet(value)) {
		return {
			t: 'Set',
			id,
			v: [...value.values()].map((item) => encode(item, seen))
		};
	}
	if (types.isArrayBuffer(value)) {
		return {
			t: 'ArrayBuffer',
			id,
			v: encodeBytes(new Uint8Array(value))
		};
	}
	if (typeof Buffer !== 'undefined' && Buffer.isBuffer(value)) {
		// A Node.js Buffer is a view onto an allocation pool that can also hold
		// unrelated bytes. Keep the exact backing store and view window so the
		// evaluator can tell implementations that copy the visible bytes apart
		// from ones that emit the whole pool.
		return {
			t: 'Buffer',
			id,
			v: encodeBytes(new Uint8Array(value.buffer)),
			offset: value.byteOffset,
			length: value.byteLength
		};
	}
	if (types.isArrayBufferView(value)) {
		const type = value.constructor?.name;
		if (types.isDataView(value)) {
			return {
				t: 'DataView',
				id,
				v: encodeBytes(
					new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
				)
			};
		}
		if (TYPED_ARRAYS.has(type)) {
			const bigintWords =
				type === 'BigInt64Array' || type === 'BigUint64Array';
			const items = bigintWords
				? [...value].map((item) => item.toString())
				: [...value];
			return { t: 'TypedArray', id, type, v: items };
		}
	}
	if (types.isBoxedPrimitive(value)) {
		// Boxed primitives have no own enumerable state, so the generic object
		// encoding would erase both the wrapper and its value.
		return { t: 'Boxed', id, v: encode(value.valueOf(), seen) };
	}
	if (Object.prototype.toString.call(value) === '[object URL]') {
		return { t: 'URL', id, v: value.href };
	}
	if (Object.prototype.toString.call(value) === '[object URLSearchParams]') {
		return { t: 'URLSearchParams', id, v: [...value.entries()] };
	}
	const Temporal = globalThis.Temporal;
	if (Temporal) {
		for (const name of [
			'Instant',
			'PlainDate',
			'PlainDateTime',
			'PlainTime',
			'ZonedDateTime',
			'Duration',
			'PlainYearMonth',
			'PlainMonthDay'
		]) {
			const Type = Temporal[name];
			if (
				typeof Type === 'function' &&
				(value instanceof Type ||
					value?.constructor?.name === name)
			) {
				return { t: `Temporal.${name}`, id, v: value.toString() };
			}
		}
	}
	if (types.isPromise(value)) {
		return { t: 'Promise', id };
	}
	if (Array.isArray(value)) {
		return {
			t: 'Array',
			id,
			length: value.length,
			v: Object.keys(value).map((key) => [key, encode(value[key], seen)])
		};
	}
	return {
		t: 'Object',
		id,
		v: Object.keys(value).map((key) => [key, encode(value[key], seen)])
	};
}

function decodeNumber(value) {
	switch (value) {
		case 'NaN':
			return NaN;
		case 'Infinity':
			return Infinity;
		case '-Infinity':
			return -Infinity;
		case '-0':
			return -0;
		default:
			return Number(value);
	}
}

function decodeTemporal(tag, value) {
	const Temporal = globalThis.Temporal;
	if (!Temporal) {
		throw new TypeError(`${tag} requires Temporal`);
	}
	const name = tag.slice('Temporal.'.length);
	const Type = Temporal[name];
	if (typeof Type !== 'function') {
		throw new TypeError(`${tag} is unavailable`);
	}
	return Type.from(value);
}

export function decode(encoded) {
	const references = [];

	function build(node) {
		const tag = node?.t;
		switch (tag) {
			case 'undefined':
				return undefined;
			case 'null':
				return null;
			case 'string':
			case 'boolean':
				return node.v;
			case 'number':
				return decodeNumber(node.v);
			case 'bigint':
				return BigInt(node.v);
			case 'symbol':
				return Symbol(node.v ?? undefined);
			case 'function': {
				const fn = function protocolFunction() {};
				return fn;
			}
			case 'ref':
				return references[Number(node.id)];
			case 'Date':
				return remember(node, new Date(node.v));
			case 'RegExp':
				return remember(node, new RegExp(node.source, node.flags));
			case 'Map': {
				const value = remember(node, new Map());
				for (const [key, item] of node.v) {
					value.set(build(key), build(item));
				}
				return value;
			}
			case 'Set': {
				const value = remember(node, new Set());
				for (const item of node.v) value.add(build(item));
				return value;
			}
			case 'ArrayBuffer': {
				const value = remember(node, new ArrayBuffer(node.v.length));
				new Uint8Array(value).set(node.v);
				return value;
			}
			case 'Buffer': {
				if (typeof Buffer !== 'function') {
					throw new TypeError('Buffer is unavailable');
				}
				const store = Uint8Array.from(node.v);
				const offset = Number(node.offset ?? 0);
				const length = Number(node.length ?? store.length);
				return remember(
					node,
					Buffer.from(store.buffer, offset, length)
				);
			}
			case 'DataView': {
				const bytes = Uint8Array.from(node.v);
				return remember(node, new DataView(bytes.buffer));
			}
			case 'TypedArray': {
				const items =
					node.type === 'BigInt64Array' || node.type === 'BigUint64Array'
						? node.v.map((item) => BigInt(item))
						: node.v;
				const Type = globalThis[node.type];
				if (typeof Type !== 'function') {
					throw new TypeError(`${node.type} is unavailable`);
				}
				return remember(node, new Type(items));
			}
			case 'URL':
				return remember(node, new URL(node.v));
			case 'URLSearchParams':
				return remember(node, new URLSearchParams(node.v));
			case 'Boxed':
				return remember(node, Object(build(node.v)));
			case 'Promise':
				return remember(node, Promise.resolve(1));
			case 'Array': {
				const value = remember(node, new Array(Number(node.length)));
				for (const [key, item] of node.v) value[key] = build(item);
				return value;
			}
			case 'Object': {
				const value = remember(node, {});
				for (const [key, item] of node.v) value[key] = build(item);
				return value;
			}
			default:
				if (typeof tag === 'string' && tag.startsWith('Temporal.')) {
					return remember(node, decodeTemporal(tag, node.v));
				}
				throw new TypeError(`unknown protocol value tag: ${String(tag)}`);
		}
	}

	function remember(node, value) {
		references[Number(node.id)] = value;
		return value;
	}

	return build(encoded);
}

