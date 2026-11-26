import { coreRuntimeEntry } from "../../package-entry.mjs";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { readFile, readdir } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { steps2, steps3, emptySteps, parsedSchema, issuesSchema } from "../../../fixtures/batches/BATCH-P2P-0001/steps.mjs";

let importSequence = 0;

async function importFresh(file) {
	importSequence += 1;
	return import(`${pathToFileURL(file).href}?p2p=${importSequence}`);
}

function hash(content) {
	return createHash("sha256").update(content).digest("hex");
}

async function hashDirectory(root, relativePaths) {
	const hashState = createHash("sha256");
	const entries = [];
	for (const relativePath of relativePaths) {
		const absolutePath = resolve(root, relativePath);
		if (!existsSync(absolutePath)) continue;
		const children = await readdir(absolutePath, { withFileTypes: true }).catch(() => []);
		if (children.length === 0) {
			entries.push(relativePath);
			continue;
		}
		for (const child of children) {
			if (child.isDirectory()) {
				if (child.name === "node_modules" || child.name === "dist") continue;
				const nested = await hashDirectory(root, [`${relativePath}/${child.name}`]);
				entries.push(...nested.entries);
			} else if (child.isFile()) {
				entries.push(`${relativePath}/${child.name}`);
			}
		}
	}
	entries.sort();
	for (const entry of entries) {
		hashState.update(entry);
		hashState.update("\0");
		hashState.update(await readFile(resolve(root, entry)));
		hashState.update("\0");
	}
	return { digest: hashState.digest("hex"), entries };
}

