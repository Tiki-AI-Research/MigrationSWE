import assert from "node:assert/strict";

export class InfrastructureError extends Error {
	constructor(message, cause) {
		super(message, { cause });
		this.name = "InfrastructureError";
	}
}

export function assertFunction(value, name) {
	assert.equal(typeof value, "function", `${name} must be a function`);
}

export function assertDeepEqual(actual, expected, label = "value") {
	assert.deepEqual(actual, expected, `${label} mismatch`);
}

export function assertUndefined(value, label = "value") {
	assert.equal(value, undefined, `${label} must be undefined`);
}

export function assertSameReference(actual, expected, label = "value") {
	assert.equal(actual, expected, `${label} must preserve object identity`);
}

export function assertDifferentReference(actual, expected, label = "value") {
	assert.notEqual(actual, expected, `${label} must be a defensive copy`);
}

export async function assertRejectsWith(operation, pattern, label = "operation") {
	let error;
	try {
		await operation;
	} catch (caught) {
		error = caught;
	}
	assert.ok(error instanceof Error, `${label} must reject with an Error`);
	if (pattern) {
		assert.match(error.message, pattern, `${label} rejection message mismatch`);
	}
	return error;
}

export function assertThrowsWith(operation, pattern, label = "operation") {
	let error;
	try {
		operation();
	} catch (caught) {
		error = caught;
	}
	assert.ok(error instanceof Error, `${label} must throw an Error`);
	if (pattern) {
		assert.match(error.message, pattern, `${label} rejection message mismatch`);
	}
	return error;
}
