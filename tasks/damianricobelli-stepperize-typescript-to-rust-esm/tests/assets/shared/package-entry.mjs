import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve, sep } from "node:path";

export function coreManifest(root) {
	return JSON.parse(readFileSync(resolve(root, "packages/core/package.json"), "utf8"));
}

function importTarget(value) {
	if (typeof value === "string") return value;
	if (Array.isArray(value)) return value.map(importTarget).find(Boolean);
	if (value && typeof value === "object") {
		for (const [condition, target] of Object.entries(value)) {
			if (["node", "node-addons", "import", "default"].includes(condition)) {
				const selected = importTarget(target);
				if (selected) return selected;
			}
		}
	}
	return undefined;
}

export function coreRuntimeEntry(root, { packageDirectory } = {}) {
	const directory = packageDirectory ? resolve(packageDirectory) : resolve(root, "packages/core");
	const manifest = JSON.parse(readFileSync(resolve(directory, "package.json"), "utf8"));
	assert.equal(manifest.name, "@stepperize/core", "the package name must remain @stepperize/core");
	assert.equal(manifest.type, "module", "core must remain an ESM package");
	const exports = manifest.exports;
	const target = exports === undefined ? manifest.main ?? "./index.js" :
		importTarget(exports && typeof exports === "object" && !Array.isArray(exports) && "." in exports ? exports["."] : exports);
	assert.ok(typeof target === "string", "the package root must have an import entry");
	if (exports !== undefined) assert.ok(target.startsWith("./"), "package exports must use a relative package target");
	const entry = resolve(directory, target);
	assert.ok(entry.startsWith(directory + sep), "the package root entry must stay inside core");
	// Node's legacy main extension/directory resolution applies only without exports.
	const resolved = exports === undefined ? createRequire(import.meta.url).resolve(entry) : entry;
	assert.ok(existsSync(resolved), `the package root import entry is missing: ${target}`);
	return resolved;
}