function runCommand(root, command, args) {
	const result = spawnSync(command, args, {
		cwd: root,
		encoding: "utf8",
		env: {
			...process.env,
			CI: "1",
			COREPACK_ENABLE_NETWORK: "0",
			COREPACK_DEFAULT_TO_LATEST: "0",
		},
		timeout: 600000,
	});
	if (result.error) throw result.error;
	if (result.status !== 0) {
		const detail = `${result.stdout ?? ""}\n${result.stderr ?? ""}`.trim().split("\n").slice(-12).join("\n");
		throw new Error(`${command} ${args.join(" ")} exited ${result.status}\n${detail}`);
	}
	return { stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

function hasSkipMarkers(source) {
	return /\b(?:describe|it|test)\.(?:skip|todo|only)\b/.test(source);
}

function stepLabel(step) {
	return step && typeof step === "object" ? step.label ?? null : null;
}

export async function runP2PChecks(root = process.cwd()) {
	const core = await importFresh(coreRuntimeEntry(root));
	const results = {};

	const record = async (id, operation) => {
		try {
			results[id] = await operation();
		} catch (error) {
			results[id] = { result: "FAIL", error: error instanceof Error ? error.message : String(error) };
		}
	};

	const checks = [
		[
			"P2P-021",
			async () => {
				const registry = JSON.parse(await readFile(resolve(root, "apps/docs/registry.json"), "utf8"));
				const registryDir = resolve(root, "apps/docs/public/r");
				const missing = [];
				const withoutContent = [];
				const withoutDependency = [];
				for (const block of registry.items ?? []) {
					const builtPath = resolve(registryDir, `${block.name}.json`);
					if (!existsSync(builtPath)) {
						missing.push(block.name);
						continue;
					}
					const item = JSON.parse(await readFile(builtPath, "utf8"));
					if (item.name !== block.name) missing.push(block.name);
					if (!(item.files?.[0]?.content?.length > 0)) withoutContent.push(block.name);
					if (!Array.isArray(item.dependencies) || !item.dependencies.includes("@stepperize/react")) {
						withoutDependency.push(block.name);
					}
				}
				assert.deepEqual(missing, [], "missing built registry items");
				assert.deepEqual(withoutContent, [], "registry items without inlined content");
				assert.deepEqual(withoutDependency, [], "registry items without @stepperize/react");
				return {
					blocks: (registry.items ?? []).length,
					missing,
					withoutContent,
					withoutDependency,
					result: "PASS",
				};
			},
		],
		[
			"P2P-011",
			async () => ({
				second: core.getInitialStepIndex(steps2, "b"),
				omitted: core.getInitialStepIndex(steps2),
				missing: core.getInitialStepIndex(steps2, "missing"),
			}),
		],
		[
			"P2P-001",
			async () => {
				const pass = await core.validateStep([{ id: "a", schema: parsedSchema }], "a", { input: true });
				const fail = await core.validateStep([{ id: "a", schema: issuesSchema }], "a", { input: true });
				assert.equal(pass.success, true);
				assert.equal(pass.data, "parsed");
				assert.equal(fail.success, false);
				assert.deepEqual(fail.issues, [{ message: "bad" }]);
				return {
					pass: { success: pass.success, data: pass.data },
					fail: { success: fail.success, issues: fail.issues },
				};
			},
		],
		[
			"P2P-002",
			async () => {
				// Preserve the package/type/test source contract without requiring the
				// retired implementation file. The facade may live in src/index.ts.
				const required = [
					"packages/core/src/index.ts",
					"packages/core/src/types.ts",
					"packages/core/src/tests/utils.test.ts",
					"packages/core/package.json",
				];
				const absent = required.filter((relativePath) => !existsSync(resolve(root, relativePath)));
				assert.deepEqual(absent, [], "required source files must exist inside the repository");
				return { result: "PASS" };
			},
		],
		[
			"P2P-004",
			async () => {
				const data = { a: { saved: true } };
				const copy = core.getInitialData(data);
				assert.deepEqual(copy, data);
				assert.notEqual(copy, data);
				assert.equal(copy.a, data.a);
				return { equal: true, differentReference: true, nestedReferencePreserved: true };
			},
		],
		[
			"P2P-005",
			async () => {
				assert.equal(existsSync(resolve(root, "package.json")), true);
				assert.equal(existsSync(resolve(root, "pnpm-workspace.yaml")), true);
				return { result: "PASS" };
			},
		],
		[
			"P2P-009",
			async () => {
				const pending = core.validateStep([{ id: "a" }], "a", { x: 1 });
				assert.equal(pending instanceof Promise, true);
				const resolved = await pending;
				assert.deepEqual(resolved, { success: true, data: { x: 1 } });
				return { isPromise: true, success: resolved.success, data: resolved.data };
			},
		],
		[
			"P2P-012",
			async () => {
				let invoked = null;
				const returned = core.matchStep(steps2, "b", {
					a: (step) => {
						invoked = "a";
						return step.label;
					},
					b: (step) => {
						invoked = "b";
						return step.label;
					},
				});
				assert.equal(returned, "B");
				assert.equal(invoked, "b");
				return { returned, invoked };
			},
		],
		[
			"P2P-013",
			async () => {
				const map = core.createStepMap(steps2);
				const empty = core.createStepMap(emptySteps);
				assert.equal(map.first(), steps2[0]);
				assert.equal(map.last(), steps2[1]);
				assert.equal(empty.first(), undefined);
				assert.equal(empty.last(), undefined);
				return {
					first: map.first().id,
					last: map.last().id,
					emptyFirst: null,
					emptyLast: null,
				};
			},
		],
		[
			"P2P-014",
			async () => ({
				a: core.getStepStatus(steps3, 1, "a"),
				b: core.getStepStatus(steps3, 1, "b"),
				c: core.getStepStatus(steps3, 1, "c"),
			}),
		],
		[
			"P2P-017",
			async () => {
				runCommand(root, "pnpm", ["--filter", "docs", "exec", "vitest", "run", "src/lib/seo.test.ts"]);
				return { result: "PASS" };
			},
		],
		[
			"P2P-018",
			async () => {
				const map = core.createStepMap(steps3);
				assert.equal(map.steps.length, 3);
				assert.equal(map.steps[0], steps3[0]);
				assert.equal(map.steps[1], steps3[1]);
				assert.equal(map.steps[2], steps3[2]);
				assert.deepEqual(map.ids, ["a", "b", "c"]);
				return {
					steps: map.steps.map((step) => step.id),
					ids: map.ids,
				};
			},
		],
		[
			"P2P-003",
			async () => {
				const names = [
					"createStepMap",
					"getInitialStepIndex",
					"parseStep",
					"getInitialData",
					"validateStep",
					"getStepStatus",
					"getStepStatuses",
					"matchStep",
				];
				for (const name of names) assert.equal(typeof core[name], "function", `${name} must be callable`);
				return { functions: names };
			},
		],
		[
			"P2P-006",
			async () => {
				let unknownMessage = "";
				let handlerMessage = "";
				try {
					core.matchStep(steps2, "missing", {});
				} catch (error) {
					unknownMessage = error instanceof Error ? error.message : String(error);
				}
				try {
					core.matchStep(steps2, "b", { a: (step) => step.id });
				} catch (error) {
					handlerMessage = error instanceof Error ? error.message : String(error);
				}
				assert.match(unknownMessage, /not found/);
				assert.match(handlerMessage, /No match handler/);
				return { notFound: true, missingHandler: true };
			},
		],
		[
			"P2P-007",
			async () => {
				const map = core.createStepMap(steps2);
				assert.equal(map.indexOf("b"), 1);
				assert.equal(map.indexOf("missing"), -1);
				return { b: map.indexOf("b"), missing: map.indexOf("missing") };
			},
		],
		[
			"P2P-008",
			async () => {
				const map = core.createStepMap(steps2);
				assert.equal(map.next("a"), steps2[1]);
				assert.equal(map.next("b"), undefined);
				assert.equal(map.next("missing"), undefined);
				return {
					a: stepLabel(map.next("a")),
					b: map.next("b") === undefined ? null : stepLabel(map.next("b")),
					missing: map.next("missing") === undefined ? null : stepLabel(map.next("missing")),
				};
			},
		],
		[
			"P2P-010",
			async () => {
				const typeSource = await readFile(resolve(root, "packages/core/src/types.ts"));
				const text = typeSource.toString("utf8");
				for (const name of ["Step", "StepMap", "StepStatus", "ValidationResult", "StandardSchemaV1", "Get"]) {
					assert.match(text, new RegExp(`\\b${name}\\b`), `${name} must remain declared`);
				}
				return { typesSha256: hash(typeSource), result: "PASS" };
			},
		],
		[
			"P2P-015",
			async () => {
				const reactDigest = await hashDirectory(root, ["packages/react/src"]);
				runCommand(root, "pnpm", ["--filter", "@stepperize/react", "exec", "vitest", "run"]);
				return { reactSha256: reactDigest.digest, reactTest: "PASS" };
			},
		],
		[
			"P2P-016",
			async () => {
				const map = core.createStepMap([{ id: "a" }]);
				const expectedKeys = [
					"at",
					"first",
					"get",
					"has",
					"ids",
					"indexOf",
					"last",
					"neighbors",
					"next",
					"prev",
					"steps",
				];
				assert.deepEqual(Object.keys(map).sort(), expectedKeys);
				for (const name of ["get", "at", "indexOf", "has", "first", "last", "next", "prev", "neighbors"]) {
					assert.equal(typeof map[name], "function", `${name} must be a function`);
				}
				assert.equal(Array.isArray(map.steps), true);
				assert.equal(Array.isArray(map.ids), true);
				return {
					keys: Object.keys(map).sort(),
					functions: ["get", "at", "indexOf", "has", "first", "last", "next", "prev", "neighbors"],
				};
			},
		],
		[
			"P2P-019",
			async () => {
				const map = core.createStepMap([{ id: "a" }]);
				assert.equal(map.has("a"), true);
				assert.equal(map.has("missing"), false);
				return { a: map.has("a"), missing: map.has("missing") };
			},
		],
		[
			"P2P-020",
			async () => {
				const map = core.createStepMap(steps2);
				assert.equal(map.at(1), steps2[1]);
				assert.equal(map.at(2), undefined);
				return { one: stepLabel(map.at(1)), two: map.at(2) === undefined ? null : stepLabel(map.at(2)) };
			},
		],
		[
			"P2P-022",
			async () => {
				const statuses = core.getStepStatuses(steps3, 1);
				assert.deepEqual(statuses, { a: "previous", b: "active", c: "upcoming" });
				return { a: statuses.a, b: statuses.b, c: statuses.c };
			},
		],
		[
			"P2P-023",
			async () => {
				const testSource = await readFile(resolve(root, "packages/core/src/tests/utils.test.ts"), "utf8");
				assert.equal(hasSkipMarkers(testSource), false);
				runCommand(root, "pnpm", ["--filter", "@stepperize/core", "exec", "vitest", "run"]);
				return { result: "PASS", skipped: false };
			},
		],
		[
			"P2P-024",
			async () => {
				const empty = core.getInitialData();
				const data = { a: 1, b: 2 };
				const copy = core.getInitialData(data);
				assert.deepEqual(empty, {});
				assert.deepEqual(copy, data);
				assert.notEqual(copy, data);
				return { empty, copy, differentReference: true };
			},
		],
	];

	for (const [id, check] of checks) {
		await record(id, check);
	}

	return results;
}
