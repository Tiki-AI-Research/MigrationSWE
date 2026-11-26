import { coreRuntimeEntry } from "../../package-entry.mjs";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { basename, dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import {
	assertPropagatesOnNativeFailure,
	assertTracksNativeReturn,
	discoverAddons,
	instrumentNativeExports,
	rustRuntime, importRustCore, discoverRuntimeArtifacts, assertCoreOperationsDelegate,
} from "../../native-layout.mjs";

let importSequence = 0;

export const nativeFunctions = Object.freeze([
	"stepIds",
	"stepGet",
	"stepAt",
	"stepIndexOf",
	"stepHas",
	"stepFirst",
	"stepLast",
	"stepNext",
	"stepPrev",
	"stepNeighbors",
	"getInitialStepIndex",
	"parseStep",
	"getInitialData",
	"validateStepPrepare",
	"validateStepFinish",
	"getStepStatus",
	"getStepStatuses",
	"matchStep",
]);

export async function importFresh(file) {
	importSequence += 1;
	return import(`${pathToFileURL(file).href}?f2p0003=${importSequence}`);
}

function platformArtifactName() {
	const platform = process.platform === "linux" ? `${process.platform}-${process.arch}-gnu` : `${process.platform}-${process.arch}`;
	return `stepperize_core_native.${platform}.node`;
}

function selectNativeArtifact(nativeFiles) {
	const expected = platformArtifactName();
	return nativeFiles.includes(expected) ? expected : nativeFiles[0];
}

export async function createEnv(root, { requireNative = true } = {}) {
	const coreDir = resolve(root, "packages/core");
	const distFile = coreRuntimeEntry(root);
	const manifest = JSON.parse(readFileSync(resolve(coreDir, "package.json"), "utf8"));
 const binding = rustRuntime(root);
 const core = await importRustCore(root);
 const addons = discoverRuntimeArtifacts(root);
 const nativeFiles = addons.map((file) => basename(file));
 const nativeFile = addons[0] ?? null;
 const nativeDir = nativeFile ? dirname(nativeFile) : null;
 const native = requireNative ? binding : null;

	const env = {
		root,
		coreDir,
		distFile,
		nativeDir,
		native,
		nativeFile,
		nativeFiles,
		nativeAddons: addons,
		manifest,
		binding,
		calls: binding ? binding.counts : new Map(),
		core,
		restore() {
			if (binding) binding.restore();
		},
	};

	return env;
}

function snapshotCalls(env) {
	return new Map(env.calls);
}

// Every migrated public operation must produce its result *from* the native
// addon, not merely call it for show.  Records each operation's result with the
// real addon, discovers the native functions it invoked, replaces exactly those
// with a unique sentinel, and re-runs against a fresh facade import: an
// unchanged result was recomputed in TypeScript (a call-count "nudge" does not
// count).  Independent of the candidate's private ABI names and return shape.
export async function assertAllOperationsDelegate(env) {
 await assertCoreOperationsDelegate(env.root);
}

export async function withNative(env, requiredBindings, operation) {
	const before = env.binding.executions;
	const result = await operation(env);

	if (!env.native) {
		throw new Error("target native module is not present on the execution path");
	}

	// Prove the public operation executed compiled Rust code without depending on
	// the reference solution's private native ABI names (undeclared by
	// `instruction.md`).  `requiredBindings` is kept for diagnostics only.
	const totalBefore = before;
	const totalAfter = env.binding.executions;
	if (totalAfter <= totalBefore) {
		throw new Error(
			`the public operation must execute the Rust native addon (reference bindings: ${requiredBindings.join(", ")})`,
		);
	}

	return result;
}
