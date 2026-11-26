export const steps = Object.freeze([
	Object.freeze({ id: "a", label: "A" }),
	Object.freeze({ id: "b", label: "B" }),
	Object.freeze({ id: "c", label: "C" }),
]);

export const twoSteps = Object.freeze([steps[0], steps[1]]);

export const emptySteps = Object.freeze([]);

export function makeSchema(validate, extra = {}) {
	return Object.freeze({
		"~standard": Object.freeze({
			version: 1,
			vendor: "differential",
			validate,
			...extra,
		}),
	});
}

export const parsedSchema = makeSchema(() => ({ value: "parsed" }));
export const issueSchema = makeSchema(() => ({ issues: [{ message: "bad" }] }));

export const schemaSteps = Object.freeze([
	Object.freeze({ id: "plain" }),
	Object.freeze({ id: "parsed", schema: parsedSchema }),
	Object.freeze({ id: "issues", schema: issueSchema }),
]);

export const labeledSteps = Object.freeze([
	Object.freeze({ id: "a", label: "A" }),
	Object.freeze({ id: "b", label: "B" }),
]);
