import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

let importSequence = 0;

export async function importFresh(file) {
	importSequence += 1;
	return import(`${pathToFileURL(file).href}?skeleton=${importSequence}`);
}

export function assertFunction(value, name) {
	assert.equal(typeof value, "function", `${name} must be a function`);
}

export function assertCallableSurface(namespace, names) {
	for (const name of names) {
		assertFunction(namespace[name], name);
	}
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

export async function assertResolves(operation, expected, label = "operation") {
	const actual = await operation;
	assert.deepEqual(actual, expected, `${label} resolved value mismatch`);
	return actual;
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
