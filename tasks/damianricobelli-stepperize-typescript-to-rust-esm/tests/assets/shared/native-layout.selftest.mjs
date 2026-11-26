// Build-time regression guard for the native capability helpers: proves the
// instrumentation is ABI-name-agnostic, that publication follows
// `package.json.files`, and that the native-stripping copy removes every addon
// wherever it lives.  Run from tests/Dockerfile; exits non-zero on regression.
import assert from "node:assert/strict";
import { chmodSync, mkdirSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import {
	assertPropagatesOnNativeFailure,
	assertTracksNativeReturn,
	copyCorePackageWithoutNative,
	copyPublishedCoreFiles,
	countDisabledTestMarkers,
	decodeStringConstants,
	instrumentNativeExports,
	rustRuntime,
	assertOperationDelegates,
	assertRustBuildWiring,
	toolchainPinsRust185,
	rustBuildOnly,
	productionSourceFiles,
} from "./native-layout.mjs";
import { assertStatusRecord } from "./batches/BATCH-F2P-0002/checks.mjs";

const sandbox = join(tmpdir(), `native-layout-selftest-${process.pid}`);
const core = join(sandbox, "core");
rmSync(sandbox, { recursive: true, force: true });
mkdirSync(join(core, "dist"), { recursive: true });
mkdirSync(join(core, "native"), { recursive: true });
mkdirSync(join(core, "src"), { recursive: true });
writeFileSync(
	join(core, "package.json"),
	JSON.stringify({ name: "@stepperize/core", files: ["dist", "native/*.node"] }),
);
writeFileSync(join(core, "dist/index.js"), "export const x = 1;\n");
writeFileSync(join(core, "dist/addon.node"), "binary");
writeFileSync(join(core, "native/stepperize.node"), "binary");
writeFileSync(join(core, "src/utils.ts"), "// not published\n");

try {
	// No compiler is needed: this tiny WASM module exports integer addition.
	// Cover embedded WASM, immutable exports, stable captured bindings and stubs.
	const runtimeRoot = join(sandbox, "wasm-root");
	const wasmBinding = rustRuntime(runtimeRoot);
	const wasmBytes = new Uint8Array([0,97,115,109,1,0,0,0,1,7,1,96,2,127,127,1,127,3,2,1,0,7,7,1,3,97,100,100,0,0,10,9,1,7,0,32,0,32,1,106,11]);
	const wasm = new WebAssembly.Instance(new WebAssembly.Module(wasmBytes)).exports;
	const add = wasm.add;
	assert.equal(add(1, 2), 3);
	assert.ok(wasmBinding.total() > 0);
	wasmBinding.reinstall();
	wasmBinding.stub("wasm.add", () => 17);
	assert.equal(add(1, 2), 17, "captured WASM bindings must remain instrumented");
	wasmBinding.restore();
	assert.equal(add(1, 2), 3);

	// Simulate the loading boundary using built-in native functions. This tests
	// returned native methods and cached direct exports without building an addon.
	const nativeRoot = join(sandbox, "node-root");
	mkdirSync(join(nativeRoot, "packages/core/dist"), { recursive: true });
	writeFileSync(join(nativeRoot, "packages/core/package.json"), JSON.stringify({ name: "@stepperize/core", type: "module", main: "./dist/index.js" }));
	const oldDlopen = process.dlopen;
	process.dlopen = (module) => {
		module.exports = {};
		Object.defineProperties(module.exports, {
			create: { value: Object.assign.bind(null, { get: Math.abs }), enumerable: true },
			lookup: { value: Math.abs, enumerable: true },
		});
	};
	const nativeBinding = rustRuntime(nativeRoot);
	const module = {};
	process.dlopen(module, join(nativeRoot, "addon.node"));
	process.dlopen = oldDlopen;
	globalThis.__qualityNative = module.exports;
	writeFileSync(join(nativeRoot, "packages/core/dist/index.js"), `
const native = globalThis.__qualityNative;
export const createStepMap = native.create;
export function nudge() { native.lookup(-3); return 3; }
export function fallback() { try { return native.lookup(-3); } catch { return 3; } }
`);
	await assertOperationDelegates(nativeRoot, (c) => c.createStepMap([]).get(-3), "returned-method");
	await assert.rejects(assertOperationDelegates(nativeRoot, (c) => c.nudge(), "nudge"), /derive/);
	await assert.rejects(assertOperationDelegates(nativeRoot, (c) => c.fallback(), "fallback"), /propagate/);
	assert.ok(nativeBinding.nativeCodeCount > 0);
	assert.equal(Object.getOwnPropertyDescriptor(module.exports, "lookup")?.configurable, false,
		"instrumentation must preserve immutable native export descriptors");
	delete globalThis.__qualityNative;

	// A neutral build.mjs filename is valid when it invokes cargo build.
	const buildRoot = join(sandbox, "build-root");
	const buildCore = join(buildRoot, "packages/core");
	mkdirSync(join(buildCore, "scripts"), { recursive: true });
	mkdirSync(join(buildCore, "rust/src"), { recursive: true });
	writeFileSync(join(buildCore, "package.json"), JSON.stringify({ scripts: { build: "node scripts/build.mjs", test: "vitest" } }));
	writeFileSync(join(buildCore, "scripts/build.mjs"), 'spawnSync("cargo", ["+1.85.0", "build"]);');
	writeFileSync(join(buildCore, "rust/Cargo.toml"), '[package]\nname="probe"\nrust-version="1.85.0"\n');
	writeFileSync(join(buildCore, "rust/src/lib.rs"), 'pub fn probe() {}');
	assertRustBuildWiring(buildRoot);
	// tsup configuration hooks and their module-relative imports are build code.
	writeFileSync(join(buildCore, "package.json"), JSON.stringify({ scripts: { build: "tsup", test: "vitest" } }));
	writeFileSync(join(buildCore, "tsup.config.ts"), 'import { buildNative } from "./scripts/hooks.mjs"; buildNative();');
	writeFileSync(join(buildCore, "scripts/hooks.mjs"), 'export { buildNative } from "./native.mjs";');
	writeFileSync(join(buildCore, "scripts/native.mjs"), 'export const buildNative = () => spawnSync("cargo", ["+1.85.0", "build"]);');
	assertRustBuildWiring(buildRoot);
	writeFileSync(join(buildCore, "scripts/native.mjs"), 'export const buildNative = () => console.log("done");');
	assert.throws(() => assertRustBuildWiring(buildRoot), /compiler/);
	writeFileSync(join(buildCore, "package.json"), JSON.stringify({ scripts: { build: "node scripts/build.mjs", test: "vitest" } }));
	// Direct rustc compilation is also a Rust crate, with no Cargo manifest.
	rmSync(join(buildCore, "rust/Cargo.toml"));
	writeFileSync(join(buildCore, "scripts/build.mjs"), 'spawnSync("rustc", ["+1.85.0", "--crate-type=cdylib", "rust/src/lib.rs"]);');
	assertRustBuildWiring(buildRoot);
	// An implicit compiler must be the exact required release, not just satisfy
	// an MSRV declaration. Use a version-only executable; no Rust install/build.
	const compilerBin = join(sandbox, "compiler-bin");
	mkdirSync(compilerBin);
	const rustc = join(compilerBin, "rustc");
	const setCompilerVersion = (version) => {
		writeFileSync(rustc, `#!/bin/sh\nprintf '%s\\n' 'rustc ${version} (selftest)'\n`);
		chmodSync(rustc, 0o755);
	};
	const inheritedPath = process.env.PATH;
	try {
		process.env.PATH = `${compilerBin}${delimiter}${inheritedPath ?? ""}`;
		writeFileSync(join(buildCore, "scripts/build.mjs"), 'spawnSync("rustc", ["--crate-type=cdylib", "rust/src/lib.rs"]);');
		setCompilerVersion("1.85.0");
		assertRustBuildWiring(buildRoot);
		setCompilerVersion("1.86.0");
		assert.equal(toolchainPinsRust185(buildRoot), false);
		assert.throws(() => assertRustBuildWiring(buildRoot), /1\.85\.0/);
		setCompilerVersion("1.85.0");
		writeFileSync(join(buildCore, "scripts/build.mjs"), 'spawnSync("rustc", ["+1.86.0", "rust/src/lib.rs"]);');
		assert.equal(toolchainPinsRust185(buildRoot), false, "explicit wrong selectors must not use the default");
		writeFileSync(join(buildCore, "scripts/build.mjs"), 'spawnSync("cargo", ["build"]);');
		writeFileSync(join(buildCore, "rust/Cargo.toml"), '[package]\nname="probe"\nrust-version="1.85.0"\n');
		assertRustBuildWiring(buildRoot);
		setCompilerVersion("1.86.0");
		assert.equal(toolchainPinsRust185(buildRoot), false, "MSRV does not prove the compiler version");
	} finally {
		if (inheritedPath === undefined) delete process.env.PATH;
		else process.env.PATH = inheritedPath;
	}
	writeFileSync(join(buildCore, "scripts/build.mjs"), 'console.log("build complete");');
	assert.throws(() => assertRustBuildWiring(buildRoot), /compiler/);
	assert.ok(rustBuildOnly('cp.execFileSync("cargo", ["build", "--release"]);'));
	assert.ok(!rustBuildOnly('cp.execFileSync("cargo", ["build"]); cp.execFileSync("node", ["legacy.js"]);'));
	assert.ok(!rustBuildOnly('cp.execFileSync("cargo", ["build"]); cp[method]("node", ["legacy.js"]);'));

	// Isolated test processes are allowed; production imports into test files
	// remain subject to the production fallback scan.
	const scanRoot = join(sandbox, "scan-root");
	const scanCore = join(scanRoot, "packages/core");
	mkdirSync(join(scanCore, "src/tests"), { recursive: true });
	const facade = join(scanCore, "src/utils.ts");
	const testFile = join(scanCore, "src/tests/package.test.ts");
	writeFileSync(facade, 'export const helper = native.helper;');
	writeFileSync(testFile, 'import { spawnSync } from "node:child_process";');
	assert.ok(!productionSourceFiles(scanRoot).includes(testFile));
	writeFileSync(facade, 'export { helper } from "./tests/package.test";');
	assert.ok(productionSourceFiles(scanRoot).includes(testFile), "test directory must not hide production dependencies");

	const statusEntries = (steps, current) => steps.map((step, index) =>
		[step.id, index === current ? "active" : index < current ? "previous" : "upcoming"]);
	assertStatusRecord({ getStepStatuses: (steps, current) => Object.fromEntries(statusEntries(steps, current)) });
	assert.throws(() => assertStatusRecord({
		getStepStatuses: (steps, current) => {
			const result = {};
			for (const [id, status] of statusEntries(steps, current)) result[id] = status;
			return result;
		},
	}), /status record must contain every step id/);

	// Names never appear in the reference ABI; every function export is wrapped.
	const addon = { ids: (value) => value, next: (value) => value, version: 1 };
	const binding = instrumentNativeExports(addon);
	assert.deepEqual(binding.names.sort(), ["ids", "next"]);
	addon.ids(1);
	addon.next(2);
	addon.ids(3);
	assert.equal(binding.total(), 3, "instrumentNativeExports must count every export");
	binding.restore();
	assert.equal(addon.ids("kept"), "kept", "restore must reinstate the original export");

	// reinstall() must both clear stubs and re-wrap every export for counting.
	const readd = { run: (value) => value };
	const rebinding = instrumentNativeExports(readd);
	rebinding.stub("run", () => "stubbed");
	assert.equal(readd.run(1), "stubbed", "stub must replace the export");
	rebinding.reinstall();
	assert.equal(readd.run(2), 2, "reinstall must drop the stub");
	rebinding.reinstall();
	readd.run(3);
	assert.equal(rebinding.total(), 1, "reinstall must keep counting calls");
	rebinding.restore();

	// A native failure must propagate; a value returned despite the addon
	// throwing is a silent TypeScript fallback and must be rejected.
	await assertPropagatesOnNativeFailure(async () => {
		throw new Error("native blew up");
	}, "async-throwing-op");
	await assertPropagatesOnNativeFailure(() => {
		throw new Error("sync native failure");
	}, "sync-throwing-op");
	let silentFallbackRejected = false;
	try {
		await assertPropagatesOnNativeFailure(async () => 42, "silent-fallback-op");
	} catch {
		silentFallbackRejected = true;
	}
	assert.ok(silentFallbackRejected, "an operation returning a value after native failure must be rejected");

	// A fallback keyed on a native *return value* (not a thrown error) must be
	// rejected, while a faithful facade that returns the native value must pass.
	// `fallback.value` selects the native return that triggers a hidden TS
	// recompute (which returns the real baseline, masking the delegation).
	const nativeFidelityHarness = ({ fallback } = {}) => {
		let stubbed = null;
		return {
			used: ["stepIndexOf"],
			baseline: 1,
			stub: (name, implementation) => {
				stubbed = { name, implementation };
			},
			reset: () => {
				stubbed = null;
			},
			invokeFresh: async () => {
				const found = stubbed && stubbed.name === "stepIndexOf" ? stubbed.implementation() : 1;
				if (fallback && Object.is(found, fallback.value)) return 1;
				return found;
			},
		};
	};
	for (const value of [-1, undefined, null]) {
		let rejected = false;
		try {
			await assertTracksNativeReturn({
				...nativeFidelityHarness({ fallback: { value } }),
				label: `value-keyed-fallback-${String(value)}`,
			});
		} catch {
			rejected = true;
		}
		assert.ok(rejected, `a fallback triggered by a native returning ${String(value)} must be rejected`);
	}
	await assertTracksNativeReturn({
		...nativeFidelityHarness(),
		label: "faithful-facade",
	});

	// Respelled banned tokens must be reconstructed from their fragments.
	const folded = decodeStringConstants(
		'const a = ["execFile", "Sync"].join(""); const b = "child" + "_process";',
	);
	assert.ok(folded.includes("execFileSync"), "array join must fold to execFileSync");
	assert.ok(folded.includes("child_process"), "plus must fold to child_process");
	assert.ok(
		!decodeStringConstants("const x = steps.find((s) => s.id === id);").some((v) => v.includes("execFileSync")),
		"unrelated source must not fold to a banned token",
	);

	// Disabled-test counting must follow the capability through brackets and
	// renamed aliases, and must stay silent for ordinary code.
	const renamed = countDisabledTestMarkers(
		'const aliasIt = it;\naliasIt["skip"]("x", () => {});\nit["only"]("y", () => {});\nit.skip("z", () => {});',
	);
	assert.equal(renamed.skipped, 2, "literal and alias-bracket skips must be counted");
	assert.equal(renamed.focused, 1, "bracket-only focus must be counted");
	assert.deepEqual(
		countDisabledTestMarkers('const skip = 1;\nit("t", () => {});\n'),
		{ skipped: 0, focused: 0 },
		"ordinary code must not be counted as disabled tests",
	);

	// Publication copies exactly what `files` declares, at any location.
	const published = join(sandbox, "published");
	await copyPublishedCoreFiles(core, published);
	assert.ok(existsSync(join(published, "dist/addon.node")), "addon inside a published dir must ship");
	assert.ok(existsSync(join(published, "native/stepperize.node")), "globbed addon must ship");
	assert.ok(!existsSync(join(published, "src/utils.ts")), "unpublished sources must not ship");

	// Native removal strips every addon regardless of layout.
	const stripped = join(sandbox, "stripped");
	copyCorePackageWithoutNative(core, stripped);
	const remaining = [];
	const walk = (directory) => {
		for (const entry of readdirSync(directory, { withFileTypes: true })) {
			const path = join(directory, entry.name);
			if (entry.isDirectory()) walk(path);
			else if (path.endsWith(".node")) remaining.push(path);
		}
	};
	walk(stripped);
	assert.deepEqual(remaining, [], "every .node must be stripped for the no-native check");
	assert.ok(existsSync(join(stripped, "dist/index.js")), "non-native files must remain");

	process.stdout.write("native-layout selftest OK\n");
} finally {
	rmSync(sandbox, { recursive: true, force: true });
}
