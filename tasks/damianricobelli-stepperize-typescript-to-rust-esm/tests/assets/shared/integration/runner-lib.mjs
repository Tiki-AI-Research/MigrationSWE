import { coreRuntimeEntry } from "../package-entry.mjs";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { hasRustMigration, rustRuntime } from "../native-layout.mjs";

export const REPORT_SCHEMA_VERSION = "migration-observations:v6";

const BATCH_DEFINITIONS = Object.freeze([
	{ id: "BATCH-P2P-0001", adapter: "p2p-0001" },
	{ id: "BATCH-P2P-0002", adapter: "p2p-0002" },
	{ id: "BATCH-F2P-0001", adapter: "f2p-0001" },
	{ id: "BATCH-F2P-0002", adapter: "f2p-0002" },
	{ id: "BATCH-F2P-0003", adapter: "f2p-0003" },
	{ id: "BATCH-DIFFERENTIAL-0001", adapter: "differential-0001" },
	{ id: "BATCH-DIFFERENTIAL-0002", adapter: "differential-0002" },
]);

const INFRASTRUCTURE_PREFIXES = Object.freeze([
	".cargo/",
	"vendor/",
	".pnpm-store/",
	"node_modules/",
	"apps/docs/.source/",
]);

// Files the evaluator itself writes into the candidate checkout (the harness
// junit/observations report) must not be reported as out-of-scope changes.
const INFRASTRUCTURE_FILES = Object.freeze([".harbor-result.json"]);

function resolveRepoRoot() {
	for (const key of ["MIGRATION_REPO_ROOT", "MIGRATION_REPO", "REPO_ROOT", "REPO"]) {
		const value = process.env[key];
		if (typeof value === "string" && value.trim().length > 0) {
			return resolve(value);
		}
	}
	return process.cwd();
}

