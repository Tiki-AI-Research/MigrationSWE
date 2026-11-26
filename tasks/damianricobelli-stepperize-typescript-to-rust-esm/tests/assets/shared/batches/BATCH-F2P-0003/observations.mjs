import { assertPublicTypeContract } from "../../type-contract.mjs";
import { coreRuntimeEntry } from "../../package-entry.mjs";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import {
	asyncRejectingSteps,
	duplicateSteps,
	emptySteps,
	makeProtoSymbolData,
	receiverSchema,
	receiverSteps,
	schemaSteps,
	steps,
	twoSteps,
} from "../../../fixtures/batches/BATCH-F2P-0003/cases.mjs";
import {
	assertPublishedPackageRunsNative,
	copyCorePackageWithoutNative,
	discoverCrate,
	toolchainPinsRust185,
	assertFailsWithoutRust, assertOperationDelegates,
} from "../../native-layout.mjs";
import { assertAllOperationsDelegate, withNative } from "./harness.mjs";

function assertNativeArtifact(env) {
	assert.ok(env.nativeFiles.length > 0 && env.binding.nativeCodeCount > 0, "target package must load compiled Rust");
	assert.ok(env.nativeFile && existsSync(env.nativeFile), "native artifact must exist on disk");
}

function assertPinnedRustToolchain(env) {
	assertNativeArtifact(env);
	assert.ok(discoverCrate(env.root), "a Rust crate with sources must exist inside packages/core");
	assert.ok(toolchainPinsRust185(env.root), "Rust toolchain must be pinned to the 1.85 line");
}

function assertChangeScope(env) {
	const gitDir = resolve(env.root, ".git");
	if (!existsSync(gitDir)) return;
	const result = spawnSync("git", ["-C", env.root, "diff", "--name-only", "HEAD"], { encoding: "utf8" });
	if (result.status !== 0) return;
	const allowed =
		/^(packages\/core\/|pnpm-lock\.yaml$|pnpm-workspace\.yaml$|package\.json$|turbo\.json$|\.gitignore$|\.cargo\/|vendor\/|apps\/docs\/\.source\/)/;
	for (const file of result.stdout.split("\n").filter(Boolean)) {
		assert.match(file, allowed, `unexpected repository change outside the migration scope: ${file}`);
	}
}

async function assertFailsWithoutNative(env) { await assertFailsWithoutRust(env.root); }

function assertProtoSymbolCopy(env) {
	const { data, key } = makeProtoSymbolData();
	const copy = env.core.getInitialData(data);
	assert.notEqual(copy, data, "getInitialData must return a defensive copy");
	assert.ok(Object.prototype.hasOwnProperty.call(copy, "__proto__"), "copy must keep an own __proto__ entry");
	assert.equal(Object.getPrototypeOf(copy), Object.prototype, "copy prototype must stay Object.prototype");
	assert.deepEqual(Object.getOwnPropertyDescriptor(copy, "__proto__")?.value, { polluted: true });
	assert.equal(copy[key], "symbol", "copy must retain symbol-keyed own entries");
}

function assertDuplicateSemantics(env) {
	const map = env.core.createStepMap(duplicateSteps);
	assert.equal(map.get("a"), duplicateSteps[0], "duplicate get must return the first step");
	assert.equal(map.indexOf("a"), 0, "duplicate indexOf must return the first index");
	assert.equal(map.next("a"), duplicateSteps[1], "duplicate next must advance past the first duplicate");
}

function assertCombinedCoreCalls(env) {
	const map = env.core.createStepMap(twoSteps);
	assert.equal(map.next("a"), steps[1], "next");
	assert.equal(env.core.parseStep(twoSteps, "b"), "b", "parseStep");
	return env.core.validateStep(twoSteps, "a", 42).then((result) => {
		assert.deepEqual(result, { success: true, data: 42 }, "validateStep");
		assert.equal(env.core.getStepStatus(twoSteps, 1, "a"), "previous", "getStepStatus");
		assert.equal(env.core.matchStep(twoSteps, "b", { b: (step) => step.id }), "b", "matchStep");
	});
}

