import { assertPublicTypeContract } from "../../type-contract.mjs";
import { coreRuntimeEntry } from "../../package-entry.mjs";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { importFresh } from "../../skeleton/assertions.mjs";
import { twoSteps } from "../../../fixtures/skeleton/steps.mjs";
import {
	DOCS_DIGEST_SHA256,
	DOCS_PUBLIC_REGISTRY_DIR,
	DOCS_REGISTRY_PATH,
	OBSERVATION_IDS,
	REACT_GUARD_TEST_TITLE,
	RUNTIME_EXPORTS,
	TYPE_NAMES,
	TYPES_FILE_SHA256,
} from "../../../fixtures/batches/BATCH-P2P-0002/constants.mjs";

function requireFile(path) {
	if (!existsSync(path)) {
		throw new Error(`required file is missing: ${path}`);
	}
	return path;
}

function run(command, args, root) {
	return execFileSync(command, args, {
		cwd: root,
		encoding: "utf8",
		env: process.env,
		stdio: "pipe",
	});
}

function ensureCoreBuilt(root) {
	try { return coreRuntimeEntry(root); }
	catch { run("pnpm", ["--filter", "@stepperize/core", "build"], root); }
	return coreRuntimeEntry(root);
}

async function loadCore(root) {
	return importFresh(ensureCoreBuilt(root));
}

function stepId(step) {
	return step === undefined ? null : { id: step.id };
}

function valueOrNull(value) {
	return value === undefined ? null : value;
}

function captureSync(operation) {
	try {
		operation();
		return null;
	} catch (error) {
		return error instanceof Error ? error : new Error(String(error));
	}
}

async function captureAsync(operation) {
	try {
		await operation();
		return null;
	} catch (error) {
		return error instanceof Error ? error : new Error(String(error));
	}
}

function docsFiles(root) {
	const registry = resolve(root, DOCS_REGISTRY_PATH);
	requireFile(registry);
	const publicDir = resolve(root, DOCS_PUBLIC_REGISTRY_DIR);
	const publicFiles = readdirSync(publicDir)
		.filter((name) => name.endsWith(".json"))
		.sort()
		.map((name) => `${DOCS_PUBLIC_REGISTRY_DIR}/${name}`);
	return [DOCS_REGISTRY_PATH, ...publicFiles];
}

function digestFiles(root, relativeFiles) {
	const hash = createHash("sha256");
	for (const relative of relativeFiles) {
		hash.update(relative);
		hash.update("\0");
		hash.update(readFileSync(resolve(root, relative)));
		hash.update("\0");
	}
	return hash.digest("hex");
}

function assertDocsUnchanged(root) {
	const files = docsFiles(root);
	const digest = digestFiles(root, files);
	if (digest !== DOCS_DIGEST_SHA256) {
		throw new Error(`apps/docs registry inputs changed: ${digest}`);
	}
	return { fileCount: files.length, sha256: digest };
}

function assertTypesUnchanged(root) {
	const typesPath = resolve(root, "packages/core/src/types.ts");
	requireFile(typesPath);
	const digest = createHash("sha256").update(readFileSync(typesPath)).digest("hex");
	if (digest !== TYPES_FILE_SHA256) {
		throw new Error(`packages/core/src/types.ts changed: ${digest}`);
	}
	const checked = assertPublicTypeContract(root);
	return { typesFileSha256: digest, typeExports: TYPE_NAMES.map(name => [name, checked.exported.includes(name)]) };
}

function packageProjection(root) {
	const manifest = JSON.parse(readFileSync(resolve(root, "packages/core/package.json"), "utf8"));
	coreRuntimeEntry(root);
	assertPublicTypeContract(root);
	return { name: manifest.name, type: manifest.type, rootImportResolves: true, rootTypesResolve: true };
}

function runReactGuardTest(root) {
	const testPath = resolve(root, "packages/react/src/tests/define-stepper.test.tsx");
	const testSource = readFileSync(requireFile(testPath), "utf8");
	if (!testSource.includes(REACT_GUARD_TEST_TITLE)) {
		throw new Error(`react guard-context test is missing: ${REACT_GUARD_TEST_TITLE}`);
	}
	const report = join(tmpdir(), `p2p-0002-react-${process.pid}-${Date.now()}.json`);
	run(
		"pnpm",
		[
			"--filter",
			"@stepperize/react",
			"exec",
			"vitest",
			"run",
			"--reporter=json",
			`--outputFile=${report}`,
			"-t",
			REACT_GUARD_TEST_TITLE,
		],
		root,
	);
	const parsed = JSON.parse(readFileSync(report, "utf8"));
	if ((parsed.numPassedTests ?? 0) < 1 || (parsed.numFailedTests ?? 0) !== 0) {
		throw new Error(`react guard-context test did not pass: ${JSON.stringify(parsed)}`);
	}
	return { test: REACT_GUARD_TEST_TITLE, present: true, status: "PASS" };
}

