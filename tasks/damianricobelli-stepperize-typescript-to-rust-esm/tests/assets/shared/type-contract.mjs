import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { trustedTypeScript } from "./compiler.mjs";
import { TYPES_FILE_SHA256 } from "../fixtures/batches/BATCH-P2P-0002/constants.mjs";

const cache = new Map();
const contracts = [
	["Step", "'a', { extra: 1 }"], ["StepMap", "Steps"], ["StepStatus", ""],
	["ValidationResult", "number"], ["StandardSchemaV1", "string, number"],
	["BeforeStepChange", "Steps"], ["FlowData", "Steps"], ["InputOf", "Steps, 'a'"],
	["MaybePromise", "number"], ["NavigationPayload", ""], ["NavigationResult", ""],
	["OutputOf", "Steps, 'a'"], ["StepChangeContext", "Steps"], ["StepChangeValidator", "Steps, 'a'"],
	["StepDirection", ""], ["StepMatcher", "Steps"], ["Stepper", "Steps"], ["StepperData", "Steps"],
	["Get.Id", "Steps"], ["Get.StepById", "Steps, 'a'"], ["Get.Match", "Steps, number"],
	["StandardSchemaV1.Props", "string, number"], ["StandardSchemaV1.Result", "number"],
	["StandardSchemaV1.SuccessResult", "number"], ["StandardSchemaV1.FailureResult", ""],
	["StandardSchemaV1.Issue", ""], ["StandardSchemaV1.PathSegment", ""],
	["StandardSchemaV1.Types", "string, number"],
	["StandardSchemaV1.InferInput", "Reference.StandardSchemaV1<string, number>"],
	["StandardSchemaV1.InferOutput", "Reference.StandardSchemaV1<string, number>"],
];

function fingerprint(directory) {
	const hash = createHash("sha256");
	function walk(path) {
		for (const entry of readdirSync(path, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
			if (["node_modules", "target", ".git", "vendor"].includes(entry.name)) continue;
			const file = join(path, entry.name);
			if (entry.isDirectory()) walk(file);
			else if (entry.isFile() && (/\.[cm]?[jt]sx?$/.test(file) || entry.name === "package.json")) {
				hash.update(file).update(readFileSync(file));
			}
		}
	}
	walk(directory);
	return hash.digest("hex");
}

// Resolve the package as an external consumer does. No assumptions about the
// declaration filename, bundling, aliases, or re-export layout are made here.
export function assertPublicTypeContract(root, { packageDirectory } = {}) {
	const directory = packageDirectory ? resolve(packageDirectory) : resolve(root, "packages/core");
	const key = fingerprint(directory);
	const source = resolve(root, "packages/core/src/types.ts");
	assert.equal(createHash("sha256").update(readFileSync(source)).digest("hex"), TYPES_FILE_SHA256,
		"packages/core/src/types.ts must remain unchanged");
	if (cache.get(directory)?.key === key) return cache.get(directory).result;
	const ts = trustedTypeScript();
	const temporary = mkdtempSync(join(tmpdir(), "stepperize-types-"));
	try {
		mkdirSync(join(temporary, "node_modules/@stepperize"), { recursive: true });
		symlinkSync(directory, join(temporary, "node_modules/@stepperize/core"), "dir");
		const consumer = join(temporary, "consumer.mts");
		const use = (prefix, name, args) => `${prefix}.${name}${args ? `<${args}>` : ""}`;
		writeFileSync(consumer, [
			'import type * as Public from "@stepperize/core";',
			`import type * as Reference from ${JSON.stringify(source)};`,
			'import { createStepMap, getInitialStepIndex, parseStep, getInitialData, validateStep, getStepStatus, getStepStatuses, matchStep } from "@stepperize/core";',
			"type IsAny<T> = 0 extends (1 & T) ? true : false;",
			"type Equal<A,B> = IsAny<A> extends true ? IsAny<B> : IsAny<B> extends true ? false : [A] extends [B] ? [B] extends [A] ? true : false : false;",
			"type Assert<T extends true> = T;",
			"type Steps = readonly [{ readonly id: 'a'; readonly schema: Reference.StandardSchemaV1<string, number> }, { readonly id: 'b'; readonly extra: 1 }];",
			...contracts.map(([name, args], i) => `type Contract${i} = Assert<Equal<${use("Public", name, args)}, ${use("Reference", name, args)}>>;`),
			"declare const steps: Steps;",
			"const map: Public.StepMap<Steps> = createStepMap(steps);",
			"const index: number = getInitialStepIndex(steps, 'a');",
			"const parsed: Public.Get.Id<Steps> | undefined = parseStep(steps, 'b');",
			"const initial: Public.FlowData<Steps> = getInitialData<Steps>({ a: 'draft' });",
			"const validation: Promise<Public.ValidationResult<number>> = validateStep(steps, 'a', 'draft');",
			"const status: Public.StepStatus = getStepStatus(steps, 0, 'a');",
			"const statuses: Record<Public.Get.Id<Steps>, Public.StepStatus> = getStepStatuses(steps, 0);",
			"const matched: number = matchStep(steps, 'a', { a: () => 1, b: () => 2 });",
		].join("\n"));
		// The original workspace uses Bundler resolution. NodeNext is also valid;
		// supporting either preserves the public contract without adding one.
		const modes = [[ts.ModuleKind.ESNext, ts.ModuleResolutionKind.Bundler], [ts.ModuleKind.NodeNext, ts.ModuleResolutionKind.NodeNext]];
		const failures = [];
		for (const [module, moduleResolution] of modes) {
			const options = { strict: true, noEmit: true, skipLibCheck: false, types: [],
				target: ts.ScriptTarget.ES2022, module, moduleResolution, allowImportingTsExtensions: true };
			const program = ts.createProgram([consumer], options);
			const diagnostics = ts.getPreEmitDiagnostics(program);
			if (diagnostics.length) {
				failures.push(diagnostics.slice(0, 12).map(d => `TS${d.code}${d.file && d.start !== undefined ? ` line ${d.file.getLineAndCharacterOfPosition(d.start).line + 1}` : ""}: ${ts.flattenDiagnosticMessageText(d.messageText, " ")}`).join("\n"));
				continue;
			}
			const checker = program.getTypeChecker();
			const file = program.getSourceFile(consumer);
			const imports = file.statements.filter(ts.isImportDeclaration);
			const publicSymbol = checker.getSymbolAtLocation(imports[0].moduleSpecifier);
			const referenceSymbol = checker.getSymbolAtLocation(imports[1].moduleSpecifier);
			const names = checker.getExportsOfModule(publicSymbol).map(s => s.name);
			const required = checker.getExportsOfModule(referenceSymbol).map(s => s.name);
			assert.ok(required.every(name => names.includes(name)), "every original type must remain exported from the package root");
			const entry = ts.resolveModuleName("@stepperize/core", consumer, options, ts.sys).resolvedModule?.resolvedFileName;
			assert.ok(entry && existsSync(entry), "package root types must resolve to a published type entry");
			const result = { entry, exported: names, typesFileSha256: TYPES_FILE_SHA256 };
			cache.set(directory, { key, result });
			return result;
		}
		assert.fail(`package root type contract failed:\n${failures.join("\n---\n")}`);
	} finally { rmSync(temporary, { recursive: true, force: true }); }
}
