export const steps3 = Object.freeze([
	Object.freeze({ id: "a", label: "A" }),
	Object.freeze({ id: "b", label: "B" }),
	Object.freeze({ id: "c", label: "C" }),
]);

export const steps2 = Object.freeze([steps3[0], steps3[1]]);

export const emptySteps = Object.freeze([]);

export function makeSchema(validate) {
	return Object.freeze({
		"~standard": Object.freeze({
			version: 1,
			vendor: "p2p",
			validate,
		}),
	});
}

export const parsedSchema = makeSchema(() => ({ value: "parsed" }));

export const issuesSchema = makeSchema(() => ({ issues: [{ message: "bad" }] }));
