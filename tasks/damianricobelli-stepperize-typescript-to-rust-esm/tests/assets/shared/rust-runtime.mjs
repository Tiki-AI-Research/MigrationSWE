// Instrument compiled Rust at its loading boundary, before ESM captures any
// bindings. Support both Node-API and synchronous WebAssembly; neither ABI is
// part of the public contract. Stable wrappers also cover native methods on a
// returned StepMap, without changing the identity of caller-owned values.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";
import { isDeepStrictEqual } from "node:util";
import { coreRuntimeEntry } from "./package-entry.mjs";
import { assertRustResponsibilities } from "./rust-responsibilities.mjs";

const sessions = new Map();
let sequence = 0;

export function rustRuntime(root) {
	root = resolve(root);
	if (sessions.has(root)) return sessions.get(root);
	const counts = new Map();
	const stubs = new Map();
	const wrapped = new WeakSet();
	const artifacts = new Set();
	let compiledFunctions = 0;
	let executions = 0;
	const ownNativeMethods = (object, prefix) => {
		if (!object || typeof object !== "object" || Array.isArray(object)) return object;
		const descriptors = Object.getOwnPropertyDescriptors(object);
		const nativeMethods = Object.entries(descriptors).filter(([, descriptor]) =>
			typeof descriptor.value === "function" &&
			!wrapped.has(descriptor.value) &&
			/\[native code\]/.test(Function.prototype.toString.call(descriptor.value)));
		// Some valid Node-API addons expose non-configurable, non-writable methods.
		// Those cannot be instrumented in place. Mirror the export object with the
		// same descriptors so the verifier can wrap the loading boundary without
		// imposing descriptor flags that are not part of the public contract.
		const target = nativeMethods.some(([, descriptor]) => !descriptor.configurable && !descriptor.writable)
			? Object.create(Object.getPrototypeOf(object))
			: object;
		for (const [key, descriptor] of nativeMethods) {
			const fn = descriptor.value;
			const name = `${prefix}.${key}`;
			counts.set(name, counts.get(name) ?? 0);
			compiledFunctions += 1;
			const wrapper = function (...args) {
				executions += 1;
				counts.set(name, (counts.get(name) ?? 0) + 1);
				if (stubs.has(name)) return stubs.get(name)(...args);
				return ownNativeMethods(Reflect.apply(fn, this, args), `${name}.return`);
			};
			wrapped.add(wrapper);
			descriptors[key] = { ...descriptor, value: wrapper };
		}
		if (target !== object) Object.defineProperties(target, descriptors);
		else {
			for (const [key] of nativeMethods) Object.defineProperty(target, key, descriptors[key]);
		}
		return target;
	};
	const originalDlopen = process.dlopen;
	process.dlopen = function (module, filename, ...rest) {
		const result = originalDlopen.call(this, module, filename, ...rest);
		if (resolve(String(filename)).startsWith(root + sep)) {
			artifacts.add(resolve(String(filename)));
			module.exports = ownNativeMethods(module.exports, String(filename));
		}
		return result;
	};
	// A WebAssembly exports object has immutable properties. Clone its property
	// table, preserving Memory/Global identities, and shadow the instance getter.
	const instrumentInstance = (instance) => {
		const exports = Object.fromEntries(Object.entries(instance.exports));
		ownNativeMethods(exports, "wasm");
		Object.defineProperty(instance, "exports", { value: exports, configurable: true });
		for (const directory of [resolve(root, "packages/core"), resolve(root, "node_modules/@stepperize/core")]) {
			try { artifacts.add(coreRuntimeEntry(root, { packageDirectory: directory })); break; }
			catch { /* A standalone instrumentation probe may have no package. */ }
		}
		return instance;
	};
	const OriginalInstance = WebAssembly.Instance;
	WebAssembly.Instance = new Proxy(OriginalInstance, {
		construct(target, args) {
			return instrumentInstance(Reflect.construct(target, args));
		},
	});
	for (const key of ["instantiate", "instantiateStreaming"]) {
		const original = WebAssembly[key];
		if (!original) continue;
		WebAssembly[key] = async function (...args) {
			const result = await original.apply(this, args);
			if (result instanceof OriginalInstance) return instrumentInstance(result);
			return { ...result, instance: instrumentInstance(result.instance) };
		};
	}
	// Cover an addon already loaded by a behavior probe, including direct dlopen
	// users which do not put their module in require.cache (the hook covers those).
	const require = createRequire(import.meta.url);
	for (const [file, module] of Object.entries(require.cache)) {
		if (file.endsWith(".node") && resolve(file).startsWith(root + sep)) {
			artifacts.add(file);
			module.exports = ownNativeMethods(module.exports, file);
		}
	}
	const session = {
		counts, artifacts,
		get nativeCodeCount() { return compiledFunctions; },
		get executions() { return executions; },
		get names() { return [...counts.keys()]; },
		total: () => [...counts.values()].reduce((a, b) => a + b, 0),
		stub(name, fn) { stubs.set(name, fn); },
		reinstall() {
			stubs.clear();
			for (const key of counts.keys()) counts.set(key, 0);
		},
		restore() { stubs.clear(); },
	};
	sessions.set(root, session);
	return session;
}

