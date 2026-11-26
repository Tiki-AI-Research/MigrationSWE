import { assertPublicTypeContract } from "../../type-contract.mjs";
import { coreRuntimeEntry } from "../../package-entry.mjs";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { promisify } from "node:util";
import {
	emptySteps,
	issueSchema,
	labeledSteps,
	makeSchema,
	parsedSchema,
	schemaSteps,
	steps,
	twoSteps,
} from "../../../fixtures/batches/BATCH-DIFFERENTIAL-0001/steps.mjs";
import { countDisabledTestMarkers } from "../../native-layout.mjs";

const execFileAsync = promisify(execFile);

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

export const publicTypes = ["Step", "StepMap", "StepStatus", "ValidationResult", "StandardSchemaV1", "Get"];

function norm(value) {
	if (value === undefined) return "<undefined>";
	if (value === null) return null;
	if (Array.isArray(value)) return value.map(norm);
	if (typeof value === "object") {
		const result = {};
		for (const key of Object.keys(value).sort()) {
			result[key] = norm(value[key]);
		}
		return result;
	}
	return value;
}

function idOf(step) {
	return step === undefined ? "<undefined>" : step.id;
}

async function readText(path) {
	return readFile(path, "utf8");
}

async function declarationNames(root) {
	const checked = assertPublicTypeContract(root);
	return { exported: publicTypes.filter(name => checked.exported.includes(name)), declared: [...publicTypes] };
}

async function catchError(operation) {
	try {
		await operation();
		return undefined;
	} catch (error) {
		return error;
	}
}

function catchSync(operation) {
	try {
		operation();
		return undefined;
	} catch (error) {
		return error;
	}
}

