import { assertPublicTypeContract } from "../../type-contract.mjs";
import { coreRuntimeEntry } from "../../package-entry.mjs";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import {
	assertDeepEqual,
	assertDifferentReference,
	assertRejectsWith,
	assertSameReference,
	assertThrowsWith,
	assertUndefined,
	InfrastructureError,
} from "./assertions.mjs";
import {
	asyncRejectingSchema,
	duplicateSteps,
	emptyIssuesSchema,
	emptySteps,
	makeSchema,
	passThroughSchema,
	receiverSchema,
	rejectingSchema,
	schemaSteps,
	steps,
	twoSteps,
} from "../../../fixtures/batches/BATCH-F2P-0001/steps.mjs";
import { copyPublishedCoreFiles, decodeStringConstants, rustBuildOnly, discoverAddons, discoverCrate, toolchainPinsRust185, discoverRuntimeArtifacts, rustRuntime, importRustCore, assertRustBuildWiring, assertFailsWithoutRust, assertPublishedPackageRunsNative, productionSourceFiles } from "../../native-layout.mjs";

export const observationIds = [
	"F2P-001",
	"F2P-002",
	"F2P-004",
	"F2P-005",
	"F2P-006",
	"F2P-008",
	"F2P-010",
	"F2P-013",
	"F2P-017",
	"F2P-019",
	"F2P-016",
	"F2P-003",
	"F2P-007",
	"F2P-009",
	"F2P-011",
	"F2P-012",
	"F2P-014",
	"F2P-015",
	"F2P-018",
	"F2P-020",
	"F2P-021",
	"F2P-022",
	"F2P-023",
	"F2P-024",
];

class SemanticFailure extends Error {
	constructor(message) {
		super(message);
		this.name = "SemanticFailure";
	}
}

const requireFromChecks = createRequire(import.meta.url);
let importSequence = 0;

function coreDir(root) {
	return resolve(root, "packages/core");
}

async function readJson(file) {
	return JSON.parse(await readFile(file, "utf8"));
}

function findNativeModule(baseDir) {
	const base = resolve(baseDir);
	for (const [file, module] of Object.entries(requireFromChecks.cache)) {
		if (file.endsWith(".node") && resolve(file).startsWith(base)) {
			return { path: file, exports: module.exports };
		}
	}
	return undefined;
}

function findLoadedNative(baseDir) {
	const base = resolve(baseDir);
	const candidates = Object.keys(requireFromChecks.cache).filter((file) => file.endsWith(".node"));
	const scoped = candidates.find((file) => resolve(file).startsWith(base));
	const selected = scoped ?? candidates[candidates.length - 1];
	if (!selected) {
		return undefined;
	}
	return { path: selected, exports: requireFromChecks.cache[selected].exports };
}

async function loadCore(root, tag) {
 const runtime = rustRuntime(root);
 const core = await importRustCore(root);
 return { root, core, runtime, dist: coreRuntimeEntry(root) };
}

async function withCore(root, tag, nativeNames, body) {
 const info = await loadCore(root, tag);
 info.runtime.reinstall();
 try {
  const result = await body(info);
  assert.ok(info.runtime.nativeCodeCount > 0, "the package must load compiled Rust functions");
  assert.ok(info.runtime.total() > 0, "the public operation must execute compiled Rust");
  return result;
 } finally { info.runtime.restore(); }
}

async function listFiles(directory) {
	const entries = await readdir(directory, { withFileTypes: true });
	const files = [];
	for (const entry of entries) {
		const file = join(directory, entry.name);
		if (entry.isDirectory()) {
			files.push(...(await listFiles(file)));
		} else if (entry.isFile()) {
			files.push(file);
		}
	}
	return files;
}

async function assertNoDelegationFallback(root) {
	const productionFiles = productionSourceFiles(root);
	const forbidden = [
		"child_process",
		"worker_threads",
		"spawnSync",
		"execSync",
		"execFileSync",
		"fork(",
		"new Worker(",
	];
	for (const file of productionFiles) {
		const content = await readFile(file, "utf8");
		const folded = decodeStringConstants(content);
		if (rustBuildOnly(content)) continue;
		for (const token of forbidden) {
			assert.ok(
				!content.includes(token),
				`production file ${file} must not delegate through ${token}`,
			);
			assert.ok(
				!folded.some((value) => value.includes(token)),
				`production file ${file} must not reconstruct ${token} from string fragments`,
			);
		}
	}
}