export async function importRustCore(root) {
	rustRuntime(root);
	return import(`${pathToFileURL(coreRuntimeEntry(root)).href}?rust-probe=${++sequence}`);
}

export async function assertOperationDelegates(root, invoke, label) {
	const binding = rustRuntime(root);
	binding.reinstall();
	const baseline = await invoke(await importRustCore(root));
	const used = [...binding.counts].filter(([, n]) => n > 0).map(([name]) => name);
	assert.ok(binding.nativeCodeCount > 0 && used.length > 0, `${label} must execute compiled Rust`);
	// Invalid ABI-shaped stubs can throw inside an entirely faithful facade.
	// Only the candidate invocation is caught; assertion failures are not caught.
	const changedUnder = async (value) => {
		binding.reinstall();
		for (const name of used) binding.stub(name, () => value);
		let result;
		try { result = await invoke(await importRustCore(root)); }
		catch { return; }
		assert.ok(!isDeepStrictEqual(result, baseline), `${label} must derive its result from Rust return values`);
	};
	try {
		await changedUnder({ rustDelegationProbe: label });
		for (const value of [undefined, null, false, 0, -1, "", NaN]) {
			if (!isDeepStrictEqual(value, baseline)) await changedUnder(value);
		}
		for (const name of used) {
			binding.reinstall();
			binding.stub(name, () => { throw new Error(`injected Rust failure: ${name}`); });
			let threw = false;
			try { await invoke(await importRustCore(root)); } catch { threw = true; }
			assert.ok(threw, `${label} must propagate Rust failures instead of using a source fallback`);
		}
	} finally { binding.reinstall(); }
}

export async function assertCoreOperationsDelegate(root) {
	assertRustResponsibilities(root);
	const steps = [{ id: "a" }, { id: "b" }];
	const schemaSteps = [{ id: "a", schema: { "~standard": { version: 1, vendor: "probe", validate: (value) => ({ value }) } } }];
	const operations = [
		["ids", (c) => c.createStepMap(steps).ids],
		...["get", "at", "indexOf", "has", "first", "last", "next", "prev", "neighbors"].map((name) => [
			name, (c) => c.createStepMap(steps)[name](name === "at" ? 1 : ["next", "neighbors"].includes(name) ? "a" : "b"),
		]),
		["getInitialStepIndex", (c) => c.getInitialStepIndex(steps, "b")],
		["parseStep", (c) => c.parseStep(steps, "b")],
		["getInitialData", (c) => c.getInitialData({ x: 1 })],
		["getStepStatus", (c) => c.getStepStatus(steps, 1, "a")],
		["getStepStatuses", (c) => c.getStepStatuses(steps, 1)],
		["matchStep", (c) => c.matchStep(steps, "b", { b: (step) => step.id })],
		["validateStep", (c) => c.validateStep(schemaSteps, "a", "x")],
	];
	for (const [label, invoke] of operations) await assertOperationDelegates(root, invoke, label);
}