async function projectionFor(id, root) {
	if (id === "P2P-025") {
		const core = await loadCore(root);
		const map = core.createStepMap(twoSteps);
		return {
			prevB: stepId(map.prev("b")),
			prevA: stepId(map.prev("a")),
			prevMissing: stepId(map.prev("missing")),
		};
	}

	if (id === "P2P-026") {
		return assertDocsUnchanged(root);
	}

	if (id === "P2P-027") {
		const core = await loadCore(root);
		const error = await captureAsync(() => core.validateStep([{ id: "a" }], "missing", null));
		return {
			isError: error instanceof Error,
			messageIncludesNotFound: Boolean(error && /not found/.test(error.message)),
		};
	}

	if (id === "P2P-028") {
		const core = await loadCore(root);
		const result = await core.validateStep([{ id: "a" }], "a", { x: 1 });
		return { success: result.success, data: result.data };
	}

	if (id === "P2P-029") {
		const core = await loadCore(root);
		return {
			withB: core.getInitialStepIndex(twoSteps, "b"),
			omitted: core.getInitialStepIndex(twoSteps),
			unknown: core.getInitialStepIndex(twoSteps, "missing"),
		};
	}

	if (id === "P2P-030") {
		const core = await loadCore(root);
		return {
			createStepMapType: typeof core.createStepMap,
			ids: core.createStepMap([{ id: "a" }]).ids,
		};
	}

	if (id === "P2P-031") {
		const core = await loadCore(root);
		const calls = [];
		const error = captureSync(() =>
			core.matchStep(twoSteps, "b", {
				a: (step) => {
					calls.push(step.id);
					return step.id;
				},
			}),
		);
		return {
			isError: error instanceof Error,
			messageIncludesNoMatchHandler: Boolean(error && /No match handler/.test(error.message)),
			otherHandlerCalls: calls.length,
		};
	}

	if (id === "P2P-032") {
		const core = await loadCore(root);
		const map = core.createStepMap(twoSteps);
		return { getB: stepId(map.get("b")), getMissing: stepId(map.get("missing")) };
	}

	if (id === "P2P-033") {
		const core = await loadCore(root);
		return { runtimeExports: RUNTIME_EXPORTS.map((name) => [name, typeof core[name]]) };
	}

	if (id === "P2P-034") {
		return packageProjection(root);
	}

	if (id === "P2P-035") {
		const core = await loadCore(root);
		return {
			second: valueOrNull(core.parseStep(twoSteps, "b")),
			missing: valueOrNull(core.parseStep(twoSteps, "missing")),
			nullValue: valueOrNull(core.parseStep(twoSteps, null)),
			undefinedValue: valueOrNull(core.parseStep(twoSteps, undefined)),
			numberValue: valueOrNull(core.parseStep(twoSteps, 42)),
		};
	}

	if (id === "P2P-036") {
		const core = await loadCore(root);
		const map = core.createStepMap(twoSteps);
		return { ids: map.ids, statuses: core.getStepStatuses(twoSteps, 0) };
	}

	if (id === "P2P-037") {
		const core = await loadCore(root);
		return {
			missing: valueOrNull(core.parseStep(twoSteps, "missing")),
			nullValue: valueOrNull(core.parseStep(twoSteps, null)),
			undefinedValue: valueOrNull(core.parseStep(twoSteps, undefined)),
			numberValue: valueOrNull(core.parseStep(twoSteps, 42)),
		};
	}

	if (id === "P2P-038") {
		const core = await loadCore(root);
		const map = core.createStepMap(twoSteps);
		const neighbors = (stepIdValue) => {
			const value = map.neighbors(stepIdValue);
			return { prev: stepId(value.prev), next: stepId(value.next) };
		};
		return {
			a: neighbors("a"),
			b: neighbors("b"),
			missing: neighbors("missing"),
		};
	}

	if (id === "P2P-039") {
		return assertTypesUnchanged(root);
	}

	if (id === "P2P-040") {
		return runReactGuardTest(root);
	}

	throw new Error(`unknown observation id: ${id}`);
}

export const projections = Object.freeze(
	Object.fromEntries(OBSERVATION_IDS.map((id) => [id, async (root) => projectionFor(id, root)])),
);

export async function runObservation(id, root) {
	if (!Object.hasOwn(projections, id)) {
		throw new Error(`unknown observation id: ${id}`);
	}
	return projections[id](root);
}

export async function runProjections(root) {
	const result = {};
	for (const id of OBSERVATION_IDS) {
		result[id] = await runObservation(id, root);
	}
	return result;
}

export { OBSERVATION_IDS };
