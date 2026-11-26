/**
 * Small, dependency-free observation helpers for the malformed-payload batch.
 */

export function isDevalueError(error) {
	return Boolean(
		error &&
			typeof error === 'object' &&
			error.name === 'DevalueError' &&
			typeof error.message === 'string' &&
			typeof error.path === 'string' &&
			'value' in error &&
			'root' in error
	);
}

function canonical(value, seen = new Map()) {
	if (value === undefined) return { type: 'undefined' };
	if (typeof value === 'number') {
		if (Number.isNaN(value)) return { type: 'number', value: 'NaN' };
		if (value === Infinity) return { type: 'number', value: 'Infinity' };
		if (value === -Infinity) return { type: 'number', value: '-Infinity' };
		if (Object.is(value, -0)) return { type: 'number', value: '-0' };
		return { type: 'number', value };
	}
	if (typeof value === 'bigint') return { type: 'bigint', value: value.toString() };
	if (typeof value !== 'object' || value === null) {
		return { type: typeof value, value };
	}
	if (seen.has(value)) return { type: 'reference', id: seen.get(value) };
	seen.set(value, seen.size);
	if (value instanceof Date) {
		return { type: 'Date', value: value.toISOString() };
	}
	if (value instanceof RegExp) {
		return { type: 'RegExp', source: value.source, flags: value.flags };
	}
	if (value instanceof Map) {
		return {
			type: 'Map',
			value: [...value.entries()].map(([key, item]) => [
				canonical(key, seen),
				canonical(item, seen)
			])
		};
	}
	if (value instanceof Set) {
		return {
			type: 'Set',
			value: [...value.values()].map((item) => canonical(item, seen))
		};
	}
	if (value instanceof ArrayBuffer) {
		return { type: 'ArrayBuffer', value: [...new Uint8Array(value)] };
	}
	if (ArrayBuffer.isView(value)) {
		return {
			type: value.constructor.name,
			value: [...new Uint8Array(value.buffer, value.byteOffset, value.byteLength)]
		};
	}
	if (value instanceof URL) return { type: 'URL', value: value.href };
	if (value instanceof URLSearchParams) {
		return { type: 'URLSearchParams', value: [...value.entries()] };
	}
	if (globalThis.Temporal) {
		for (const name of ['Instant', 'PlainDate', 'PlainDateTime', 'PlainTime', 'ZonedDateTime']) {
			const Type = globalThis.Temporal[name];
			if (typeof Type === 'function' && value instanceof Type) {
				return { type: `Temporal.${name}`, value: value.toString() };
			}
		}
	}
	if (Array.isArray(value)) {
		return {
			type: 'Array',
			length: value.length,
			value: Object.keys(value).map((key) => [key, canonical(value[key], seen)])
		};
	}
	return {
		type: 'Object',
		value: Object.keys(value)
			.sort()
			.map((key) => [key, canonical(value[key], seen)])
	};
}

export function canonicalValue(value) {
	return canonical(value);
}

export function errorDetails(error) {
	if (!error || typeof error !== 'object') {
		return { name: null, message: String(error), path: null };
	}
	return {
		name: typeof error.name === 'string' ? error.name : null,
		message: typeof error.message === 'string' ? error.message : null,
		path: typeof error.path === 'string' ? error.path : null
	};
}

export function jsonPayload(serialized) {
	return JSON.parse(serialized);
}

export function equivalent(left, right) {
	return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
}

export function createObservations(devalue) {
	return {
		publicExports: () => {
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
		},
		malformedParse: (payload) => {
			try {
				devalue.parse(payload);
				return false;
			} catch {
				return true;
			}
		},
		malformedUnflatten: (value) => {
			try {
				devalue.unflatten(value);
				return false;
			} catch {
				return true;
			}
		}
	};
}