async function assertFailsWithoutNative(root, tag) {
 await assertFailsWithoutRust(root);
}

async function assertRustNativeBuild(root, info) {
	const directory = coreDir(root);
	const manifest = await readJson(join(directory, "package.json"));
	assert.equal(manifest.type, "module", "core package must remain an ESM package");
	assertRustBuildWiring(root);
	assert.ok(info.runtime.nativeCodeCount > 0, "the loaded runtime must expose compiled Rust functions");

	const map = info.core.createStepMap(twoSteps);
	assertSameReference(map.next("a"), twoSteps[1], "next from the native artifact");
	await assertFailsWithoutNative(root, "rust-build");
}

async function copyPublishedFiles(directory, destination) {
	return copyPublishedCoreFiles(directory, destination);
}

async function importPackedPackage(root, tag) {
	const directory = coreDir(root);
	const temp = await mkdtemp(join(tmpdir(), "f2p-0001-consumer-"));
	const packageDestination = join(temp, "node_modules", "@stepperize/core");
	const manifest = await copyPublishedFiles(directory, packageDestination);
	const consumer = join(temp, "consumer.mjs");
	await writeFile(consumer, "export {};\n");
	const requireFromConsumer = createRequire(pathToFileURL(consumer));
	const entry = requireFromConsumer.resolve("@stepperize/core");
	importSequence += 1;
	const core = await import(`${pathToFileURL(entry).href}?packed=${tag}-${importSequence}`);
	return { temp, core, manifest, packageDestination };
}

async function assertPackedConsumer(root, tag) {
 await assertPublishedPackageRunsNative(root, tag);
}

async function assertTypeContract(root) {
	return assertPublicTypeContract(root);
}

async function assertDuplicateSemantics(core, includeMatchFailure) {
	const map = core.createStepMap(duplicateSteps);
	assertSameReference(map.get("a"), duplicateSteps[0], "duplicate get");
	assertDeepEqual(map.indexOf("a"), 0, "duplicate indexOf");
	assertSameReference(map.next("a"), duplicateSteps[1], "duplicate next");
	if (!includeMatchFailure) {
		return;
	}
	const calls = [];
	assertThrowsWith(
		() =>
			core.matchStep(steps, "missing", {
				a: () => calls.push("a"),
				b: () => calls.push("b"),
				c: () => calls.push("c"),
			}),
		/not found/,
		"matchStep missing",
	);
	assertDeepEqual(calls, [], "matchStep must not invoke any handler when the id is unknown");
	assertThrowsWith(
		() => core.matchStep(steps, "b", { a: () => calls.push("a") }),
		/No match handler/,
		"matchStep missing handler",
	);
	assertDeepEqual(calls, [], "matchStep must not invoke another handler when the selected handler is absent");
}

async function assertAtBoundaries(core) {
	const map = core.createStepMap(twoSteps);
	assertSameReference(map.at(1), twoSteps[1], "at(1)");
	for (const index of [-1, 1.5, Number.NaN, 2]) {
		assertUndefined(map.at(index), `at(${String(index)})`);
	}
}

async function assertStepIdentity(core) {
	const map = core.createStepMap(twoSteps);
	assertSameReference(map.get("a"), twoSteps[0], "get identity");
	assertSameReference(map.first(), twoSteps[0], "first identity");
	assertSameReference(map.next("a"), twoSteps[1], "next identity");
	assert.equal(map.has("a"), true, "has(a)");
	assert.equal(map.has("missing"), false, "has(missing)");
}

async function assertBoomRejection(core) {
	await assertRejectsWith(
		core.validateStep(schemaSteps, "async-reject", "x"),
		/boom/,
		"validateStep rejected schema",
	);
}

