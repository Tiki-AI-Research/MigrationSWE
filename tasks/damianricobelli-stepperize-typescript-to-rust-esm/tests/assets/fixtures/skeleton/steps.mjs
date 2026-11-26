export const steps = Object.freeze([
	Object.freeze({ id: "a", label: "A" }),
	Object.freeze({ id: "b", label: "B" }),
	Object.freeze({ id: "c", label: "C" }),
]);

export const twoSteps = Object.freeze([steps[0], steps[1]]);

export const emptySteps = Object.freeze([]);

export const duplicateSteps = Object.freeze([
	Object.freeze({ id: "a", label: "first" }),
	Object.freeze({ id: "a", label: "second" }),
]);

export function makeSchema(validate, extra = {}) {
	return Object.freeze({
		"~standard": Object.freeze({
			version: 1,
			vendor: "skeleton",
			validate,
			...extra,
		}),
	});
}

export const passThroughSchema = makeSchema((value) => ({ value }));

export const rejectingSchema = makeSchema(() => ({ issues: [{ message: "Required" }] }));

export const emptyIssuesSchema = makeSchema(() => ({ issues: [] }));

export const asyncSchema = makeSchema(async (value) =>
	value === "valid" ? { value: "parsed" } : { issues: [{ message: "Invalid" }] },
);

export const asyncRejectingSchema = makeSchema(async () => {
	throw new Error("boom");
});

export const receiverSchema = makeSchema(function validate(value) {
	return value === this.expected ? { value } : { issues: [{ message: "Unexpected" }] };
}, { expected: "ok" });

export const schemaSteps = Object.freeze([
	Object.freeze({ id: "pass", schema: passThroughSchema }),
	Object.freeze({ id: "reject", schema: rejectingSchema }),
	Object.freeze({ id: "empty", schema: emptyIssuesSchema }),
	Object.freeze({ id: "async", schema: asyncSchema }),
	Object.freeze({ id: "async-reject", schema: asyncRejectingSchema }),
	Object.freeze({ id: "receiver", schema: receiverSchema }),
	Object.freeze({ id: "plain" }),
]);
