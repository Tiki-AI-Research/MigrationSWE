/**
 * Behaviour fixtures shared by the source and target runners.
 *
 * The matrix deliberately contains only public values.  It must not encode
 * source/target implementation details, otherwise a legitimate migration
 * would produce different observation values on the two sides.
 */

export const primitives = [
	undefined,
	null,
	true,
	false,
	0,
	-0,
	42,
	-7,
	1.5,
	NaN,
	Infinity,
	-Infinity,
	'',
	'plain',
	'quote " backslash \\ newline \n',
	'Symbol-like text'
];

export const structured = {
	label: 'matrix',
	count: 3,
	nested: { values: [1, 2, { deep: true }] },
	list: [undefined, NaN, -0, 9007199254740993n]
};

export function binaryValues() {
	const buffer = new Uint8Array([0, 1, 2, 127, 128, 254, 255]).buffer;
	const typed = new Uint16Array([0, 1, 65535]);
	const view = new DataView(new Uint8Array([9, 8, 7, 6]).buffer, 1, 2);
	return { buffer, typed, view };
}

export function customTypeRoundTrip(devalue) {
	if (typeof devalue.customTypeRoundTrip === 'function') {
		return devalue.customTypeRoundTrip();
	}
	class Foo {
		constructor(value) {
			this.value = value;
		}
	}

	const value = new Foo({ answer: 42 });
	const serialized = devalue.stringify(
		value,
		{ Foo: (candidate) => candidate instanceof Foo && candidate.value }
	);
	const restored = devalue.parse(serialized, {
		Foo: (payload) => new Foo(payload)
	});

	return restored instanceof Foo && restored.value.answer === 42;
}

export function temporalValue() {
	const Temporal = globalThis.Temporal;
	if (!Temporal) {
		throw new Error(
			'Temporal is required to observe the documented Temporal round-trip behaviour'
		);
	}
	return new Temporal.PlainDate(2024, 2, 29);
}