function isInfrastructurePath(path) {
	const normalized = String(path).replace(/^\.\//, "");
	return (
		INFRASTRUCTURE_FILES.includes(normalized) ||
		INFRASTRUCTURE_PREFIXES.some(
			(prefix) => path === prefix.slice(0, -1) || path.startsWith(prefix),
		)
	);
}

function stableValue(value) {
	if (value === undefined) return null;
	if (typeof value === "number" && !Number.isFinite(value)) return String(value);
	if (Array.isArray(value)) return value.map(stableValue);
	if (value && typeof value === "object") {
		const result = {};
		for (const key of Object.keys(value).sort()) {
			result[key] = stableValue(value[key]);
		}
		return result;
	}
	return value;
}

function sanitizeDetail(detail, root, assetsRoot) {
	let text = detail instanceof Error ? detail.message : String(detail ?? "");
	for (const [prefix, replacement] of [
		[root, "<repo>"],
		[assetsRoot, "<assets>"],
	]) {
		if (prefix) text = text.split(prefix).join(replacement);
	}
	return text;
}

function normalizeStatus(status) {
	const value = String(status ?? "").toUpperCase();
	if (["PASS", "FAIL", "NA", "INFRA_ERROR", "INCONCLUSIVE", "TIMEOUT"].includes(value)) {
		return value;
	}
	return "INCONCLUSIVE";
}

function resultFromProbe(entry, defaultStatus = "PASS") {
	if (!entry) return { status: "INCONCLUSIVE", value: null };
	if (typeof entry === "string") return { status: normalizeStatus(entry), value: null };
	const status = entry.status ?? entry.result ?? defaultStatus;
	return {
		status: normalizeStatus(status),
		value: entry.error ?? entry.detail ?? entry.observation ?? entry,
	};
}

async function importProbe(assetsRoot, side, batchId) {
	const file = resolve(assetsRoot, side, "batches", batchId, "probe.mjs");
	return import(pathToFileURL(file).href);
}

function runPnpm(root, args, label) {
	const result = spawnSync("pnpm", args, {
		cwd: root,
		encoding: "utf8",
		env: { ...process.env, CI: "1" },
		timeout: 900000,
	});
	if (result.error || result.status !== 0) {
		const detail = `${result.stdout ?? ""}\n${result.stderr ?? ""}`.trim().split("\n").slice(-8).join("\n");
		throw new Error(`${label}: ${detail}`);
	}
}

async function ensureCoreBuild(root) {
	try { coreRuntimeEntry(root); return; } catch {}
	if (!existsSync(resolve(root, "node_modules"))) {
		runPnpm(root, ["install", "--frozen-lockfile"], "unable to install workspace dependencies");
	}
	runPnpm(root, ["--filter", "@stepperize/core", "build"], "unable to build packages/core before integration probes");
}

async function runP2P0001(context) {
	const probe = await importProbe(context.assetsRoot, context.side, "BATCH-P2P-0001");
	const fn = context.side === "source" ? probe.probeSource : probe.probeTarget;
	const raw = await fn(context.root);
	const results = {};
	for (const id of context.ids) {
		const entry = raw?.[id];
		const status = entry && entry.result === "FAIL" ? "FAIL" : entry ? "PASS" : "INCONCLUSIVE";
		results[id] = {
			status,
			value: entry ?? null,
		};
	}
	return results;
}

async function runP2P0002(context) {
	const probe = await importProbe(context.assetsRoot, context.side, "BATCH-P2P-0002");
	const results = {};
	for (const id of context.ids) {
		try {
			const value = await probe.runObservation(id, context.root);
			results[id] = {
				status: value && value.status === "FAIL" ? "FAIL" : "PASS",
				value,
			};
		} catch (error) {
			results[id] = {
				status: "FAIL",
				value: { error: context.sanitize(error) },
			};
		}
	}
	return results;
}

async function runF2P0001(context) {
	const probe = await importProbe(context.assetsRoot, context.side, "BATCH-F2P-0001");
	const fn = context.side === "source" ? probe.probeSource : probe.probeTarget;
	const entries = await fn(context.root);
	const byId = new Map(entries.map((entry) => [entry.observation_id, entry]));
	const results = {};
	for (const id of context.ids) {
		const entry = byId.get(id);
		results[id] = entry
			? { status: normalizeStatus(entry.status), value: { detail: context.sanitize(entry.detail) } }
			: { status: "INCONCLUSIVE", value: null };
	}
	return results;
}

async function runF2P0002(context) {
	const probe = await importProbe(context.assetsRoot, context.side, "BATCH-F2P-0002");
	const fn = context.side === "source" ? probe.probeSource : probe.probeTarget;
	const raw = await fn(context.root);
	const results = {};
	for (const id of context.ids) {
		const entry = raw?.[id];
		results[id] = entry
			? { status: normalizeStatus(entry.status), value: { detail: context.sanitize(entry.detail) } }
			: { status: "INCONCLUSIVE", value: null };
	}
	return results;
}

async function runF2P0003(context) {
	const probe = await importProbe(context.assetsRoot, context.side, "BATCH-F2P-0003");
	const fn = context.side === "source" ? probe.probeSource : probe.probeTarget;
	const entries = await fn(context.root);
	const byId = new Map(entries.map((entry) => [entry.id, entry]));
	const results = {};
	for (const id of context.ids) {
		const entry = byId.get(id);
		results[id] = entry
			? {
					status: normalizeStatus(entry.status),
					value: { detail: context.sanitize(entry.error ?? "") },
				}
			: { status: "INCONCLUSIVE", value: null };
	}
	return results;
}

function filterStructural(raw, context) {
	const value = stableValue(raw ?? {});
	if (Array.isArray(value.outOfScope)) {
		value.outOfScope = value.outOfScope.filter((path) => !isInfrastructurePath(path)).sort();
	}
	if (Array.isArray(value.reactChanges)) {
		value.reactChanges = value.reactChanges.filter((path) => !isInfrastructurePath(path)).sort();
	}
	if (Array.isArray(value.docsChanges)) {
		value.docsChanges = value.docsChanges.filter((path) => !isInfrastructurePath(path)).sort();
	}
	return value;
}

function differentialStatus(id, value) {
	if (id === "DIFF-028") return value.outOfScope?.length === 0 ? "PASS" : "FAIL";
	if (id === "DIFF-030") return value.reactChanges?.length === 0 ? "PASS" : "FAIL";
	if (id === "DIFF-031") return value.docsChanges?.length === 0 ? "PASS" : "FAIL";
	return "PASS";
}

async function runDifferential0001(context) {
	const probe = await importProbe(context.assetsRoot, context.side, "BATCH-DIFFERENTIAL-0001");
	const fn = context.side === "source" ? probe.probeSource : probe.probeTarget;
	const raw = await fn(context.root);
	const results = {};
	for (const id of context.ids) {
		const entry = raw?.[id];
		if (entry === undefined) {
			results[id] = { status: "INCONCLUSIVE", value: null };
			continue;
		}
		const value = filterStructural(entry, context);
		results[id] = { status: differentialStatus(id, value), value };
	}
	return results;
}

async function runDifferential0002(context) {
	const fixtures = await import(
		pathToFileURL(resolve(context.assetsRoot, "fixtures/batches/BATCH-DIFFERENTIAL-0002/repo.mjs")).href
	);
	const helpers = await import(
		pathToFileURL(
			resolve(context.assetsRoot, "shared/batches/BATCH-DIFFERENTIAL-0002/repo-assertions.mjs"),
		).href
	);

	const baselineFiles =
		context.side === "source" ? fixtures.baselineFiles : fixtures.targetBaselineFiles;
	await helpers.assertFilesUnderRoot(context.root, baselineFiles);
	const requiredFiles =
		context.side === "source" ? fixtures.sourceRequiredFiles : fixtures.targetRequiredFiles;
	const requiredCount = await helpers.assertFilesUnderRoot(context.root, requiredFiles);
	const workspace = await helpers.readJson(context.root, "package.json");
	const core = await helpers.readJson(context.root, "packages/core/package.json");
	const types = readFileSync(resolve(context.root, "packages/core/src/types.ts"), "utf8");

	if (workspace.private !== true) {
		throw new Error("the workspace baseline manifest must remain private");
	}
	if (core.name !== "@stepperize/core") {
		throw new Error("the supplied @stepperize/core manifest must remain in place");
	}
	if (types.length === 0) {
		throw new Error("the baseline types layer must remain present");
	}

	let diffWithinScope = true;
	if (context.side === "target") {
		await helpers.assertGitCheckout(context.root);
		const changed = (await helpers.collectChangedPaths(context.root)).filter(
			(path) => !isInfrastructurePath(path),
		);
		const outside = changed.filter(
			(path) =>
				!helpers.isAllowedWorkspaceChange(
					path,
					fixtures.allowedWorkspaceChangePrefixes,
					fixtures.allowedWorkspaceChangeFiles,
				),
		);
		diffWithinScope = outside.length === 0;
	}

	const requiredPresent = requiredCount === requiredFiles.length;
	const filesPresent =
		baselineFiles.length > 0 &&
		requiredPresent &&
		(context.side !== "target" || hasRustMigration(context.root));
	const status = filesPresent && diffWithinScope ? "PASS" : "FAIL";

	return {
		"DIFF-033": {
			status,
			value: {
				baselineFiles: fixtures.baselineFiles.length,
				baselinePresent: filesPresent,
				diffWithinScope,
			},
		},
		"DIFF-034": {
			status,
			value: {
				coreName: core.name,
				workspacePrivate: workspace.private === true,
				requiredFilesPresent: filesPresent,
				typesLayerPresent: types.length > 0,
			},
		},
	};
}

const ADAPTERS = Object.freeze({
	"p2p-0001": runP2P0001,
	"p2p-0002": runP2P0002,
	"f2p-0001": runF2P0001,
	"f2p-0002": runF2P0002,
	"f2p-0003": runF2P0003,
	"differential-0001": runDifferential0001,
	"differential-0002": runDifferential0002,
});

export function loadObservationInventory(assetsRoot) {
	const file = resolve(assetsRoot, "shared/integration/observations.json");
	return JSON.parse(readFileSync(file, "utf8"));
}

export async function collectSideObservations({ side, assetsRoot, root = resolveRepoRoot() }) {
	rustRuntime(root);
	await ensureCoreBuild(root);
	const inventory = loadObservationInventory(assetsRoot);
	const collected = new Map();
	const sanitize = (detail) => sanitizeDetail(detail, root, assetsRoot);

	for (const definition of BATCH_DEFINITIONS) {
		const ids = inventory
			.filter((entry) => entry.batch_id === definition.id)
			.map((entry) => entry.id);
		if (ids.length === 0) continue;
		const adapter = ADAPTERS[definition.adapter];
		try {
			const results = await adapter({ side, assetsRoot, root, ids, sanitize });
			for (const id of ids) {
				collected.set(id, results[id] ?? { status: "INCONCLUSIVE", value: null });
			}
		} catch (error) {
			for (const id of ids) {
				collected.set(id, {
					status: "INFRA_ERROR",
					value: { error: sanitize(error) },
				});
			}
		}
	}

	return inventory.map((entry) => {
		const result = collected.get(entry.id) ?? { status: "INCONCLUSIVE", value: null };
		return {
			id: entry.id,
			kind: entry.kind,
			status: normalizeStatus(result.status),
			value: { type: "json", value: stableValue(result.value) },
		};
	});
}

export async function writeReport({ side, assetsRoot, reportPath }) {
	const observations = await collectSideObservations({ side, assetsRoot });
	const report = {
		schema_version: REPORT_SCHEMA_VERSION,
		observations,
	};
	const serialized = `${JSON.stringify(report, null, 2)}\n`;
	if (reportPath) {
		mkdirSync(dirname(resolve(reportPath)), { recursive: true });
		writeFileSync(resolve(reportPath), serialized);
	}
	process.stdout.write(serialized);
	return report;
}

export { resolveRepoRoot };
