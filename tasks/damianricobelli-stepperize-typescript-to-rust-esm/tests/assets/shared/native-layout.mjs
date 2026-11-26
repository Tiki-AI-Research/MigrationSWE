// Capability discovery for the migrated `@stepperize/core` Rust package.
//
// `instruction.md` only requires "new Rust crate sources, build and binding
// configuration, and the ESM facade" to live inside `packages/core`; it never
// names the reference solution's private files (`native/Cargo.toml`,
// `scripts/copy-native.mjs`, `src/native.ts`).  These helpers locate the
// migrated artefacts by *capability* instead of by private path so that any
// equivalent layout is accepted, while every behavioural / native-addon /
// anti-fallback assertion in the batches stays untouched.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { isDeepStrictEqual } from "node:util";
import { createRequire } from "node:module";
import { assertPublicTypeContract } from "./type-contract.mjs";
import { coreRuntimeEntry } from "./package-entry.mjs";
import { rustRuntime } from "./rust-runtime.mjs";
export { rustRuntime, importRustCore, assertOperationDelegates, assertCoreOperationsDelegate } from "./rust-runtime.mjs";

// Directory names that never contain candidate-authored migration sources:
// installed dependencies, build output (the verifier itself pre-creates
// `packages/core/native/target`), and VCS metadata.
const CRATE_SKIP_DIRS = new Set([
	"node_modules",
	"target",
	".git",
	"vendor",
	".cargo",
	".turbo",
	"coverage",
	".next",
	"build",
]);

// The addon may legitimately be emitted next to the crate (`native/*.node`) or
// into `dist/`, so build directories are *not* skipped when hunting the `.node`.
const ADDON_SKIP_DIRS = new Set(["node_modules", "target", ".git", "vendor", ".cargo", ".turbo", "coverage", ".next"]);

function listFiles(directory, skipDirs) {
	const found = [];
	let entries;
	try {
		entries = readdirSync(directory, { withFileTypes: true });
	} catch {
		return found;
	}
	for (const entry of entries) {
		if (entry.isDirectory()) {
			if (skipDirs.has(entry.name)) continue;
			found.push(...listFiles(join(directory, entry.name), skipDirs));
		} else if (entry.isFile()) {
			found.push(join(directory, entry.name));
		}
	}
	return found;
}

function coreDir(root) {
	return resolve(root, "packages/core");
}

function safeRead(file) {
	try {
		return readFileSync(file, "utf8");
	} catch {
		return "";
	}
}

/**
 * Locate the Rust crate that performs the migration, regardless of its
 * directory name (`native/`, `rust/`, `crate/`, ...).  A directory qualifies
 * when it contains a `Cargo.toml` next to at least one `.rs` source; a
 * `crate-type = ["cdylib"]` and a Rust 1.85 requirement rank candidates so the
 * real addon crate wins over incidental manifests.
 */
