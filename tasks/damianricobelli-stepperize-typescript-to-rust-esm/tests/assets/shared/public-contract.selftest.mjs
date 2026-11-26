// Regression controls for the audited false negatives and false positives.
import assert from "node:assert/strict";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { trustedTypeScript } from "./compiler.mjs";
import { coreRuntimeEntry } from "./package-entry.mjs";
import { assertPublicTypeContract } from "./type-contract.mjs";
import { assertCoreOperationsDelegate, rustRuntime } from "./rust-runtime.mjs";
import { assertRustResponsibilities } from "./rust-responsibilities.mjs";
import { copyPublishedCoreFiles } from "./native-layout.mjs";

const sandbox = mkdtempSync(join(tmpdir(), "stepperize-contract-selftest-"));
const fixtures = join(dirname(fileURLToPath(import.meta.url)), "../fixtures/type-contract");
const typesSource = readFileSync(join(fixtures, "types.ts"), "utf8");
const utils = readFileSync(join(fixtures, "utils.d.ts"), "utf8");
const types = trustedTypeScript().transpileDeclaration(typesSource, {}).outputText;
let checked = 0;
const put = (file, text) => { mkdirSync(dirname(file), { recursive: true }); writeFileSync(file, text); };
const test = async (name, body) => { await body(); checked += 1; process.stdout.write(`PASS ${name}\n`); };
function fixture(name, { split = true, declarations = "dist/index.d.ts", files = ["dist"] } = {}) {
	const root = join(sandbox, name), core = join(root, "packages/core"), directory = dirname(join(core, declarations));
	put(join(core, "src/types.ts"), typesSource);
	put(join(core, "package.json"), JSON.stringify({ name: "@stepperize/core", version: "1.0.0", type: "module", files,
		main: "./dist/index.js", types: `./${declarations}`, exports: { ".": { types: `./${declarations}`, import: "./dist/index.js" } } }));
	put(join(core, "dist/index.js"), "export {};\n");
	if (split) {
		put(join(directory, "types.d.ts"), types);
		put(join(directory, "utils.d.ts"), utils);
		put(join(core, declarations), 'export type * from "./types.js";\nexport * from "./utils.js";\n');
	} else {
		put(join(core, declarations), types + "\n" + utils.replace(/^import type .*;\r?\n/m, ""));
	}
	return { root, core, entry: join(core, declarations) };
}
function filesBelow(root, prefix = "") {
	return readdirSync(join(root, prefix), { withFileTypes: true }).flatMap(entry => {
		const path = prefix ? `${prefix}/${entry.name}` : entry.name;
		return entry.isDirectory() ? filesBelow(root, path) : [path];
	}).sort();
}