export const observations = {
	"F2P-059": (env) =>
		withNative(env, ["stepGet", "stepIndexOf", "stepNext"], async () => {
			assertDuplicateSemantics(env);
		}),

	"F2P-072": (env) =>
		withNative(env, ["stepIds"], async () => {
			assertNativeArtifact(env);
			assert.deepEqual(env.core.createStepMap([{ id: "a" }]).ids, ["a"], "native ids");
			// Publication is verified behaviourally: exactly the files
			// `package.json.files` declares must let a consumer import the package
			// and execute the Rust addon (no `.node` literal / private path).
			await assertPublishedPackageRunsNative(env.root, "f2p-072");
		}),

	"F2P-049": (env) =>
		withNative(env, ["validateStepPrepare", "validateStepFinish"], async () => {
			const result = await env.core.validateStep(receiverSteps, "receiver", "ok");
			assert.deepEqual(result, { success: true, data: "ok" }, "receiver-bound schema result");
			const wrongReceiver = receiverSchema["~standard"].validate.call(undefined, "ok");
			assert.ok(wrongReceiver.issues && wrongReceiver.issues.length > 0, "schema must be receiver sensitive");
		}),

	"F2P-050": (env) =>
		withNative(env, ["getInitialData"], async () => {
			assertProtoSymbolCopy(env);
		}),

	"F2P-051": async (env) => {
		await withNative(env, ["stepNeighbors"], async () => {
			const map = env.core.createStepMap(steps);
			assert.deepEqual(map.neighbors("a"), { prev: undefined, next: steps[1] }, "neighbors(a)");
   await assertOperationDelegates(env.root, (core) => core.createStepMap(steps).neighbors("a"), "neighbors");
		});
		await assertAllOperationsDelegate(env);
	},

	"F2P-052": (env) =>
		withNative(env, ["stepIndexOf", "getInitialData"], async () => {
			const map = env.core.createStepMap(twoSteps);
			assert.equal(map.indexOf("b"), 1, "indexOf(b)");
			assert.equal(map.indexOf("missing"), -1, "indexOf(missing)");
			assertProtoSymbolCopy(env);
		}),

	"F2P-053": (env) =>
		withNative(env, ["getInitialStepIndex", "stepGet", "stepIndexOf", "stepNext"], async () => {
			assert.equal(env.core.getInitialStepIndex(twoSteps, "b"), 1, "getInitialStepIndex(b)");
			assert.equal(env.core.getInitialStepIndex(twoSteps), 0, "getInitialStepIndex()");
			assert.equal(env.core.getInitialStepIndex(twoSteps, "missing"), 0, "getInitialStepIndex(missing)");
			assertDuplicateSemantics(env);
		}),

	"F2P-054": (env) =>
		withNative(
			env,
			["getInitialData", "stepNext", "parseStep", "validateStepPrepare", "getStepStatus", "matchStep"],
			async () => {
				assert.deepEqual(env.core.getInitialData(), {}, "getInitialData()");
				const data = { a: 1, b: 2 };
				const copy = env.core.getInitialData(data);
				assert.deepEqual(copy, data, "getInitialData entries");
				assert.notEqual(copy, data, "getInitialData reference");
				await assertCombinedCoreCalls(env);
			},
		),

	"F2P-055": (env) =>
		withNative(env, ["validateStepPrepare", "validateStepFinish"], async () => {
			const plain = await env.core.validateStep(schemaSteps, "plain", { anything: true });
			assert.deepEqual(plain, { success: true, data: { anything: true } }, "schema-less validateStep");
			const receiver = await env.core.validateStep(receiverSteps, "receiver", "ok");
			assert.deepEqual(receiver, { success: true, data: "ok" }, "receiver validateStep");
		}),

	"F2P-056": (env) =>
		withNative(env, ["stepIds"], async () => {
			assertPinnedRustToolchain(env);
			assert.equal(typeof env.core.createStepMap, "function", "createStepMap export");
			assert.deepEqual(env.core.createStepMap([{ id: "a" }]).ids, ["a"], "synchronous named export");
			assertPublicTypeContract(env.root);
		}),

	"F2P-057": (env) =>
		withNative(env, ["stepAt", "validateStepPrepare"], async () => {
			const map = env.core.createStepMap(twoSteps);
			assert.equal(map.at(1), steps[1], "at(1)");
			assert.equal(map.at(-1), undefined, "at(-1)");
			assert.equal(map.at(1.5), undefined, "at(1.5)");
			assert.equal(map.at(Number.NaN), undefined, "at(NaN)");
			assert.equal(map.at(2), undefined, "at(2)");
			const result = await env.core.validateStep([{ id: "a" }], "a", { x: 1 });
			assert.deepEqual(result, { success: true, data: { x: 1 } }, "schema-less validateStep");
		}),

	"F2P-058": (env) =>
		withNative(env, ["validateStepPrepare"], async () => {
			assertPinnedRustToolchain(env);
			let error;
			try {
				await env.core.validateStep([{ id: "a" }], "missing", null);
			} catch (caught) {
				error = caught;
			}
			assert.ok(error instanceof Error, "unknown step must reject with an Error");
			assert.match(error.message, /not found/, "unknown step message");
			await assertFailsWithoutNative(env);
		}),

	"F2P-060": (env) =>
		withNative(env, ["getInitialStepIndex", "validateStepPrepare"], async () => {
			assert.equal(env.core.getInitialStepIndex(twoSteps, "b"), 1, "getInitialStepIndex(b)");
			assert.equal(env.core.getInitialStepIndex(twoSteps), 0, "getInitialStepIndex()");
			assert.equal(env.core.getInitialStepIndex(twoSteps, "missing"), 0, "getInitialStepIndex(missing)");
			let error;
			try {
				await env.core.validateStep(asyncRejectingSteps, "async-reject", "x");
			} catch (caught) {
				error = caught;
			}
			assert.ok(error instanceof Error, "rejected schema must reject with an Error");
			assert.equal(error.message, "boom", "rejected schema message");
		}),

	"F2P-061": (env) =>
		withNative(env, ["stepIds", "stepNext"], async () => {
			assert.deepEqual(env.core.createStepMap([{ id: "a" }]).ids, ["a"], "synchronous ids");
			const map = env.core.createStepMap(twoSteps);
			assert.equal(map.next("a"), steps[1], "next(a)");
			assert.equal(map.next("b"), undefined, "next(b)");
			assert.equal(map.next("missing"), undefined, "next(missing)");
			assertPublicTypeContract(env.root);
		}),

	"F2P-062": (env) =>
		withNative(env, ["matchStep", "stepGet", "stepIndexOf", "stepNext"], async () => {
			const result = env.core.matchStep(steps, "b", {
				a: (step) => step.label,
				b: (step) => step.label,
				c: (step) => step.label,
			});
			assert.equal(result, "B", "matchStep handler result");
			assertDuplicateSemantics(env);
		}),

	"F2P-063": (env) =>
		withNative(env, ["stepGet", "getInitialData"], async () => {
			const map = env.core.createStepMap(twoSteps);
			assert.equal(map.get("b"), steps[1], "get(b)");
			assert.equal(map.get("missing"), undefined, "get(missing)");
			assertProtoSymbolCopy(env);
		}),

	"F2P-064": (env) =>
		withNative(env, ["stepFirst", "stepLast", "getInitialData"], async () => {
			const map = env.core.createStepMap(twoSteps);
			assert.equal(map.first(), steps[0], "first");
			assert.equal(map.last(), steps[1], "last");
			const empty = env.core.createStepMap(emptySteps);
			assert.equal(empty.first(), undefined, "empty first");
			assert.equal(empty.last(), undefined, "empty last");
			assertProtoSymbolCopy(env);
		}),

	"F2P-065": (env) =>
		withNative(env, ["validateStepPrepare", "validateStepFinish", "getInitialData"], async () => {
			const result = await env.core.validateStep(schemaSteps, "async", "valid");
			assert.deepEqual(result, { success: true, data: "parsed" }, "parsed schema result");
			assertProtoSymbolCopy(env);
		}),

	"F2P-066": (env) =>
		withNative(
			env,
			["getStepStatus", "getStepStatuses", "stepGet", "stepIndexOf", "stepNext"],
			async () => {
				assert.equal(env.core.getStepStatus(steps, 1, "a"), "previous", "previous status");
				assert.equal(env.core.getStepStatus(steps, 1, "b"), "active", "active status");
				assert.equal(env.core.getStepStatus(steps, 1, "c"), "upcoming", "upcoming status");
				assert.deepEqual(
					env.core.getStepStatuses(steps, 1),
					{ a: "previous", b: "active", c: "upcoming" },
					"status record",
				);
				assertDuplicateSemantics(env);
			},
		),

	"F2P-067": (env) =>
		withNative(env, ["getInitialStepIndex", "stepGet", "stepFirst", "stepNext"], async () => {
			assert.equal(env.core.getInitialStepIndex(twoSteps, "b"), 1, "matching index");
			const map = env.core.createStepMap(twoSteps);
			assert.equal(map.get("a"), steps[0], "get identity");
			assert.equal(map.first(), steps[0], "first identity");
			assert.equal(map.next("a"), steps[1], "next identity");
		}),

	"F2P-068": (env) =>
		withNative(env, ["stepIds", "stepNext"], async () => {
			assertPinnedRustToolchain(env);
			assertChangeScope(env);
			const map = env.core.createStepMap(twoSteps);
			assert.equal(map.next("a"), steps[1], "native next");
		}),

	"F2P-069": (env) =>
		withNative(
			env,
			["stepAt", "stepNext", "parseStep", "validateStepPrepare", "getStepStatus", "matchStep"],
			async () => {
				const map = env.core.createStepMap(twoSteps);
				assert.equal(map.at(1), steps[1], "at(1)");
				assert.equal(map.at(2), undefined, "at(2)");
				await assertCombinedCoreCalls(env);
			},
		),

	"F2P-070": (env) =>
		withNative(env, ["stepIds", "stepAt"], async () => {
			const map = env.core.createStepMap(steps);
			assert.deepEqual(map.steps, steps, "steps in order");
			assert.deepEqual(map.ids, ["a", "b", "c"], "ids in order");
			assert.equal(map.at(1), steps[1], "at(1)");
			assert.equal(map.at(-1), undefined, "at(-1)");
			assert.equal(map.at(1.5), undefined, "at(1.5)");
			assert.equal(map.at(Number.NaN), undefined, "at(NaN)");
			assert.equal(map.at(3), undefined, "at(3)");
		}),

	"F2P-071": (env) =>
		withNative(env, ["getInitialStepIndex", "stepGet", "stepIndexOf", "stepNext"], async () => {
			assert.equal(env.core.getInitialStepIndex(steps), 0, "default index");
			assert.equal(env.core.getInitialStepIndex(steps, "missing"), 0, "missing index");
			assertDuplicateSemantics(env);
		}),
};

export async function runObservation(env, id) {
	const observation = observations[id];
	if (!observation) {
		throw new Error(`unknown observation ${id}`);
	}
	try {
		await observation(env);
		return { id, status: "PASS" };
	} catch (error) {
		return { id, status: "FAIL", error: error instanceof Error ? error.message : String(error) };
	}
}

export async function runAll(env) {
	const results = [];
	for (const id of Object.keys(observations)) {
		results.push(await runObservation(env, id));
	}
	return results;
}