export async function collectRuntimeObservations(core, root) {
	const result = {};

	result["DIFF-001"] = (() => {
		const map = core.createStepMap([{ id: "a" }]);
		return norm({
			createStepMapIsFunction: typeof core.createStepMap === "function",
			ids: map.ids,
		});
	})();

	result["DIFF-002"] = norm(
		Object.fromEntries(publicFunctions.map((name) => [name, typeof core[name] === "function"])),
	);

	result["DIFF-003"] = await (async () => {
		const pending = core.validateStep([{ id: "a" }], "a", { x: 1 });
		const thenable = typeof pending?.then === "function";
		const resolved = await pending;
		return norm({ thenable, result: resolved });
	})();

	result["DIFF-004"] = (() => {
		const map = core.createStepMap([{ id: "a" }]);
		return norm({
			stepsIsArray: Array.isArray(map.steps),
			idsIsArray: Array.isArray(map.ids),
			functions: [
				"get",
				"at",
				"indexOf",
				"has",
				"first",
				"last",
				"next",
				"prev",
				"neighbors",
			].filter((name) => typeof map[name] === "function"),
			valueTypes: { steps: typeof map.steps, ids: typeof map.ids },
		});
	})();

	result["DIFF-005"] = norm(await declarationNames(root));

	result["DIFF-006"] = await (async () => {
		const manifest = JSON.parse(await readText(resolve(root, "packages/core/package.json")));
		coreRuntimeEntry(root);
		assertPublicTypeContract(root);
		return norm({ type: manifest.type, rootImportResolves: true, rootTypesResolve: true });
	})();

	result["DIFF-007"] = (() => {
		const map = core.createStepMap(steps);
		return norm({ ids: map.ids, steps: map.steps.map((step) => step.id) });
	})();

	result["DIFF-008"] = (() => {
		const map = core.createStepMap(twoSteps);
		return norm({ b: idOf(map.get("b")), missing: idOf(map.get("missing")) });
	})();

	result["DIFF-009"] = (() => {
		const map = core.createStepMap(twoSteps);
		return norm({ at1: idOf(map.at(1)), at2: idOf(map.at(2)) });
	})();

	result["DIFF-010"] = (() => {
		const map = core.createStepMap(twoSteps);
		return norm({ b: map.indexOf("b"), missing: map.indexOf("missing") });
	})();

	result["DIFF-011"] = (() => {
		const map = core.createStepMap(twoSteps);
		return norm({ a: map.has("a"), missing: map.has("missing") });
	})();

	result["DIFF-012"] = (() => {
		const map = core.createStepMap(twoSteps);
		const empty = core.createStepMap(emptySteps);
		return norm({
			first: idOf(map.first()),
			last: idOf(map.last()),
			emptyFirst: idOf(empty.first()),
			emptyLast: idOf(empty.last()),
		});
	})();

	result["DIFF-013"] = (() => {
		const map = core.createStepMap(twoSteps);
		return norm({ a: idOf(map.next("a")), b: idOf(map.next("b")), missing: idOf(map.next("missing")) });
	})();

	result["DIFF-014"] = (() => {
		const map = core.createStepMap(twoSteps);
		return norm({ b: idOf(map.prev("b")), a: idOf(map.prev("a")), missing: idOf(map.prev("missing")) });
	})();

	result["DIFF-015"] = (() => {
		const map = core.createStepMap(steps);
		return norm({
			b: { prev: idOf(map.neighbors("b").prev), next: idOf(map.neighbors("b").next) },
			a: { prev: idOf(map.neighbors("a").prev), next: idOf(map.neighbors("a").next) },
			c: { prev: idOf(map.neighbors("c").prev), next: idOf(map.neighbors("c").next) },
			missing: {
				prev: idOf(map.neighbors("missing").prev),
				next: idOf(map.neighbors("missing").next),
			},
		});
	})();

	result["DIFF-016"] = norm({
		b: core.getInitialStepIndex(twoSteps, "b"),
		omitted: core.getInitialStepIndex(twoSteps),
		unknown: core.getInitialStepIndex(twoSteps, "missing"),
	});

	result["DIFF-017"] = norm({
		b: core.parseStep(twoSteps, "b"),
		missing: core.parseStep(twoSteps, "missing"),
		null: core.parseStep(twoSteps, null),
		undefined: core.parseStep(twoSteps, undefined),
		number: core.parseStep(twoSteps, 42),
	});

	result["DIFF-018"] = (() => {
		const empty = core.getInitialData();
		const data = { a: 1, b: 2 };
		const copy = core.getInitialData(data);
		return norm({
			emptyKeys: Object.keys(empty),
			entries: copy,
			sameReference: copy === data,
		});
	})();

	result["DIFF-019"] = norm({
		a: core.getStepStatus(steps, 1, "a"),
		b: core.getStepStatus(steps, 1, "b"),
		c: core.getStepStatus(steps, 1, "c"),
	});

	result["DIFF-020"] = norm(core.getStepStatuses(steps, 1));

	result["DIFF-021"] = await (async () => {
		const error = await catchError(() => core.validateStep([{ id: "a" }], "missing", null));
		return norm({
			isError: error instanceof Error,
			hasNotFound: error instanceof Error && /not found/.test(error.message),
		});
	})();

	result["DIFF-022"] = norm(await core.validateStep([{ id: "a" }], "a", { x: 1 }));

	result["DIFF-023"] = await (async () => {
		const parsed = await core.validateStep(schemaSteps, "parsed", "value");
		const issues = await core.validateStep(schemaSteps, "issues", "value");
		return norm({ parsed, issues });
	})();

	result["DIFF-024"] = (() => {
		const calls = [];
		const matched = core.matchStep(labeledSteps, "b", {
			a: (step) => {
				calls.push(`a:${step.id}`);
				return step.label;
			},
			b: (step) => {
				calls.push(`b:${step.id}`);
				return step.label;
			},
		});
		return norm({ calls, matched });
	})();

	result["DIFF-025"] = (() => {
		const missing = catchSync(() => core.matchStep(labeledSteps, "missing", {}));
		const noHandler = catchSync(() => core.matchStep(labeledSteps, "b", { a: (step) => step.id }));
		return norm({
			missingIsError: missing instanceof Error,
			missingHasNotFound: missing instanceof Error && /not found/.test(missing.message),
			noHandlerIsError: noHandler instanceof Error,
			noHandlerHasMessage: noHandler instanceof Error && /No match handler/.test(noHandler.message),
		});
	})();

	result["DIFF-026"] = (() => {
		const calls = [];
		const handlers = {
			a: () => calls.push("a"),
			b: () => calls.push("b"),
		};
		const missing = catchSync(() => core.matchStep(labeledSteps, "missing", handlers));
		const noHandler = catchSync(() => core.matchStep(labeledSteps, "b", { a: () => calls.push("a") }));
		return norm({
			missingThrew: missing instanceof Error,
			noHandlerThrew: noHandler instanceof Error,
			calls,
		});
	})();

	result["DIFF-027"] = await (async () => {
		const names = await declarationNames(root);
		const map = core.createStepMap(steps);
		const statuses = core.getStepStatuses(steps, 0);
		return norm({
			functions: publicFunctions.filter((name) => typeof core[name] === "function"),
			types: names.exported,
			mapIds: map.ids,
			statuses,
		});
	})();

	result["DIFF-032"] = await (async () => {
		const source = await readText(resolve(root, "packages/core/src/tests/utils.test.ts"));
		const markers = countDisabledTestMarkers(source);
		return norm({
			testFilePresent: source.length > 0,
			skipped: markers.skipped,
			focused: markers.focused,
			hasAssertions: /expect\s*\(/.test(source),
		});
	})();

	return result;
}

async function gitLines(root, args) {
	const { stdout } = await execFileAsync("git", args, { cwd: root });
	return stdout
		.split("\n")
		.map((line) => line.trim())
		.filter(Boolean);
}

const allowedRootConfig = new Set([
	"package.json",
	"pnpm-lock.yaml",
	"pnpm-workspace.yaml",
	"turbo.json",
	".npmrc",
	".gitignore",
	"biome.json",
]);

function inScope(path) {
	return path.startsWith("packages/core/") || allowedRootConfig.has(path);
}

export async function collectStructuralObservations(root, mode) {
	if (mode === "source") {
		return {
			"DIFF-028": norm({ outOfScope: [] }),
			"DIFF-029": await typesFingerprint(root),
			"DIFF-030": norm({ reactChanges: [] }),
			"DIFF-031": norm({ docsChanges: [] }),
		};
	}

	const [tracked, untracked] = await Promise.all([
		gitLines(root, ["diff", "--name-only", "HEAD"]),
		gitLines(root, ["ls-files", "--others", "--exclude-standard"]),
	]);
	const changed = [...new Set([...tracked, ...untracked])].sort();
	const outOfScope = changed.filter((path) => !inScope(path));
	const reactChanges = changed.filter((path) => path.startsWith("packages/react/"));
	const docsChanges = changed.filter((path) => path.startsWith("apps/docs/"));

	return {
		"DIFF-028": norm({ outOfScope }),
		"DIFF-029": await typesFingerprint(root),
		"DIFF-030": norm({ reactChanges }),
		"DIFF-031": norm({ docsChanges }),
	};
}

async function typesFingerprint(root) {
	const source = await readText(resolve(root, "packages/core/src/types.ts"));
	return norm({
		typesHash: createHash("sha256").update(source).digest("hex"),
		importsNative: /from\s+["']\.\/native["']/.test(source),
	});
}

export function makeSchemaFixture(validate) {
	return makeSchema(validate);
}

export { parsedSchema, issueSchema };