try {
	await test("legal split declarations and all public type definitions", () => {
		const f = fixture("split");
		assert.ok(assertPublicTypeContract(f.root).exported.includes("Step"));
	});
	await test("legal bundled declarations", () => assertPublicTypeContract(fixture("bundled", { split: false }).root));
	await test("alternate declaration path and .d.mts root entry", () => {
		const f = fixture("alternate", { declarations: "declarations/public.d.mts" });
		assert.equal(assertPublicTypeContract(f.root).entry, f.entry);
	});
	await test("a published TypeScript type entry is also legal", () => {
		const f = fixture("source-type-entry", { declarations: "types/public.ts" });
		assert.equal(assertPublicTypeContract(f.root).entry, f.entry);
	});
	await test("actual exports required, local type names cannot substitute", () => {
		const f = fixture("private-types", { split: false });
		const noTypes = types.replace(/export\s+type\s*\{[^}]*\};?/g, "");
		const noFunctions = utils.replace(/^import type .*;\r?\n/m, "").replaceAll("export declare function", "declare function");
		put(f.entry, noTypes + noFunctions + "\nexport { createStepMap, getInitialStepIndex, parseStep, getInitialData, validateStep, getStepStatus, getStepStatuses, matchStep };\n");
		assert.throws(() => assertPublicTypeContract(f.root), /type contract failed/);
	});
	await test("changed built type definitions rejected with unchanged types.ts", () => {
		const f = fixture("wrong-status", { split: false });
		put(f.entry, readFileSync(f.entry, "utf8").replace('type StepStatus = "active" | "previous" | "upcoming";', 'type StepStatus = "broken";'));
		assert.throws(() => assertPublicTypeContract(f.root), /type contract failed/);
	});
	await test("any cannot replace an original public type", () => {
		const f = fixture("any-status", { split: false });
		put(f.entry, readFileSync(f.entry, "utf8").replace('type StepStatus = "active" | "previous" | "upcoming";', 'type StepStatus = any;'));
		assert.throws(() => assertPublicTypeContract(f.root), /type contract failed/);
	});
	await test("original types.ts remains protected", () => {
		const f = fixture("changed-source");
		put(join(f.core, "src/types.ts"), typesSource + "\n// changed\n");
		assert.throws(() => assertPublicTypeContract(f.root), /must remain unchanged/);
	});
	await test("types must also resolve in the published package", async () => {
		const f = fixture("unpublished-types", { declarations: "types/public.d.ts", files: ["dist"] });
		const published = join(sandbox, "published-no-types");
		await copyPublishedCoreFiles(f.core, published);
		assert.throws(() => assertPublicTypeContract(f.root, { packageDirectory: published }), /type contract failed/);
	});
	await test("package import entry can use another directory and conditional exports", () => {
		const f = fixture("runtime-entry");
		put(join(f.core, "esm/public.mjs"), "export {};\n");
		const manifest = JSON.parse(readFileSync(join(f.core, "package.json")));
		manifest.exports["."].import = { node: "./esm/public.mjs", default: "./dist/index.js" };
		put(join(f.core, "package.json"), JSON.stringify(manifest));
		assert.equal(coreRuntimeEntry(f.root), join(f.core, "esm/public.mjs"));
	});
	await test("npm brace/globstar patterns and mandatory files preserved", async () => {
		const f = fixture("brace", { files: ["dist/**/*.{js,wasm,d.ts}"] });
		put(join(f.core, "dist/runtime/engine.wasm"), "compiled artifact");
		put(join(f.core, "dist/unpublished.txt"), "not selected");
		put(join(f.core, "README.md"), "mandatory npm file");
		const published = join(sandbox, "brace-published");
		await copyPublishedCoreFiles(f.core, published);
		assert.ok(existsSync(join(published, "dist/runtime/engine.wasm")));
		assert.ok(existsSync(join(published, "dist/index.js")));
		assert.ok(existsSync(join(published, "README.md")));
		assert.ok(!existsSync(join(published, "dist/unpublished.txt")));
		assertPublicTypeContract(f.root, { packageDirectory: published });
	});
	await test("npm negated patterns respected", async () => {
		const f = fixture("negated", { files: ["dist", "!dist/private/**"] });
		put(join(f.core, "dist/private/secret.wasm"), "not shipped");
		const published = join(sandbox, "negated-published");
		await copyPublishedCoreFiles(f.core, published);
		assert.ok(existsSync(join(published, "dist/index.js")));
		assert.ok(!existsSync(join(published, "dist/private/secret.wasm")));
	});
	await test("published synchronous WASM is attributed to the actual package entry", async () => {
		const f = fixture("published-wasm");
		const publishedRoot = join(sandbox, "wasm-consumer"), publishedCore = join(publishedRoot, "node_modules/@stepperize/core");
		await copyPublishedCoreFiles(f.core, publishedCore);
		const binding = rustRuntime(publishedRoot);
		const bytes = new Uint8Array([0,97,115,109,1,0,0,0,1,7,1,96,2,127,127,1,127,3,2,1,0,7,7,1,3,97,100,100,0,0,10,9,1,7,0,32,0,32,1,106,11]);
		const wasm = new WebAssembly.Instance(new WebAssembly.Module(bytes));
		assert.equal(wasm.exports.add(1, 2), 3);
		assert.ok(binding.total() > 0);
		assert.ok(binding.artifacts.has(join(publishedCore, "dist/index.js")));
	});
	await test("npm default packlist and .npmignore respected", async () => {
		const f = fixture("default-files");
		const manifest = JSON.parse(readFileSync(join(f.core, "package.json")));
		delete manifest.files;
		put(join(f.core, "package.json"), JSON.stringify(manifest));
		put(join(f.core, ".npmignore"), "hidden.txt\n");
		put(join(f.core, "hidden.txt"), "ignored");
		const published = join(sandbox, "default-published");
		await copyPublishedCoreFiles(f.core, published);
		assert.ok(!filesBelow(published).includes("hidden.txt"));
		assert.ok(filesBelow(published).includes("dist/index.d.ts"));
	});
	await test("JS step search followed by compiled identity rejected", async () => {
		const f = fixture("identity-runtime");
		const bytes = [0,97,115,109,1,0,0,0,1,6,1,96,1,111,1,111,3,2,1,0,7,8,1,4,101,99,104,111,0,0,10,6,1,4,0,32,0,11];
		put(join(f.core, "dist/index.js"), `const echo = new WebAssembly.Instance(new WebAssembly.Module(new Uint8Array(${JSON.stringify(bytes)}))).exports.echo;
export function getInitialStepIndex(steps, id) { for (let i=0;i<steps.length;i++) if (steps[i].id === id) return echo(i); return echo(0); }`);
		await assert.rejects(assertCoreOperationsDelegate(f.root), /step lookup is implemented/);
	});
	await test("renamed/computed lookup in a production dependency rejected", () => {
		const f = fixture("imported-lookup");
		put(join(f.core, "src/index.ts"), 'export { choose } from "./tests/dependency";');
		put(join(f.core, "src/tests/dependency.ts"), 'export const choose=(sequence,key)=>sequence.find(item=>item["i"+"d"]===key);');
		assert.throws(() => assertRustResponsibilities(f.root), /step lookup is implemented/);
	});
	await test("lookup over marshalled step ids must still execute in Rust", () => {
		const f = fixture("id-list-lookup");
		put(join(f.core, "src/helpers.ts"), 'export const choose=(steps,id)=>{const ids=steps.map(step=>step.id);return native.echo(ids.indexOf(id));};');
		assert.throws(() => assertRustResponsibilities(f.root), /step lookup is implemented/);
	});
	await test("production code at an alternate package entry is checked", () => {
		const f = fixture("alternate-lookup");
		const manifest = JSON.parse(readFileSync(join(f.core, "package.json")));
		manifest.exports["."].import = "./esm/main.mjs";
		put(join(f.core, "package.json"), JSON.stringify(manifest));
		put(join(f.core, "esm/main.mjs"), 'export const choose=(steps,id)=>steps.find(s=>s.id===id);');
		assert.throws(() => assertRustResponsibilities(f.root), /step lookup is implemented/);
	});
	await test("JS matching handler selection rejected", () => {
		const f = fixture("js-matching");
		put(join(f.core, "src/helpers.ts"), 'export const match=(handlers,id,step)=>{const handler=handlers[id];if(!handler)throw new Error("No match handler");return native.echo(handler(step));};');
		assert.throws(() => assertRustResponsibilities(f.root), /matching handler selection/);
	});
	await test("JS positional status derivation rejected", () => {
		const f = fixture("js-status");
		put(join(f.core, "src/helpers.ts"), 'export const status=(i,current)=>i===current?"active":i>=0&&i<current?"previous":"upcoming";');
		assert.throws(() => assertRustResponsibilities(f.root), /positional status derivation/);
	});
	await test("JS positional status derivation through a difference rejected", () => {
		const f = fixture("js-status-difference");
		put(join(f.core, "src/helpers.ts"), 'export const status=(i,current)=>{const delta=i-current;return delta===0?"active":delta<0?"previous":"upcoming";};');
		assert.throws(() => assertRustResponsibilities(f.root), /positional status derivation/);
	});
	await test("decoding Rust status tags with numeric comparisons is accepted", () => {
		const f = fixture("status-tag-decoder");
		put(join(f.core, "src/helpers.ts"), `const previousTag = 2;
export const decodeStatus=code=>code===0?"active":code<previousTag?"previous":"upcoming";
export const status=(steps,current,id)=>decodeStatus(native.status(steps,current,id));`);
		assertRustResponsibilities(f.root);
	});
	await test("JS schema result decisions rejected", () => {
		const f = fixture("js-validation");
		put(join(f.core, "src/helpers.ts"), 'export const finish=(result)=>result.issues?{success:false,issues:result.issues}:{success:true,data:result.value};');
		assert.throws(() => assertRustResponsibilities(f.root), /validation dispatch/);
	});
	await test("type layers/comments and legal serialization are accepted", () => {
		const f = fixture("marshalling");
		put(join(f.core, "src/helpers.ts"), `// steps.find(step=>step.id===id) is not executable
type Status = "active" | "previous" | "upcoming";
export const encode=steps=>JSON.stringify(steps.map(step=>({id:step.id})));
export const validate=async(steps,id,value)=>{
 const fields=[]; for(const step of steps) fields.push(step.id,step.schema?"1":"0");
 const plan=native.plan(id,fields); if(plan[0]==="passthrough") return {success:true,data:value};
 const result=await steps[Number(plan[1])].schema["~standard"].validate(value);
 const outcome=native.outcome(result.issues?"1":"0");
 return outcome==="failure"?{success:false,issues:result.issues}:{success:true,data:result.value};
};`);
		assertRustResponsibilities(f.root);
	});
	await test("lexical alias shadowing does not invent a step lookup in a codec", () => {
		const f = fixture("shadowed-codec");
		put(join(f.core, "src/helpers.ts"), `const key="id";
export function encode(records,key){const result=[];for(const item of records){if(item[key]===undefined)continue;result.push(item[key]);}return JSON.stringify(result);}`);
		assertRustResponsibilities(f.root);
	});
	await test("the exact archived Muse 02 tsup artifact is a legal Rust facade", () => {
		const f = fixture("archived-muse02");
		put(join(f.core, "dist/index.js"), readFileSync(join(fixtures, "../rust-responsibilities/muse02-tsup.mjs"), "utf8"));
		assertRustResponsibilities(f.root);
	});
	await test("the full reflected JS plus WASM identity counterexample is rejected", async () => {
		const f = fixture("reflected-identity");
		put(join(f.core, "dist/index.js"), readFileSync(join(fixtures, "../rust-responsibilities/reflect-identity.mjs"), "utf8"));
		await assert.rejects(assertCoreOperationsDelegate(f.root), error =>
			["step lookup", "positional status", "matching handler", "validation dispatch"].every(label => error.message.includes(label)));
	});
	const provenanceCases = [
		["reflected issues still cannot decide validation in JS",
			'export const finish=result=>Reflect.get(result,"issues")?{success:false,issues:result.issues}:{success:true,data:result.value};', /validation dispatch/],
		["minified booleans do not bypass JS validation checks",
			'export const finish=result=>result.issues?{success:!1,issues:result.issues}:{success:!0,data:result.value};', /validation dispatch/],
		["destructured validation facts retain their origin",
			'export const finish=result=>{const {issues:errors}=result;return errors?{success:false,issues:errors}:{success:true,data:result.value};};', /validation dispatch/],
		["local JS decisions through helper early returns remain JS decisions",
			'function hasErrors(result){if(Reflect.get(result,"issues"))return true;return false;} export const finish=result=>hasErrors(result)?{success:!1,issues:result.issues}:{success:!0,data:result.value};', /validation dispatch/],
		["local response builders cannot conceal JS validation decisions",
			'const fail=issues=>({success:false,issues}),ok=data=>({success:true,data});export const finish=result=>result.issues?fail(result.issues):ok(result.value);', /validation dispatch/],
		["a computed success field is also a validation decision",
			'export const finish=result=>({success:!Reflect.get(result,"issues"),data:result.value,issues:result.issues});', /validation dispatch/],
		["a destructured Reflect.get alias still exposes step lookup",
			'const {get:read}=Reflect;export const choose=(steps,id)=>steps.find(step=>read(step,"i"+"d")===id);', /step lookup/],
		["local property-reading helpers retain step-id provenance",
			'const read=(object,key)=>Reflect.get(object,key),stepId=step=>read(step,"id");export const choose=(steps,id)=>steps.find(step=>stepId(step)===id);', /step lookup/],
		["destructured step ids retain lookup provenance",
			'export const choose=(steps,id)=>steps.find(step=>{const {id:got}=step;return got===id;});', /step lookup/],
		["a helper returning reflected ids cannot move includes out of Rust",
			'const idsOf=steps=>steps.map(step=>Reflect.get(step,"id"));export const parse=(steps,id)=>idsOf(steps).includes(id)?id:undefined;', /step lookup/],
		["reflected missing-handler decisions remain matching logic",
			'const read=Reflect.get;export const match=(handlers,id,step)=>{const handler=read(handlers,id);if(!handler)throw new Error("No match handler");return handler(step);};', /matching handler/],
		["a status string table cannot conceal positional decisions",
			'const labels=["active","previous","upcoming"];export const status=(index,current)=>labels[index===current?0:index>=0&&index<current?1:2];', /positional status/],
		["helper early returns cannot conceal a positional status decision",
			'const labels=["active","previous","upcoming"];function code(index,current){if(index===current)return 0;if(index>=0&&index<current)return 1;return 2;}export const status=(index,current)=>labels[code(index,current)];', /positional status/],
		["inline Rust decisions with reflected input facts are legal",
			'const read=Reflect.get;export const finish=result=>native.outcome(read(result,"issues")?1:0)===0?{success:true,data:result.value}:{success:false,issues:result.issues};', null],
		["local Rust wrappers preserve the native-result boundary",
			'function callRust(fields){const wire=JSON.stringify(fields);const output=native.dispatch(wire);if(output==="")return [];return output.split("|");}export const finish=result=>callRust([result.issues?1:0])[0]==="failure"?{success:!1,issues:result.issues}:{success:!0,data:result.value};', null],
		["a native function alias is still a native-result boundary",
			'const decide=native.finish;export const finish=result=>decide(result.issues?1:0)===0?{success:!0,data:result.value}:{success:!1,issues:result.issues};', null],
		["Rust status codes can be decoded with a fixed string table",
			'const labels=["active","previous","upcoming"];export const status=(steps,current,id)=>labels[native.status(steps,current,id)];', null],
		["fixed enum tags remain legal after const-to-let minification",
			'let tags={Active:0,Upcoming:2};export const decode=code=>code===tags.Active?"active":code<tags.Upcoming?"previous":"upcoming";', null],
		["Rust status decoding does not inherit unrelated serializer comparisons",
			'const labels=["active","previous","upcoming"];export function status(steps,current,id){let fields=[];for(let i=0;i<steps.length;i++)fields.push(steps[i].id);return labels[native.status(fields,current,id)];}', null],
		["generic WASM host property access and callbacks are legal",
			'export const host={get:(object,key)=>Reflect.get(object,key),call:(fn,receiver,arg)=>Reflect.apply(fn,receiver,[arg]),equal:(left,right)=>left===right};', null],
		["constant property helpers retain reflected step-id provenance",
			'function field(){return "id";}export const choose=(steps,id)=>steps.find(step=>Reflect.get(step,field())===id);', /step lookup/],
		["constant joins retain reflected step-id provenance",
			'export const choose=(steps,id)=>steps.find(step=>Reflect.get(step,["id"].join())===id);', /step lookup/],
		["constant analysis does not execute object coercions in a codec",
			'export function encode(value){const a=""+{},b=+[value];return JSON.stringify([a,b]);}', null],
		["object array joins in a codec do not coerce analysis records",
			'export function encode(){return [{}].join();}', null],
		["nested unknown array joins in a codec stay unknown",
			'export function encode(value){return [[value]].join();}', null],
		["dynamic builtin destructuring stays unknown",
			'export function encode(value,key){const {[key]:operation}=Reflect;return operation(value,"wire");}', null],
		["object-valued builtin destructuring does not coerce analysis records",
			'const {[{}]:operation}=Reflect;export const encode=value=>operation?.(value,"wire");', null],
		["computed record keys and member keys do not coerce analysis records",
			'export function encode(value){const record={[{}]:value};return [record,({wire:value})[{}]];}', null],
		["computed destructuring keys do not coerce analysis records",
			'export function encode(value){const {[{}]:wire}={wire:value};return wire;}', null],
		["nested known joins still expose reflected step-id lookup",
			'export const choose=(steps,id)=>steps.find(step=>Reflect.get(step,[["i","d"].join("")].join())===id);', /step lookup/],
		["known array property keys still expose step-id lookup",
			'export const choose=(steps,id)=>steps.find(step=>step[["id"]]===id);', /step lookup/],
	];
	for (const [name, source, rejection] of provenanceCases) await test(name, () => {
		const f = fixture("provenance-" + checked);
		put(join(f.core, "src/helpers.ts"), source);
		if (rejection) assert.throws(() => assertRustResponsibilities(f.root), rejection);
		else assertRustResponsibilities(f.root);
	});
	process.stdout.write(`public-contract selftest OK (${checked} checks)\n`);
} finally { rmSync(sandbox, { recursive: true, force: true }); }
