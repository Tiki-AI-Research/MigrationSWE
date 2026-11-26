import { readdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
	assertCallableSurface,
	assertDeepEqual,
	assertDifferentReference,
	assertSameReference,
	assertUndefined,
	importFresh,
} from "../../shared/skeleton/assertions.mjs";
import { schemaSteps, steps, twoSteps } from "../../fixtures/skeleton/steps.mjs";

export const publicFunctions = [
	"createStepMap",
	"getInitialStepIndex",
	"parseStep",
	"getInitialData",
	"validateStep",
	"getStepStatus",
	"getStepStatuses",
	"matchStep",
];

export async function probeTarget(root = process.cwd()) {
	const coreDir = resolve(root, "packages/core");
	const manifest = JSON.parse(await readFile(resolve(coreDir, "package.json"), "utf8"));
	const nativeFiles = await readdir(resolve(coreDir, "native"));
	if (!nativeFiles.some((name) => name.endsWith(".node"))) {
		throw new Error("target package must contain a built native module");
	}
	if (!Array.isArray(manifest.files) || !manifest.files.some((entry) => entry.includes(".node"))) {
		throw new Error("target manifest must publish the native module");
	}

	const core = await importFresh(resolve(coreDir, "dist/index.js"));
	assertCallableSurface(core, publicFunctions);

	const map = core.createStepMap(steps);
	assertDeepEqual(map.ids, ["a", "b", "c"], "ids");
	assertSameReference(map.get("a"), steps[0], "get");
	assertSameReference(map.at(1), steps[1], "at");
	assertUndefined(map.at(-1), "at(-1)");
	assertUndefined(map.at(1.5), "at(1.5)");
	assertUndefined(map.at(Number.NaN), "at(NaN)");
	assertDeepEqual(map.neighbors("a"), { prev: undefined, next: steps[1] }, "neighbors(a)");
	assertDeepEqual(map.neighbors("b"), { prev: steps[0], next: steps[2] }, "neighbors(b)");
	assertDeepEqual(map.neighbors("c"), { prev: steps[1], next: undefined }, "neighbors(c)");

	assertDeepEqual(core.getInitialStepIndex(twoSteps, "b"), 1, "getInitialStepIndex");
	assertDeepEqual(core.parseStep(twoSteps, "b"), "b", "parseStep");
	assertUndefined(core.parseStep(twoSteps, null), "parseStep(null)");

	const data = { a: { saved: true } };
	const copy = core.getInitialData(data);
	assertDifferentReference(copy, data, "getInitialData");
	assertSameReference(copy.a, data.a, "getInitialData nested value");

	const plain = await core.validateStep(schemaSteps, "plain", 42);
	assertDeepEqual(plain, { success: true, data: 42 }, "validateStep plain");
	const parsed = await core.validateStep(schemaSteps, "async", "valid");
	assertDeepEqual(parsed, { success: true, data: "parsed" }, "validateStep async");
	const empty = await core.validateStep(schemaSteps, "empty", "x");
	assertDeepEqual(empty, { success: false, issues: [] }, "validateStep empty issues");

	assertDeepEqual(core.getStepStatus(steps, 1, "a"), "previous", "getStepStatus previous");
	assertDeepEqual(core.getStepStatuses(steps, 1), { a: "previous", b: "active", c: "upcoming" }, "getStepStatuses");

	const matched = core.matchStep(steps, "b", {
		a: (step) => step.label,
		b: (step) => step.label,
		c: (step) => step.label,
	});
	assertDeepEqual(matched, "B", "matchStep");

	return core;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	probeTarget(process.argv[2] ?? process.cwd()).catch((error) => {
		console.error(error);
		process.exitCode = 1;
	});
}
