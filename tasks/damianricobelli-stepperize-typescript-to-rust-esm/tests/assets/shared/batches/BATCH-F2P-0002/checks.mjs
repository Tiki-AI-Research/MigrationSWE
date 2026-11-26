import { assertPublicTypeContract } from "../../type-contract.mjs";
import { coreRuntimeEntry } from "../../package-entry.mjs";
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { cp, mkdir, mkdtemp, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import * as fixtures from "../../../fixtures/batches/BATCH-F2P-0002/steps.mjs";
import {
	assertPropagatesOnNativeFailure,
	assertPublishedPackageRunsNative,
	assertTracksNativeReturn,
	rustRuntime, importRustCore, discoverRuntimeArtifacts, assertRustBuildWiring,
	assertFailsWithoutRust, assertCoreOperationsDelegate, assertOperationDelegates,
	decodeStringConstants, rustBuildOnly, productionSourceFiles,
	discoverAddons,
	discoverCrate,
	toolchainPinsRust185,
} from "../../native-layout.mjs";

export const observationIds = [
	"F2P-028",
	"F2P-032",
	"F2P-035",
	"F2P-044",
	"F2P-038",
	"F2P-025",
	"F2P-026",
	"F2P-027",
	"F2P-029",
	"F2P-030",
	"F2P-031",
	"F2P-033",
	"F2P-034",
	"F2P-036",
	"F2P-037",
	"F2P-039",
	"F2P-040",
	"F2P-041",
	"F2P-042",
	"F2P-043",
	"F2P-045",
	"F2P-046",
	"F2P-047",
	"F2P-048",
];

class InfraError extends Error {
	constructor(message) {
		super(message);
		this.name = "InfraError";
	}
}

let importSequence = 0;

function coreDir(root) {
	return resolve(root, "packages/core");
}

function findNativeArtifact(root) {
 const artifacts = discoverRuntimeArtifacts(root);
 assert.ok(artifacts.length > 0, "a compiled Rust runtime must have been loaded");
 return artifacts[0];
}

async function importCore(root) { return importRustCore(root); }

function assertNativeCalls(context, names) {
	// Prove the public operation executed compiled Rust code without depending on
	// the reference solution's private native ABI names (undeclared by
	// `instruction.md`).  `names` is kept for diagnostics only.
	const observed = context.calls();
	assert.ok(
		observed.length > 0,
		`execution path must call at least one native function (reference operations: ${names.join(", ")}); observed none`,
	);
}

// Every migrated public operation must produce its result *from* the native
// addon, not merely call the addon for show.  For each operation we record the
// result with the real addon, discover which native functions it invoked, then
// replace exactly those functions with a unique sentinel and re-run against a
// fresh facade import.  A result that is unchanged under the stub was computed
// in TypeScript (a "nudge" call to satisfy a call-count gate does not count),
// which is precisely the fake-migration this check must reject.  The proof is
// independent of the candidate's private ABI names and of the native return
// shape, so equivalent implementations that genuinely delegate still pass.
async function assertAllPublicOperationsDelegate(context) {
 await assertCoreOperationsDelegate(context.root);
}

async function assertNeighborsDelegation(context, two, sentinel) {
 await assertOperationDelegates(context.root, (core) => core.createStepMap(two).neighbors("a"), "neighbors");
}

async function copyBuiltPackage(root, destination) {
	const core = coreDir(root);
	await mkdir(destination, { recursive: true });
	await cp(join(core, "package.json"), join(destination, "package.json"));
	await cp(join(core, "dist"), join(destination, "dist"), { recursive: true });
	const addons = discoverAddons(root);
	if (addons.length === 0) {
		throw new Error(`Rust native artifact is missing from ${core}`);
	}
	for (const addon of addons) {
		const relativePath = relative(core, addon).replaceAll("\\", "/");
		const target = join(destination, relativePath);
		await mkdir(dirname(target), { recursive: true });
		await cp(addon, target);
	}
}

async function assertPublicCallsFailWithoutNative(root) { await assertFailsWithoutRust(root); }

async function assertPackagedConsumer(root) {
	// Copy exactly the files `package.json.files` declares and prove a consumer
	// can import the package and execute the addon (no `.node` literal).
	await assertPublishedPackageRunsNative(root, "f2p-0002");
}

function assertNoFallbackIndirection(root) {
	const files = productionSourceFiles(root);
	const forbidden = ["child_process", "worker_threads", "spawnSync", "execFileSync", "execSync"];
	for (const file of files) {
		if (!existsSync(file)) continue;
		const contents = readFileSync(file, "utf8");
		const folded = decodeStringConstants(contents);
		if (rustBuildOnly(contents)) continue;
		for (const token of forbidden) {
			assert.ok(!contents.includes(token), `${file} must not use ${token}`);
			assert.ok(
				!folded.some((value) => value.includes(token)),
				`${file} must not reconstruct ${token} from string fragments`,
			);
		}
	}
}

export function assertStatusRecord(core) {
	// Every string id is legal. Ordinary assignment on {} invokes the legacy
	// __proto__ setter and silently omits that id instead of recording a status.
	const ids = ["before", "__proto__", "after", "constructor", "toString"];
	const steps = ids.map((id) => ({ id }));
	for (let current = 0; current < steps.length; current += 1) {
		const statuses = core.getStepStatuses(steps, current);
		assert.deepEqual(Object.keys(statuses).sort(), [...ids].sort(),
			"status record must contain every step id");
		for (let index = 0; index < ids.length; index += 1) {
			assert.equal(statuses[ids[index]], index === current ? "active" : index < current ? "previous" : "upcoming",
				`status record position for ${ids[index]}`);
		}
	}
}

const checks = {
	async "F2P-028"(context) {
		const map = context.core.createStepMap([{ id: "a" }]);
		assert.equal(map.has("a"), true);
		assert.equal(map.has("missing"), false);

		const { data, symbol } = fixtures.makeOwnKeyData();
		const copy = context.core.getInitialData(data);
		assert.notEqual(copy, data, "getInitialData must return a different object");
		assert.equal(
			Object.prototype.hasOwnProperty.call(copy, "__proto__"),
			true,
			"copy must own __proto__",
		);
		assert.deepEqual(copy["__proto__"], { polluted: true });
		assert.equal(Object.getPrototypeOf(copy), Object.prototype);
		const symbols = Object.getOwnPropertySymbols(copy);
		assert.ok(symbols.includes(symbol), "copy must retain the symbol-keyed property");
		assert.equal(copy[symbol], "symbol");
		assertNativeCalls(context, ["stepHas", "getInitialData"]);
	},

	async "F2P-032"(context) {
		const manifest = JSON.parse(readFileSync(join(coreDir(context.root), "package.json"), "utf8"));
		assert.equal(manifest.type, "module");
		assertRustBuildWiring(context.root);
		assert.match(manifest.scripts.test, /test|vitest/i, "test script must remain available");
		assert.ok(existsSync(findNativeArtifact(context.root)));
		assert.ok(discoverCrate(context.root), "a Rust crate with sources must exist inside packages/core");
		assert.ok(toolchainPinsRust185(context.root), "the Rust toolchain must be pinned to the 1.85 line");
		assert.ok(existsSync(coreRuntimeEntry(context.root)));
		assert.deepEqual(context.core.createStepMap([{ id: "a" }]).ids, ["a"]);
		assertNativeCalls(context, ["stepIds"]);
	},

	async "F2P-035"(context) {
		const steps = [
			{ id: "a" },
			{ id: "b" },
			{ id: "c" },
		];
		assert.equal(context.core.getStepStatus(steps, 1, "a"), "previous");
		assert.equal(context.core.getStepStatus(steps, 1, "b"), "active");
		assert.equal(context.core.getStepStatus(steps, 1, "c"), "upcoming");

		const two = [{ id: "a" }, { id: "b" }];
		assert.deepEqual(context.core.createStepMap(two).neighbors("a"), {
			prev: undefined,
			next: two[1],
		});
		assertNativeCalls(context, ["stepNeighbors", "getStepStatus"]);

		const sentinel = { prev: "native-stub-prev", next: "native-stub-next" };
		await assertNeighborsDelegation(context, two, sentinel);
	},

	async "F2P-044"(context) {
		const two = [{ id: "a" }, { id: "b" }];
		assert.deepEqual(context.core.createStepMap(two).next("a"), two[1]);
		assert.equal(context.core.parseStep(two, "b"), "b");
		assert.deepEqual(await context.core.validateStep(two, "a", 42), { success: true, data: 42 });
		assert.equal(context.core.getStepStatus(two, 1, "a"), "previous");
		assert.equal(context.core.matchStep(two, "b", { b: (step) => step.id }), "b");
		assertNativeCalls(context, [
			"stepNext",
			"parseStep",
			"validateStepPrepare",
			"getStepStatus",
			"matchStep",
		]);
		await assertPublicCallsFailWithoutNative(context.root);
		await assertAllPublicOperationsDelegate(context);
	},

	async "F2P-038"(context) {
		const steps = [{ id: "a" }, { id: "b" }];
		const map = context.core.createStepMap(steps);
		assert.deepEqual(map.at(1), { id: "b" });
		for (const index of [-1, 1.5, Number.NaN, 2]) {
			assert.equal(map.at(index), undefined, `at(${String(index)}) must be undefined`);
		}
		assertNativeCalls(context, ["stepAt"]);
	},

	async "F2P-025"(context) {
		assert.equal(context.core.getInitialStepIndex(fixtures.twoSteps, "b"), 1);
		assert.equal(context.core.getInitialStepIndex(fixtures.twoSteps), 0);
		const steps = [{ id: "a", schema: fixtures.emptyIssuesSchema }];
		assert.deepEqual(await context.core.validateStep(steps, "a", "x"), {
			success: false,
			issues: [],
		});
		assertNativeCalls(context, [
			"getInitialStepIndex",
			"validateStepPrepare",
			"validateStepFinish",
		]);
	},

	async "F2P-026"(context) {
		const steps = [{ id: "a" }, { id: "b" }];
		const invoked = [];
		assert.throws(
			() =>
				context.core.matchStep(steps, "missing", {
					a: () => invoked.push("a"),
					b: () => invoked.push("b"),
				}),
			/not found/,
		);
		assert.deepEqual(invoked, []);
		assert.throws(
			() => context.core.matchStep(steps, "b", { a: () => invoked.push("a") }),
			/No match handler/,
		);
		assert.deepEqual(invoked, []);
		assert.deepEqual(context.core.createStepMap(steps).at(1), steps[1]);
		assertNativeCalls(context, ["matchStep", "stepAt"]);
	},

	async "F2P-027"(context) {
		const map = context.core.createStepMap(fixtures.threeSteps);
		assert.deepEqual(map.ids, ["a", "b", "c"]);
		assert.deepEqual(map.neighbors("b"), { prev: fixtures.steps[0], next: fixtures.steps[2] });
		assert.deepEqual(map.neighbors("a"), { prev: undefined, next: fixtures.steps[1] });
		assert.deepEqual(map.neighbors("c"), { prev: fixtures.steps[1], next: undefined });
		assert.deepEqual(map.neighbors("missing"), { prev: undefined, next: undefined });
		assertPublicTypeContract(context.root);
		assertNativeCalls(context, ["stepIds", "stepNeighbors"]);
	},

	async "F2P-029"(context) {
		const two = [{ id: "a" }, { id: "b" }];
		const map = context.core.createStepMap(two);
		assert.equal(map.get("b"), two[1]);
		assert.equal(map.get("missing"), undefined);
		const steps = [{ id: "a", schema: fixtures.emptyIssuesSchema }];
		assert.deepEqual(await context.core.validateStep(steps, "a", "x"), {
			success: false,
			issues: [],
		});
		assertNativeCalls(context, ["stepGet", "validateStepPrepare", "validateStepFinish"]);
	},

	async "F2P-030"(context) {
		const duplicate = fixtures.duplicateSteps;
		const map = context.core.createStepMap(duplicate);
		assert.equal(map.get("a"), duplicate[0]);
		assert.equal(map.indexOf("a"), 0);
		assert.equal(map.next("a"), duplicate[1]);
		assert.deepEqual(map.neighbors("a"), { prev: undefined, next: duplicate[1] });
		assert.deepEqual(map.steps, duplicate);
		assert.deepEqual(map.ids, ["a", "a"]);
		assertNativeCalls(context, ["stepGet", "stepIndexOf", "stepNext", "stepNeighbors"]);
	},

	async "F2P-031"(context) {
		const two = [{ id: "a" }, { id: "b" }];
		const map = context.core.createStepMap(two);
		assert.equal(map.at(1), two[1]);
		assert.equal(map.at(2), undefined);

		const duplicate = fixtures.duplicateSteps;
		const duplicateMap = context.core.createStepMap(duplicate);
		assert.equal(duplicateMap.get("a"), duplicate[0]);
		assert.equal(duplicateMap.indexOf("a"), 0);
		assert.equal(duplicateMap.next("a"), duplicate[1]);
		assertNativeCalls(context, ["stepAt", "stepGet", "stepIndexOf", "stepNext"]);
	},

	async "F2P-033"(context) {
		const value = { x: 1 };
		assert.deepEqual(await context.core.validateStep([{ id: "a" }], "a", value), {
			success: true,
			data: value,
		});
		assert.deepEqual(context.core.createStepMap([{ id: "a" }]).ids, ["a"]);
		assertNativeCalls(context, ["validateStepPrepare", "stepIds"]);
		assertNoFallbackIndirection(context.root);
	},

	async "F2P-034"(context) {
		const steps = [{ id: "receiver", schema: fixtures.receiverSchema }];
		assert.deepEqual(await context.core.validateStep(steps, "receiver", "ok"), {
			success: true,
			data: "ok",
		});

		let directFailure;
		try {
			const unboundValidate = fixtures.receiverSchema["~standard"].validate;
			unboundValidate("ok");
		} catch (error) {
			directFailure = error;
		}
		assert.ok(directFailure instanceof Error, "validate without its receiver must fail");
		assert.deepEqual(context.core.createStepMap([{ id: "a" }]).ids, ["a"]);
		assertNativeCalls(context, ["validateStepPrepare", "stepIds"]);
	},

	async "F2P-036"(context) {
		const two = fixtures.twoSteps;
		assert.equal(context.core.parseStep(two, "b"), "b");
		for (const value of ["missing", null, undefined, 42]) {
			assert.equal(context.core.parseStep(two, value), undefined);
		}

		const duplicate = fixtures.duplicateSteps;
		const map = context.core.createStepMap(duplicate);
		assert.equal(map.get("a"), duplicate[0]);
		assert.equal(map.indexOf("a"), 0);
		assert.equal(map.next("a"), duplicate[1]);
		assertNativeCalls(context, ["parseStep", "stepGet", "stepIndexOf", "stepNext"]);
	},

	async "F2P-037"(context) {
		const two = [{ id: "a" }, { id: "b" }];
		assert.throws(() => context.core.matchStep(two, "missing", { a: () => 1 }), /not found/);
		assert.throws(() => context.core.matchStep(two, "b", { a: () => 1 }), /No match handler/);
		const map = context.core.createStepMap(two);
		assert.deepEqual(map.at(1), { id: "b" });
		for (const index of [-1, 1.5, Number.NaN, 2]) {
			assert.equal(map.at(index), undefined);
		}
		assertNativeCalls(context, ["matchStep", "stepAt"]);
	},

	async "F2P-039"(context) {
		const steps = [
			{ id: "pass", schema: fixtures.passThroughSchema },
			{ id: "reject", schema: fixtures.rejectingSchema },
		];
		assert.deepEqual(await context.core.validateStep(steps, "pass", "parsed"), {
			success: true,
			data: "parsed",
		});
		assert.deepEqual(await context.core.validateStep(steps, "reject", "x"), {
			success: false,
			issues: [{ message: "Required" }],
		});

		const duplicate = fixtures.duplicateSteps;
		const map = context.core.createStepMap(duplicate);
		assert.equal(map.get("a"), duplicate[0]);
		assert.equal(map.indexOf("a"), 0);
		assert.equal(map.next("a"), duplicate[1]);
		assertNativeCalls(context, [
			"validateStepPrepare",
			"validateStepFinish",
			"stepGet",
			"stepIndexOf",
			"stepNext",
		]);
	},

	async "F2P-040"(context) {
		const steps = [
			{ id: "a" },
			{ id: "b" },
			{ id: "c" },
		];
		assert.deepEqual(context.core.getStepStatuses(steps, 1), {
			a: "previous",
			b: "active",
			c: "upcoming",
		});
		assertStatusRecord(context.core);
		const schemaSteps = [{ id: "a", schema: fixtures.emptyIssuesSchema }];
		assert.deepEqual(await context.core.validateStep(schemaSteps, "a", "x"), {
			success: false,
			issues: [],
		});
		assertNativeCalls(context, [
			"getStepStatuses",
			"validateStepPrepare",
			"validateStepFinish",
		]);
	},

	async "F2P-041"(context) {
		const data = { a: { saved: true } };
		const copy = context.core.getInitialData(data);
		assert.notEqual(copy, data);
		assert.equal(copy.a, data.a);
		assert.deepEqual(copy, data);
		assert.deepEqual(context.core.getInitialData(), {});

		const steps = [{ id: "a", schema: fixtures.emptyIssuesSchema }];
		assert.deepEqual(await context.core.validateStep(steps, "a", "x"), {
			success: false,
			issues: [],
		});
		assertNativeCalls(context, [
			"getInitialData",
			"validateStepPrepare",
			"validateStepFinish",
		]);
	},

	async "F2P-042"(context) {
		const two = [{ id: "a" }, { id: "b" }];
		const map = context.core.createStepMap(two);
		assert.equal(map.get("b"), two[1]);
		assert.equal(map.get("missing"), undefined);
		assert.equal(map.at(1), two[1]);
		assert.equal(map.at(2), undefined);
		assert.equal(map.indexOf("b"), 1);
		assert.equal(map.indexOf("missing"), -1);

		const steps = [{ id: "receiver", schema: fixtures.receiverSchema }];
		assert.deepEqual(await context.core.validateStep(steps, "receiver", "ok"), {
			success: true,
			data: "ok",
		});
		assertNativeCalls(context, ["stepGet", "stepAt", "stepIndexOf", "validateStepPrepare"]);
	},

	async "F2P-043"(context) {
		assert.ok(discoverCrate(context.root), "a Rust crate with sources must exist inside packages/core");
		assert.ok(toolchainPinsRust185(context.root), "the Rust toolchain must be pinned to the 1.85 line");
		assert.ok(existsSync(findNativeArtifact(context.root)));

		const steps = [{ id: "a" }, { id: "b" }];
		assert.deepEqual(context.core.createStepMap(steps).next("a"), { id: "b" });
		assertPublicTypeContract(context.root);
		assertNativeCalls(context, ["stepNext"]);
		await assertPublicCallsFailWithoutNative(context.root);
	},

	async "F2P-045"(context) {
		assert.deepEqual(context.core.createStepMap([{ id: "a" }]).ids, ["a"]);
		assertNativeCalls(context, ["stepIds"]);
		// Publication is verified behaviourally (see assertPackagedConsumer).
		await assertPackagedConsumer(context.root);
	},

	async "F2P-046"(context) {
		const two = [{ id: "a" }, { id: "b" }];
		assert.deepEqual(context.core.createStepMap(two).neighbors("a"), {
			prev: undefined,
			next: two[1],
		});
		assertNativeCalls(context, ["stepNeighbors"]);

		const sentinel = { prev: { id: "stub-prev" }, next: { id: "stub-next" } };
		await assertNeighborsDelegation(context, two, sentinel);
	},

	async "F2P-047"(context) {
		const steps = [
			{ id: "a" },
			{ id: "b" },
			{ id: "c" },
		];
		const map = context.core.createStepMap(steps);
		assert.deepEqual(map.steps, steps);
		assert.equal(map.steps[0], steps[0]);
		assert.equal(map.steps[2], steps[2]);
		assert.deepEqual(map.ids, ["a", "b", "c"]);

		const schemaSteps = [{ id: "a", schema: fixtures.emptyIssuesSchema }];
		assert.deepEqual(await context.core.validateStep(schemaSteps, "a", "x"), {
			success: false,
			issues: [],
		});
		assertNativeCalls(context, ["stepIds", "validateStepPrepare", "validateStepFinish"]);
	},

	async "F2P-048"(context) {
		const required = [
			"packages/core/package.json",
			coreRuntimeEntry(context.root),
		];
		for (const relativePath of required) {
			assert.ok(existsSync(resolve(context.root, relativePath)), `missing required file ${relativePath}`);
		}
		assert.ok(discoverCrate(context.root), "a Rust crate with sources must exist inside packages/core");
		assert.ok(toolchainPinsRust185(context.root), "the Rust toolchain must be pinned to the 1.85 line");
		const manifest = JSON.parse(readFileSync(join(coreDir(context.root), "package.json"), "utf8"));
		assertRustBuildWiring(context.root);
		assert.match(manifest.scripts.test, /test|vitest/i);
		assert.ok(existsSync(findNativeArtifact(context.root)));

		const steps = [{ id: "a" }, { id: "b" }];
		assert.deepEqual(context.core.createStepMap(steps).next("a"), { id: "b" });
		assertNativeCalls(context, ["stepNext"]);
	},
};

export async function runChecks(root = process.cwd()) {
	const results = {};
 const runtime = rustRuntime(root);
 const probe = {
  install: () => runtime.reinstall(),
  restore: () => runtime.restore(),
  calls: () => [...runtime.counts].flatMap(([name, n]) => Array(n).fill(name)),
  stub: (name, implementation) => runtime.stub(name, implementation),
 };
	for (const id of observationIds) {
		probe.install();
		try {
			const core = await importCore(root);
			await checks[id]({
				root,
				core,
				native: runtime,
				artifact: discoverRuntimeArtifacts(root)[0],
				calls: () => probe.calls(),
				stub: (name, implementation) => probe.stub(name, implementation),
				reset: () => probe.install(),
				fresh: () => importCore(root),
			});
			results[id] = { status: "PASS" };
		} catch (error) {
			results[id] = {
				status: error instanceof InfraError ? "INFRA_ERROR" : "FAIL",
				detail: error.message,
			};
		} finally {
			probe.restore();
		}
	}
	return results;
}

export function reportResults(results) {
	const summary = {};
	for (const id of observationIds) {
		summary[id] = results[id] ?? { status: "INFRA_ERROR", detail: "observation was not executed" };
	}
	return summary;
}