async function assertReceiverSchema(core) {
	const result = await core.validateStep(schemaSteps, "receiver", "ok");
	assertDeepEqual(result, { success: true, data: "ok" }, "validateStep receiver schema");

	const standard = receiverSchema["~standard"];
	const detached = standard.validate;
	let detachedFailed = false;
	try {
		const detachedResult = detached.call(undefined, "ok");
		detachedFailed = Boolean(detachedResult && detachedResult.issues);
	} catch {
		detachedFailed = true;
	}
	assert.ok(detachedFailed, "detached validate must fail without the ~standard receiver");
}

async function assertEmptyIssues(core) {
	const result = await core.validateStep(schemaSteps, "empty", "x");
	assertDeepEqual(result, { success: false, issues: [] }, "validateStep empty issues");
}

const checks = {
	"F2P-001": (root) =>
		withCore(root, "001", ["stepGet", "stepIndexOf", "stepNext", "matchStep"], async ({ core }) => {
			await assertDuplicateSemantics(core, true);
		}),
	"F2P-002": (root) =>
		withCore(root, "002", ["stepAt", "stepPrev"], async ({ core }) => {
			await assertAtBoundaries(core);
			const map = core.createStepMap(twoSteps);
			assertSameReference(map.prev("b"), twoSteps[0], "prev(b)");
			assertUndefined(map.prev("a"), "prev(a)");
			assertUndefined(map.prev("missing"), "prev(missing)");
		}),
	"F2P-004": (root) =>
		withCore(root, "004", ["validateStepPrepare", "validateStepFinish", "stepIndexOf"], async ({ core }) => {
			await assertReceiverSchema(core);
			const map = core.createStepMap(twoSteps);
			assertDeepEqual(map.indexOf("b"), 1, "indexOf(b)");
			assertDeepEqual(map.indexOf("missing"), -1, "indexOf(missing)");
		}),
	"F2P-005": (root) =>
		withCore(root, "005", ["stepGet", "stepFirst", "stepNext", "stepHas"], async ({ core }) => {
			await assertStepIdentity(core);
		}),
	"F2P-006": (root) =>
		withCore(root, "006", ["validateStepPrepare", "stepFirst", "stepLast"], async ({ core }) => {
			await assertBoomRejection(core);
			const map = core.createStepMap(twoSteps);
			assertSameReference(map.first(), twoSteps[0], "first()");
			assertSameReference(map.last(), twoSteps[1], "last()");
			const empty = core.createStepMap(emptySteps);
			assertUndefined(empty.first(), "empty first()");
			assertUndefined(empty.last(), "empty last()");
		}),
	"F2P-008": (root) =>
		withCore(root, "008", ["stepNext"], async (info) => {
			await assertRustNativeBuild(root, info);
		}),
	"F2P-010": (root) =>
		withCore(root, "010", ["stepIds"], async ({ core }) => {
			const required = [
				"packages/core/src/index.ts",
				"packages/core/src/types.ts",
				"packages/core/package.json",
				"packages/core/tsconfig.json",
			];
			for (const path of required) {
				assert.ok(existsSync(resolve(root, path)), `${path} must exist under the repository root`);
			}
			assert.ok(discoverCrate(root), "a Rust crate with sources must exist inside packages/core");
			assert.ok(toolchainPinsRust185(root), "the Rust toolchain must be pinned to the 1.85 line");
			assert.ok(discoverRuntimeArtifacts(root).length > 0, "the package must contain a built native addon");
			assertDeepEqual(core.createStepMap([{ id: "a" }]).ids, ["a"], "native ids");
			await assertPackedConsumer(root, "010");
		}),
	"F2P-013": (root) =>
		withCore(root, "013", ["validateStepPrepare", "validateStepFinish", "stepIndexOf"], async ({ core }) => {
			await assertEmptyIssues(core);
			const map = core.createStepMap(twoSteps);
			assertDeepEqual(map.indexOf("b"), 1, "indexOf(b)");
			assertDeepEqual(map.indexOf("missing"), -1, "indexOf(missing)");
		}),
	"F2P-017": (root) =>
		withCore(root, "017", ["stepIds"], async ({ core }) => {
			const map = core.createStepMap([{ id: "a" }]);
			assertDeepEqual(map.ids, ["a"], "synchronous ids");
			await assertTypeContract(root);
		}),
	"F2P-019": (root) =>
		withCore(root, "019", ["stepIds"], async ({ core }) => {
			const map = core.createStepMap([{ id: "a" }]);
			assertDeepEqual(map.ids, ["a"], "ids without indirection");
			await assertNoDelegationFallback(root);
		}),
	"F2P-016": (root) =>
		withCore(root, "016", ["validateStepPrepare", "getStepStatus"], async ({ core }) => {
			await assertBoomRejection(core);
			assertDeepEqual(core.getStepStatus(steps, 1, "a"), "previous", "status previous");
			assertDeepEqual(core.getStepStatus(steps, 1, "b"), "active", "status active");
			assertDeepEqual(core.getStepStatus(steps, 1, "c"), "upcoming", "status upcoming");
		}),
	"F2P-003": (root) =>
		withCore(root, "003", ["stepGet", "stepIndexOf", "stepNext", "getInitialData"], async ({ core }) => {
			await assertDuplicateSemantics(core, false);
			const empty = core.getInitialData();
			assertDeepEqual(empty, {}, "getInitialData()");
			const sourceData = { a: 1, b: 2 };
			const copy = core.getInitialData(sourceData);
			assertDeepEqual(copy, sourceData, "getInitialData copy");
			assertDifferentReference(copy, sourceData, "getInitialData defensive copy");
		}),
	"F2P-007": (root) =>
		withCore(root, "007", ["stepAt", "getInitialData"], async ({ core }) => {
			await assertAtBoundaries(core);
			const sourceData = { a: 1, b: 2 };
			const copy = core.getInitialData(sourceData);
			assertDeepEqual(copy, sourceData, "getInitialData copy");
			assertDifferentReference(copy, sourceData, "getInitialData defensive copy");
		}),
	"F2P-009": (root) =>
		withCore(root, "009", ["stepNext", "matchStep"], async (info) => {
			await assertRustNativeBuild(root, info);
			const calls = [];
			const result = info.core.matchStep(twoSteps, "b", {
				a: (step) => {
					calls.push("a");
					return step.label;
				},
				b: (step) => {
					calls.push("b");
					return step.label;
				},
			});
			assertDeepEqual(result, "B", "matchStep dispatch");
			assertDeepEqual(calls, ["b"], "matchStep must invoke only the selected handler");
		}),
	"F2P-011": (root) =>
		withCore(root, "011", ["validateStepPrepare", "validateStepFinish", "getStepStatus"], async ({ core }) => {
			await assertReceiverSchema(core);
			assertDeepEqual(core.getStepStatus(steps, 1, "a"), "previous", "status previous");
			assertDeepEqual(core.getStepStatus(steps, 1, "b"), "active", "status active");
			assertDeepEqual(core.getStepStatus(steps, 1, "c"), "upcoming", "status upcoming");
		}),
	"F2P-012": (root) =>
		withCore(root, "012", ["validateStepPrepare", "validateStepFinish"], async ({ core }) => {
			await assertReceiverSchema(core);
			const passed = await core.validateStep([{ id: "pass", schema: passThroughSchema }], "pass", "value");
			assertDeepEqual(passed, { success: true, data: "value" }, "validateStep pass-through schema");
			const rejected = await core.validateStep([{ id: "reject", schema: rejectingSchema }], "reject", "x");
			assertDeepEqual(rejected, { success: false, issues: [{ message: "Required" }] }, "validateStep rejecting schema");
		}),
	"F2P-014": (root) =>
		withCore(root, "014", ["validateStepPrepare", "parseStep"], async ({ core }) => {
			await assertBoomRejection(core);
			assertDeepEqual(core.parseStep(twoSteps, "b"), "b", "parseStep(b)");
			for (const value of ["missing", null, undefined, 42]) {
				assertUndefined(core.parseStep(twoSteps, value), `parseStep(${String(value)})`);
			}
		}),
	"F2P-015": (root) =>
		withCore(root, "015", ["stepAt", "stepIds"], async ({ core }) => {
			await assertAtBoundaries(core);
			const map = core.createStepMap(steps);
			assertSameReference(map.steps, steps, "steps order");
			assertDeepEqual(map.ids, ["a", "b", "c"], "ids order");
		}),
	"F2P-018": (root) =>
		withCore(root, "018", ["stepIds"], async ({ core }) => {
			const map = core.createStepMap([{ id: "a" }]);
			assertDeepEqual(map.ids, ["a"], "synchronous ids");
			await assertTypeContract(root);
			const manifest = await readJson(resolve(coreDir(root), "package.json"));
			assert.equal(manifest.type, "module", "core package must remain an ESM package");
		}),
	"F2P-020": (root) =>
		withCore(root, "020", ["stepNext"], async (info) => {
			await assertRustNativeBuild(root, info);
			assert.ok(
				!existsSync(resolve(root, "packages/react/native")),
				"Rust native files must remain inside packages/core",
			);
			const crate = discoverCrate(root);
			assert.ok(
				crate && crate.directory.startsWith(resolve(root, "packages/core")),
				"the Rust crate must live inside packages/core",
			);
		}),
	"F2P-021": (root) =>
		withCore(
			root,
			"021",
			["validateStepPrepare", "stepIds", "stepGet", "stepFirst", "stepNext", "getStepStatuses", "matchStep"],
			async ({ core }) => {
				await assertBoomRejection(core);
				const map = core.createStepMap(steps);
				assertDeepEqual(map.ids, ["a", "b", "c"], "ids");
				assertSameReference(map.get("a"), steps[0], "get");
				assertSameReference(map.first(), steps[0], "first");
				assertSameReference(map.next("a"), steps[1], "next");
				assertDeepEqual(core.getStepStatuses(steps, 1), { a: "previous", b: "active", c: "upcoming" }, "statuses");
				assertDeepEqual(
					core.matchStep(steps, "b", {
						a: (step) => step.label,
						b: (step) => step.label,
						c: (step) => step.label,
					}),
					"B",
					"matchStep",
				);
			},
		),
	"F2P-022": (root) =>
		withCore(root, "022", ["stepGet", "stepFirst", "stepNext", "matchStep"], async ({ core }) => {
			await assertStepIdentity(core);
			const calls = [];
			const result = core.matchStep(twoSteps, "b", {
				a: (step) => {
					calls.push("a");
					return step.label;
				},
				b: (step) => {
					calls.push("b");
					return step.label;
				},
			});
			assertDeepEqual(result, "B", "matchStep dispatch");
			assertDeepEqual(calls, ["b"], "matchStep must invoke only the selected handler");
		}),
	"F2P-023": (root) =>
		withCore(root, "023", ["stepGet", "stepIndexOf", "stepNext", "matchStep"], async ({ core }) => {
			await assertDuplicateSemantics(core, true);
		}),
	"F2P-024": (root) =>
		withCore(root, "024", ["validateStepPrepare", "validateStepFinish"], async ({ core }) => {
			await assertEmptyIssues(core);
			await assertRejectsWith(core.validateStep([{ id: "a" }], "missing", null), /not found/, "validateStep unknown step");
		}),
};

export async function runObservations(root = process.cwd()) {
	const observations = [];
	for (const observationId of observationIds) {
		const check = checks[observationId];
		try {
			await check(root);
			observations.push({ observation_id: observationId, status: "PASS", detail: "target postcondition satisfied" });
		} catch (error) {
			const status = error instanceof InfrastructureError ? "INFRA_ERROR" : "FAIL";
			observations.push({
				observation_id: observationId,
				status,
				detail: error instanceof Error ? error.message : String(error),
			});
		}
	}
	return observations;
}

export async function cli(root = process.argv[2] ?? process.cwd()) {
	const observations = await runObservations(root);
	const result = { root: resolve(root), observations };
	console.log(JSON.stringify(result, null, 2));
	const failed = observations.filter((observation) => observation.status === "FAIL");
	const infra = observations.filter((observation) => observation.status === "INFRA_ERROR");
	return { failed, infra };
}