export function discoverCrate(root) {
	const manifests = listFiles(coreDir(root), CRATE_SKIP_DIRS).filter((file) => file.endsWith("Cargo.toml"));
	const candidates = manifests
		.map((manifestPath) => {
			const directory = dirname(manifestPath);
			const text = safeRead(manifestPath);
			const sources = listFiles(directory, CRATE_SKIP_DIRS).filter((file) => file.endsWith(".rs"));
			const score =
				(sources.length > 0 ? 2 : 0) +
				(/rust-version\s*=\s*"1\.85/.test(text) ? 1 : 0) +
				(/crate-type\s*=\s*\[[^\]]*cdylib/.test(text) ? 1 : 0);
			return { directory, manifestPath, text, sources, score };
		})
		.filter((candidate) => candidate.sources.length > 0 || candidate.score > 0)
		.sort((left, right) => right.score - left.score || left.manifestPath.localeCompare(right.manifestPath));
	if (candidates.length > 0) return candidates[0];
	// Rust can be compiled directly with rustc. Cargo manifests and a particular
	// crate directory layout are not part of the public migration contract.
	const sources = listFiles(coreDir(root), CRATE_SKIP_DIRS).filter((file) => file.endsWith(".rs"));
	if (sources.length > 0 && /\brustc\b/.test(buildSources(root))) {
		return { directory: coreDir(root), manifestPath: null, text: "", sources, score: 2 };
	}
	return null;
}

/** Locate a pinned Rust toolchain file anywhere under `packages/core`. */
export function discoverToolchain(root) {
	const files = listFiles(coreDir(root), CRATE_SKIP_DIRS);
	return files.find((file) => /(^|[/\\])rust-toolchain(\.toml)?$/.test(file)) ?? null;
}

/** Check Rust 1.85.0 selection, including the evaluator's installed default. */
export function toolchainPinsRust185(root) {
	const sources = buildSources(root);
	const selectors = [...sources.matchAll(/\+((?:\d+\.\d+(?:\.\d+)?(?:-[\w.-]+)?)|(?:stable|beta|nightly)(?:-[\w.-]+)?)/g)].map((match) => match[1]);
	if (selectors.length > 0) return selectors.every((selector) => selector === "1.85.0");
	const rustupRun = /\brustup\s+run\s+([^\s;'\"]+)/.exec(sources) ??
		(/\brustup\b/.test(sources) ? /["']run["']\s*,\s*["']([^"']+)["']/.exec(sources) : null);
	if (rustupRun) return rustupRun[1] === "1.85.0";
	const file = discoverToolchain(root);
	if (file) {
		const declaration = safeRead(file).trim();
		return declaration === "1.85.0" || /\bchannel\s*=\s*["']1\.85\.0["']/.test(declaration);
	}
	// An implicit toolchain is valid when it really resolves to the required
	// compiler. rust-version alone is only an MSRV and is not sufficient proof.
	// Explicit selectors must not be mistaken for the installed default.
	if (/\brustup\b[\s\S]*?["']run["']/.test(sources)) return false;
	const crate = discoverCrate(root);
	if (!crate) return false;
	const version = spawnSync("rustc", ["--version"], {
		cwd: crate.directory, encoding: "utf8", timeout: 10000, env: process.env,
	});
	return version.status === 0 && /^rustc 1\.85\.0\s/.test(version.stdout ?? "");
}

// Follow the build entry point's local scripts and pnpm script references.
// Script filenames (including a plain `build.mjs`) are not public contracts.
function buildSources(root) {
	const directory = coreDir(root);
	const manifest = JSON.parse(safeRead(join(directory, "package.json")) || "{}");
	const pending = [{ text: manifest.scripts?.build ?? "", base: directory, command: true }];
	const seen = new Set();
	const sources = [];
	const enqueueFile = (file) => {
		const path = resolve(file);
		if (path.startsWith(directory + "/") && existsSync(path)) {
			pending.push({ text: safeRead(path), base: dirname(path), command: false });
		}
	};
	for (let i = 0; i < pending.length; i += 1) {
		const { text, base, command } = pending[i];
		const key = JSON.stringify([base, command, text]);
		if (seen.has(key)) continue;
		seen.add(key);
		sources.push(text);
		for (const match of text.matchAll(/[\w./-]+\.(?:mjs|cjs|mts|cts|js|ts|sh)\b/g)) {
			enqueueFile(resolve(base, match[0]));
		}
		for (const match of text.matchAll(/\b(?:pnpm|npm)(?:\s+run)?\s+([\w:-]+)/g)) {
			if (manifest.scripts?.[match[1]]) {
				pending.push({ text: manifest.scripts[match[1]], base: directory, command: true });
			}
		}
		// tsup loads this configuration implicitly; a compiler invocation in its
		// configuration hooks is part of the build, even when the script is "tsup".
		if (command && /\btsup\b/.test(text)) {
			const explicit = /(?:--config(?:=|\s+)|-c\s+)["']?([\w./-]+)/.exec(text);
			if (explicit) enqueueFile(resolve(directory, explicit[1]));
			else {
				const config = ["ts", "cts", "mts", "js", "cjs", "mjs"]
					.map((extension) => join(directory, `tsup.config.${extension}`))
					.find((file) => existsSync(file));
				if (config) enqueueFile(config);
			}
		}
	}
	return sources.join("\n");
}

/** Scan production sources and their local imports, including imports into a
 * test directory. Standalone tests may spawn consumer processes; production
 * code cannot evade the fallback scan by importing such a file. */
export function productionSourceFiles(root) {
	const directory = coreDir(root);
	const code = new Set(listFiles(directory, ADDON_SKIP_DIRS)
		.filter((file) => /\.(?:[cm]?[jt]sx?|rs)$/.test(file)));
	const pending = [...code].filter((file) => {
		const path = relative(directory, file).replaceAll("\\", "/");
		return /^(?:src|dist)\//.test(path) &&
			!/(?:^|\/)(?:tests?|__tests__)(?:\/|$)|\.(?:test|spec)\.[cm]?[jt]sx?$/.test(path);
	});
	try {
		const entry = coreRuntimeEntry(root);
		if (code.has(entry)) pending.push(entry);
	} catch { /* Source-only build-wiring probes have no built package yet. */ }
	const found = new Set();
	for (let index = 0; index < pending.length; index += 1) {
		const file = pending[index];
		if (found.has(file)) continue;
		found.add(file);
		const text = safeRead(file);
		for (const match of text.matchAll(/\b(?:from\s*|import\s*(?:\(\s*)?|require\s*\(\s*)["'](\.[^"']+)["']/g)) {
			const base = resolve(dirname(file), match[1]);
			const candidates = [base, ...["ts", "mts", "cts", "tsx", "js", "mjs", "cjs", "jsx"]
				.flatMap((extension) => [`${base}.${extension}`, join(base, `index.${extension}`)])];
			if (/\.[cm]?js$/.test(base)) candidates.push(base.replace(/\.[cm]?js$/, ".ts"));
			const dependency = candidates.find((path) => code.has(path));
			if (dependency) pending.push(dependency);
		}
	}
	return [...found];
}

export function assertRustBuildWiring(root) {
	const manifest = JSON.parse(safeRead(join(coreDir(root), "package.json")) || "{}");
	assert.ok(typeof manifest.scripts?.build === "string" && manifest.scripts.build.trim(), "build script must remain available");
	assert.ok(typeof manifest.scripts?.test === "string" && manifest.scripts.test.trim(), "test script must remain available");
	const sources = buildSources(root);
	assert.ok(/\b(?:cargo|rustc|napi|wasm-pack)\b/.test(sources) && /\bbuild\b/.test(sources), "build must invoke the Rust compiler through its script graph");
	assert.ok(discoverCrate(root)?.sources.length > 0, "a Rust crate with sources must exist inside packages/core");
	assert.ok(toolchainPinsRust185(root), "build must use Rust 1.85.0");
}

export function discoverRuntimeArtifacts(root) {
	return [...new Set([...discoverAddons(root), ...rustRuntime(root).artifacts])];
}

/**
 * Locate the built Node addon(s) anywhere under `packages/core`.  Sorted for a
 * deterministic pick; excludes cargo `target/` build trees and dependencies.
 */
export function discoverAddons(root) {
	return listFiles(coreDir(root), ADDON_SKIP_DIRS)
		.filter((file) => file.endsWith(".node"))
		.sort();
}

/** Convenience wrapper: does the package ship a Rust crate plus a built addon? */
export function hasRustMigration(root) {
	return Boolean(discoverCrate(root)) && discoverRuntimeArtifacts(root).length > 0;
}

export { existsSync };

// ---------------------------------------------------------------------------
// Constant-string reconstruction shared by the F2P anti-fallback token scans.
//
// A banned token (`execFileSync`, `child_process`, ...) must not be defeatable
// by respelling it: adjacent string fragments (`["execFile","Sync"].join("")`,
// `"exec"+"FileSync"`, `String.fromCharCode(...)`) and `const` forwarding all
// produce the same value the literal scan cannot see.  This folds fully-constant
// expressions back to their values; runtime/unknown expressions stay opaque, so
// it never invents a token the source cannot produce.  Mirrors the Python
// `residue._decode_string_constants` used by the hard gate.
// ---------------------------------------------------------------------------

const _UNKNOWN = Symbol("fold-unknown");
const _FOLD_TOKEN =
	/\s+|\/\/[^\n]*|\/\*[\s\S]*?\*\/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`|0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?|[A-Za-z_$][A-Za-z0-9_$]*|[\[\](){}.,;:+\-*/%=<>!&|?]/g;
const _UNESCAPE = { n: "\n", t: "\t", r: "\r", 0: "\0", "\\": "\\", "'": "'", '"': '"', "`": "`" };

function unescapeJs(body) {
	let out = "";
	for (let index = 0; index < body.length; index += 1) {
		const character = body[index];
		if (character === "\\" && index + 1 < body.length) {
			const next = body[index + 1];
			if (next in _UNESCAPE) {
				out += _UNESCAPE[next];
				index += 1;
				continue;
			}
			if (next === "x" || next === "u") {
				const digits = next === "x" ? 2 : 4;
				const hex = body.slice(index + 2, index + 2 + digits);
				if (/^[0-9a-fA-F]+$/.test(hex)) {
					out += String.fromCharCode(parseInt(hex, 16));
					index += 1 + digits;
					continue;
				}
			}
			out += next;
			index += 1;
			continue;
		}
		out += character;
	}
	return out;
}

function tokenizeSource(source) {
	const tokens = [];
	let match;
	_FOLD_TOKEN.lastIndex = 0;
	while ((match = _FOLD_TOKEN.exec(source))) {
		const value = match[0];
		if (/^\s+$/.test(value) || value.startsWith("//") || value.startsWith("/*")) continue;
		const quote = value[0];
		if (quote === "'" || quote === '"' || quote === "`") {
			const body = value.slice(1, -1);
			if (quote === "`" && body.includes("${")) {
				tokens.push(["opaque", ""]);
				continue;
			}
			tokens.push(["string", unescapeJs(body)]);
			continue;
		}
		if (/^[0-9]/.test(value)) {
			tokens.push(["number", value]);
			continue;
		}
		if (/^[A-Za-z_$]/.test(value)) {
			tokens.push(["ident", value]);
			continue;
		}
		tokens.push(["punct", value]);
	}
	return tokens;
}

const peek = (tokens, index) => (index >= 0 && index < tokens.length ? tokens[index] : ["", ""]);
const isPunct = (tokens, index, value) => {
	const token = peek(tokens, index);
	return token[0] === "punct" && token[1] === value;
};

function parseSequence(tokens, index, env, closer) {
	const items = [];
	for (;;) {
		if (isPunct(tokens, index, closer)) return [items, index + 1];
		const [item, next] = parseConcat(tokens, index, env);
		index = next;
		if (item === _UNKNOWN) return [_UNKNOWN, index];
		items.push(item);
		if (isPunct(tokens, index, ",")) {
			index += 1;
			continue;
		}
		if (isPunct(tokens, index, closer)) return [items, index + 1];
		return [_UNKNOWN, index];
	}
}

function parsePrimary(tokens, index, env) {
	const [kind, value] = peek(tokens, index);
	if (kind === "string") return [value, index + 1];
	if (kind === "punct" && value === "[") return parseSequence(tokens, index + 1, env, "]");
	if (kind === "punct" && value === "(") {
		const [inner, next] = parseConcat(tokens, index + 1, env);
		return [inner, isPunct(tokens, next, ")") ? next + 1 : next];
	}
	if (kind === "ident") {
		if (
			value === "String" &&
			isPunct(tokens, index + 1, ".") &&
			peek(tokens, index + 2)[1] === "fromCharCode" &&
			isPunct(tokens, index + 3, "(")
		) {
			const codes = [];
			let cursor = index + 4;
			for (;;) {
				const [itemKind, itemValue] = peek(tokens, cursor);
				if (itemKind === "number") {
					codes.push(String.fromCharCode(parseInt(itemValue, 0)));
					cursor += 1;
				} else if (itemKind === "string" && itemValue.length === 1) {
					codes.push(itemValue);
					cursor += 1;
				} else {
					return [_UNKNOWN, cursor];
				}
				if (isPunct(tokens, cursor, ",")) {
					cursor += 1;
					continue;
				}
				break;
			}
			if (isPunct(tokens, cursor, ")")) cursor += 1;
			return [codes.join(""), cursor];
		}
		if (Object.prototype.hasOwnProperty.call(env, value)) return [env[value], index + 1];
		return [_UNKNOWN, index + 1];
	}
	return [_UNKNOWN, index + 1];
}

function parsePostfix(tokens, index, env) {
	let [value, next] = parsePrimary(tokens, index, env);
	while (isPunct(tokens, next, ".")) {
		if (peek(tokens, next + 1)[1] === "join" && isPunct(tokens, next + 2, "(")) {
			const [separator, cursor] = parseConcat(tokens, next + 3, env);
			const after = isPunct(tokens, cursor, ")") ? cursor + 1 : cursor;
			if (!Array.isArray(value) || typeof separator !== "string") return [_UNKNOWN, after];
			if (!value.every((item) => typeof item === "string")) return [_UNKNOWN, after];
			return [value.join(separator), after];
		}
		return [_UNKNOWN, next + 1];
	}
	return [value, next];
}

function parseConcat(tokens, index, env) {
	let [value, next] = parsePostfix(tokens, index, env);
	while (isPunct(tokens, next, "+")) {
		const [rhs, after] = parsePostfix(tokens, next + 1, env);
		next = after;
		if (typeof value !== "string" || typeof rhs !== "string") return [_UNKNOWN, next];
		value = value + rhs;
	}
	return [value, next];
}

/** Every concrete string a source file can fold to (superset of raw literals). */
export function decodeStringConstants(source) {
	const tokens = tokenizeSource(source);
	const env = {};
	for (let pass = 0; pass < 6; pass += 1) {
		let changed = false;
		for (let index = 0; index < tokens.length - 2; index += 1) {
			const [kind, value] = tokens[index];
			if (kind !== "ident" || !["const", "let", "var"].includes(value)) continue;
			if (tokens[index + 1][0] !== "ident" || !isPunct(tokens, index + 2, "=")) continue;
			const name = tokens[index + 1][1];
			const [resolved] = parseConcat(tokens, index + 3, env);
			if (resolved === _UNKNOWN) continue;
			if (!Object.prototype.hasOwnProperty.call(env, name) || env[name] !== resolved) {
				env[name] = resolved;
				changed = true;
			}
		}
		if (!changed) break;
	}
	const found = new Set();
	for (const [kind, value] of tokens) {
		if (kind === "string") found.add(value);
	}
	for (let index = 0; index < tokens.length; index += 1) {
		const [resolved] = parseConcat(tokens, index, env);
		if (typeof resolved === "string") found.add(resolved);
		else if (Array.isArray(resolved)) {
			for (const item of resolved) if (typeof item === "string") found.add(item);
		}
	}
	return [...found];
}

// Compiler invocation is build/binding wiring, permitted by the task. Retain
// rejection of JS subprocesses, workers, RPC and computed/unknown commands.
export function rustBuildOnly(source) {
	if (/\[\s*[A-Za-z_$][\w$]*\s*\]\s*\(/.test(source)) return false;
	const calls = [...source.matchAll(/\b(execFileSync|execFile|spawnSync|spawn|execSync|exec|fork|Worker)\s*(?:['"]\s*\])?\s*\(/g)];
	return calls.length > 0 && calls.every((call) =>
		["execFileSync", "execFile", "spawnSync", "spawn"].includes(call[1]) &&
		/^\s*['"]cargo['"]\s*,\s*\[\s*['"]build['"]/.test(source.slice(call.index + call[0].length)),
	);
}

// ---------------------------------------------------------------------------
// Shared source scans (independent of the native ABI / artifact layout).
// ---------------------------------------------------------------------------

const _TEST_FRAMEWORK_BASES = new Set(["describe", "it", "test"]);

/**
 * Count test-framework skip/only *capabilities* in a test source, independent
 * of how the receiver is spelled.  Besides the literal ``.skip``/``.only``
 * forms this resolves aliases (``const aliasIt = it; aliasIt["skip"](...)``)
 * and bracket access, which the plain dot regex misses.  It is a strict
 * superset of the dot-form counts, so it never under-reports a disabled test.
 */
export function countDisabledTestMarkers(source) {
	const dotSkipped = (source.match(/\.(?:skip|todo)\b/g) ?? []).length;
	const dotFocused = (source.match(/\.only\b/g) ?? []).length;
	const bases = new Set(_TEST_FRAMEWORK_BASES);
	for (let pass = 0; pass < 8; pass += 1) {
		let changed = false;
		for (const match of source.matchAll(/([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)(?![.\w$[(])/g)) {
			const [, lhs, rhs] = match;
			if (bases.has(rhs) && !bases.has(lhs)) {
				bases.add(lhs);
				changed = true;
			}
		}
		if (!changed) break;
	}
	let bracketSkipped = 0;
	let bracketFocused = 0;
	for (const match of source.matchAll(/([A-Za-z_$][\w$]*)\s*\[\s*['"](skip|todo|only)['"]\s*\]\s*\(/g)) {
		if (!bases.has(match[1])) continue;
		if (match[2] === "only") bracketFocused += 1;
		else bracketSkipped += 1;
	}
	return { skipped: dotSkipped + bracketSkipped, focused: dotFocused + bracketFocused };
}

// ---------------------------------------------------------------------------
// Shared native instrumentation / publication helpers.
//
// `instruction.md` declares only the public ESM contract and "logic runs in
// Rust"; it never names the reference solution's private native ABI functions
// or the artifact location.  Every batch therefore proves two things by
// *capability* instead of by private name/path:
//   1. a public operation executed compiled Rust code -> instrument *all*
//      function exports of the loaded addon (no fixed name list);
//   2. the package actually ships that addon -> copy exactly the files
//      `package.json.files` publishes, then load them as a consumer.
// ---------------------------------------------------------------------------

/**
 * Wrap every function export of a loaded addon so callers can prove the public
 * operation executed native code, independent of the candidate's private ABI
 * names.  Returns counters plus `stub`/`restore` control.
 */
export function instrumentNativeExports(exportsObject) {
	const originals = new Map();
	const counts = new Map();
	let nativeCodeCount = 0;
	const wrap = (name, original) => {
		exportsObject[name] = function instrumentedNativeBinding(...args) {
			counts.set(name, (counts.get(name) ?? 0) + 1);
			return original.apply(this, args);
		};
	};
	for (const name of Object.keys(exportsObject ?? {})) {
		const original = exportsObject[name];
		if (typeof original !== "function") continue;
		if (/\[native code\]/.test(Function.prototype.toString.call(original))) {
			nativeCodeCount += 1;
		}
		originals.set(name, original);
		counts.set(name, 0);
		wrap(name, original);
	}
	return {
		names: [...originals.keys()],
		nativeCodeCount,
		counts,
		total: () => [...counts.values()].reduce((sum, value) => sum + value, 0),
		countFor: (name) => counts.get(name) ?? 0,
		reinstall() {
			for (const [name, original] of originals) {
				counts.set(name, 0);
				wrap(name, original);
			}
		},
		stub(name, implementation) {
			if (!counts.has(name)) counts.set(name, 0);
			exportsObject[name] = function stubbedNativeBinding(...args) {
				counts.set(name, (counts.get(name) ?? 0) + 1);
				return implementation(...args);
			};
		},
		restore() {
			for (const [name, original] of originals) {
				exportsObject[name] = original;
			}
		},
	};
}

/**
 * Prove a public operation *propagates* a native-addon failure instead of
 * silently recomputing the result in TypeScript.
 *
 * `invoke` runs the operation against a facade whose native bindings have been
 * replaced by throwing stubs.  The load-bearing-delegation proof (sentinel
 * replacement) only exercises the success path, so a candidate can keep a
 * hidden TypeScript fallback that activates *only* when the addon throws (a
 * try/catch that recomputes the result from the source data instead of letting
 * the error surface).  Such a
 * candidate returns a value here even though every native binding failed, which
 * is exactly the forbidden source fallback; it is rejected regardless of how
 * the fallback is spelled.  A facade that lets the addon error surface, or
 * rethrows it after adding context, is accepted because the error still
 * propagates.
 */
export async function assertPropagatesOnNativeFailure(invoke, label) {
	let returned = false;
	try {
		await invoke();
		returned = true;
	} catch {
		return;
	}
	if (returned) {
		throw new Error(
			`${label} must propagate a native-addon failure instead of recomputing the result in TypeScript`,
		);
	}
}

// Degenerate return values a native binding may use to signal "not found" or
// "no result".  A hidden TypeScript fallback can be triggered by *any specific
// return value* (e.g. `if (native.stepIndexOf(...) === -1) { <TS recompute> }`)
// rather than by a thrown error, so the error-propagation proof alone cannot
// see it.  These are the sentinels a lookup/aggregate helper legitimately
// produces on a miss, and therefore the values a value-keyed fallback branches
// on.  `true` is deliberately excluded: it is a *positive* result (e.g.
// `has`), and pinning a native to it would make a faithful result coincide with
// the real baseline.
export const NATIVE_DEGENERATE_RETURNS = Object.freeze([
	undefined,
	null,
	false,
	0,
	-1,
	"",
	Number.NaN,
]);

function describeReturn(value) {
	if (value === undefined) return "undefined";
	if (typeof value === "number" && Number.isNaN(value)) return "NaN";
	return JSON.stringify(value);
}

/**
 * Prove a public operation's result is *driven by* the native return values
 * rather than recomputed in TypeScript.
 *
 * The sentinel proof (a single non-primitive stub) and the throwing-stub proof
 * both pass a facade that keeps a hidden TypeScript fallback keyed on a native
 * *return value*: the sentinel is never `=== -1`, and the throwing stub makes
 * the error surface so the fallback never runs.  This sweep pins every native
 * binding the operation actually used to each degenerate return value in turn
 * and requires the operation's result to change accordingly.  A faithful facade
 * (whose only source of truth is the native return) yields a degenerate result
 * that differs from the real baseline; a value-keyed fallback recomputes from
 * the input and therefore reproduces the baseline, which is rejected.  A stub
 * that makes the operation throw is accepted: the throwing-stub proof already
 * guarantees failures propagate, so a throw here is not a fallback.
 */
export async function assertTracksNativeReturn({ used, stub, reset, invokeFresh, baseline, label }) {
	for (const value of NATIVE_DEGENERATE_RETURNS) {
		if (isDeepStrictEqual(value, baseline)) continue;
		// Pin *every* native binding the operation used to the same degenerate
		// value, so each contributing call is constant.  Stubbing one at a time
		// would leave an unrelated real call (e.g. the eager `stepIds` inside
		// `createStepMap`) free to reproduce the baseline and cause a false
		// failure on a faithful facade.
		reset();
		for (const name of used) stub(name, () => value);
		let stubbed;
		try {
			stubbed = await invokeFresh();
		} catch {
			continue;
		} finally {
			reset();
		}
		assert.notDeepEqual(
			stubbed,
			baseline,
			`${label} must derive its result from the native return values (natives ${used.join(", ")} were pinned to ${describeReturn(value)}), not recompute it in TypeScript`,
		);
	}
}

/**
 * Copy exactly the files declared by `package.json.files` (including
 * `parent/*.ext` globs) into `destination`, preserving relative paths, so a
 * consumer sees the same artifact a published package would expose.
 */
export async function copyPublishedCoreFiles(coreDirectory, destination) {
	const manifest = JSON.parse(readFileSync(join(coreDirectory, "package.json"), "utf8"));
	// Use npm's own packlist implementation (including brace/glob patterns,
	// exclusions, ignores, mandatory files and bundled dependencies). No scripts
	// or network are involved, and candidate node_modules do not supply the code.
	const npmRequire = createRequire(resolve(dirname(process.execPath), "../lib/node_modules/npm/package.json"));
	const Arborist = npmRequire("@npmcli/arborist");
	const packlist = npmRequire("npm-packlist");
	const tree = await new Arborist({ path: coreDirectory }).loadActual();
	const files = await packlist(tree);
	mkdirSync(destination, { recursive: true });
	for (const file of files) {
		assert.ok(!file.split(/[/\\]/).includes("..") && !file.startsWith("/"), "npm packlist must stay inside the package");
		const target = join(destination, file);
		mkdirSync(dirname(target), { recursive: true });
		cpSync(join(coreDirectory, file), target);
	}
	return manifest;
}

/**
 * Copy the whole built package (minus dependencies / cargo build output) and
 * strip every addon, wherever the candidate emitted it, so "the package must
 * fail without its native artifact" is independent of the artifact location.
 */
export function copyCorePackageWithoutNative(coreDirectory, destination) {
	mkdirSync(destination, { recursive: true });
	for (const entry of readdirSync(coreDirectory, { withFileTypes: true })) {
		if (entry.name === "node_modules" || entry.name === "target" || entry.name === ".git") continue;
		cpSync(join(coreDirectory, entry.name), join(destination, entry.name), { recursive: true });
	}
	for (const file of listFiles(destination, ADDON_SKIP_DIRS)) {
		if (file.endsWith(".node")) {
			rmSync(file, { force: true });
		}
	}
}

/**
 * Behavioural publication check: the files `package.json.files` declares must
 * be enough for a consumer to import `@stepperize/core` and *execute* the
 * native addon.  A `process.dlopen` hook installed before the dynamic import
 * proves the addon was actually loaded (not a TypeScript fallback), and the
 * returned ids prove the public contract still holds.  Location- and
 * name-agnostic: works whether the addon lives in `native/`, `dist/`, or the
 * package root, and whatever it exports.
 */
export async function assertPublishedPackageRunsNative(root, tag = "publish") {
	const coreDirectory = coreDir(root);
	const temporary = mkdtempSync(join(tmpdir(), `stepperize-publish-${tag}-`));
	try {
		const packageDirectory = join(temporary, "node_modules", "@stepperize", "core");
		await copyPublishedCoreFiles(coreDirectory, packageDirectory);
		assertPublicTypeContract(root, { packageDirectory });
		writeFileSync(join(temporary, "package.json"), JSON.stringify({ type: "module", private: true }));
		writeFileSync(
			join(temporary, "consumer.mjs"),
			[
				`import { rustRuntime } from ${JSON.stringify(new URL("./rust-runtime.mjs", import.meta.url).href)};`,
				`const runtime = rustRuntime(${JSON.stringify(temporary)});`,
				'const { createStepMap } = await import("@stepperize/core");',
				'const ids = createStepMap([{ id: "a" }]).ids;',
				"process.stdout.write(JSON.stringify({ ids, nativeLoads: [...runtime.artifacts], calls: runtime.total(), compiled: runtime.nativeCodeCount }));",
				"",
			].join("\n"),
		);
		const consumer = spawnSync(process.execPath, [join(temporary, "consumer.mjs")], {
			cwd: temporary,
			encoding: "utf8",
			env: process.env,
		});
		if (consumer.error) throw consumer.error;
		assert.equal(
			consumer.status,
			0,
			`published consumer failed: ${(consumer.stderr || consumer.stdout || "").trim()}`,
		);
		const payload = JSON.parse(consumer.stdout.trim());
		assert.deepEqual(payload.ids, ["a"], "published consumer must return the native step ids");
		assert.ok(
			payload.nativeLoads.length > 0 && payload.calls > 0 && payload.compiled > 0,
			"the published package must load and execute the Rust native addon",
		);
		return payload;
	} finally {
		rmSync(temporary, { recursive: true, force: true });
	}
}

// Disable compiled execution, including WASM embedded in JS. A fresh process
// prevents module caches or paths into the original checkout masking a fallback.
export async function assertFailsWithoutRust(root) {
	const temporary = mkdtempSync(join(tmpdir(), "stepperize-without-rust-"));
	try {
		const packageDirectory = join(temporary, "node_modules/@stepperize/core");
		await copyPublishedCoreFiles(coreDir(root), packageDirectory);
		writeFileSync(join(temporary, "package.json"), JSON.stringify({ type: "module" }));
		const script = join(temporary, "probe.mjs");
		writeFileSync(script, `
const unavailable = () => { throw new Error("compiled runtime unavailable"); };
process.dlopen = unavailable;
WebAssembly.Instance = new Proxy(WebAssembly.Instance, { construct: unavailable });
WebAssembly.instantiate = unavailable;
WebAssembly.instantiateStreaming = unavailable;
let failed = false;
try {
 const core = await import("@stepperize/core");
 core.createStepMap([{ id: "a" }]).next("a");
} catch { failed = true; }
if (!failed) throw new Error("public operations used a source fallback without Rust");
`);
		const result = spawnSync(process.execPath, [script], { cwd: temporary, encoding: "utf8", env: process.env });
		if (result.error) throw result.error;
		assert.equal(result.status, 0, result.stderr || "package must require compiled Rust execution");
	} finally { rmSync(temporary, { recursive: true, force: true }); }
}
