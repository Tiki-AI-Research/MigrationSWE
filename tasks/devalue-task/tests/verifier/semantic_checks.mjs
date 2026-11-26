// Regression checks against the actual evaluator gates, independent of Harbor.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import compiler from '../assets/shared/typescript.cjs';
import { typeSurfaceGate, TYPE_GATES } from '../assets/shared/batches/BATCH-F2P-0002/run.mjs';
import { primitiveEmissionCount as count } from '../assets/shared/primitive-encoding.mjs';
import { captureAsyncOutcome, asyncRejectionValue } from '../assets/public/replacer-cases.mjs';
import {
  repeatedBigIntEncoding, repeatedBigIntInCollections, repeatedLongStringEncoding,
  repeatedLongStringInCollections, boxedPrimitiveDeduplication, scopedFragmentOutcomes,
} from '../assets/shared/batches/BATCH-P2P-0001/harness.mjs';

const results = [];
function check(name, fn) { fn(); results.push(name); }
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'verifier-semantic-'));
const surface = fragment => `${fragment}\n
export type JavaScriptTag = (s: TemplateStringsArray, ...v: unknown[]) => JavaScriptFragment;
export type UnevalReplacer = (v: unknown, js: JavaScriptTag) => JavaScriptFragment | false | null | undefined | void;
export {};
`;

try {
  const scopedSource = `(()=>{const shared={n:11},nested={n:13,kind:'nested'},child={n:17};
    const holder=[child,child];return [{list:[shared,shared],nested:[nested,nested],
    holders:[holder,holder],calls:1},shared]})()`;
  const scopedPass = source => Object.values(scopedFragmentOutcomes({source})).every(value => value === true);
  check('fragment-scope:equivalent-expression', () => assert.equal(scopedPass(scopedSource), true));
  for (const [name, source] of [
    ['hoisted-constructor', '(function(){let shared=new Local(11);return (()=>{class Local{};return shared})()})()'],
    ['lost-outer-identity', scopedSource.replace('},shared]', '},{n:11}]')],
    ['duplicate-construction', scopedSource.replace('calls:1', 'calls:2')],
    ['lost-nested-identity', scopedSource.replace('nested:[nested,nested]', "nested:[nested,{n:13,kind:'nested'}]")],
    ['lost-holder-identity', scopedSource.replace('holders:[holder,holder]', 'holders:[holder,[child,child]]')],
    ['lost-child-identity', scopedSource.replace('holder=[child,child]', 'holder=[child,{n:17}]')],
  ]) check(`fragment-scope:${name}`, () => assert.equal(scopedPass(source), false));

  const cases = [
    ['symbol-brand', 'declare const hidden: unique symbol; export type JavaScriptFragment = {readonly [hidden]: true};', true],
    ['private-class', 'declare class Internal { private secret; } export type JavaScriptFragment = Internal;', true],
    ['private-field', 'declare class Internal { #secret; } export type JavaScriptFragment = Internal;', true],
    ['unique-symbol-value', 'export type JavaScriptFragment = {readonly token: unique symbol};', true],
    ['never-brand', 'export type JavaScriptFragment = {readonly token: never};', true],
    ['source-shape', 'export type JavaScriptFragment = {readonly source: string};', false],
    ['different-shape', 'export interface JavaScriptFragment { payload: unknown[]; flag: true; }', false],
    ['optional-brand', 'declare const hidden: unique symbol; export type JavaScriptFragment = {readonly [hidden]?: true};', false],
    ['forgeable-union', 'declare const hidden: unique symbol; export type JavaScriptFragment = {readonly [hidden]: true} | {source:string};', false],
    ['exported-brand', 'export declare const publicBrand: unique symbol; export type JavaScriptFragment = {readonly [publicBrand]: true};', false],
    ['namespace-brand', 'export declare namespace Brands { const key: unique symbol; } export type JavaScriptFragment = {readonly [Brands.key]: true};', false],
    ['nested-namespace-brand', 'export declare namespace Brands { namespace Nested { const key: unique symbol; } } export type JavaScriptFragment = {readonly [Brands.Nested.key]: true};', false],
    ['namespace-value-brand', 'export declare namespace Brands { const key: unique symbol; } export type JavaScriptFragment = {readonly token: typeof Brands.key};', false],
    ['object-brand', 'export declare const Brands: {readonly key: unique symbol}; export type JavaScriptFragment = {readonly [Brands.key]: true};', false],
    ['nested-object-brand', 'export declare const Brands: {readonly inner: {readonly key: unique symbol}}; export type JavaScriptFragment = {readonly [Brands.inner.key]: true};', false],
    ['object-value-brand', 'export declare const Brands: {readonly key: unique symbol}; export type JavaScriptFragment = {readonly token: typeof Brands.key};', false],
    ['object-alias-brand', 'declare const hidden: unique symbol; export declare const Brands: {readonly key: typeof hidden}; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['tuple-brand', 'declare const hidden: unique symbol; export declare const Brands: readonly [typeof hidden]; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['array-brand', 'declare const hidden: unique symbol; export declare const Brands: readonly (typeof hidden)[]; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['set-brand', 'declare const hidden: unique symbol; export declare const Brands: ReadonlySet<typeof hidden>; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['map-brand', 'declare const hidden: unique symbol; export declare const Brands: ReadonlyMap<string,typeof hidden>; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['promise-brand', 'declare const hidden: unique symbol; export declare const Brands: Promise<typeof hidden>; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['getter-brand', 'declare const hidden: unique symbol; export declare const Brands: {get key():typeof hidden}; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['static-class-brand', 'declare const hidden: unique symbol; export declare class Brands {static readonly key: typeof hidden} export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['factory-brand', 'declare const hidden: unique symbol; export declare function key():typeof hidden; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['factory-object-brand', 'declare const hidden: unique symbol; export declare const Brands:{key():typeof hidden}; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['recursive-object-brand', 'declare const hidden: unique symbol; interface Tree {self:Tree; key:typeof hidden} export declare const Brands:Tree; export type JavaScriptFragment = {readonly [hidden]: true};', false],
    ['private-object-brand', 'declare const hidden: unique symbol; declare const Brands:{readonly key:typeof hidden}; export type JavaScriptFragment = {readonly [hidden]: true};', true],
    ['type-only-object-brand', 'declare const hidden: unique symbol; declare const Brands:{readonly key:typeof hidden}; export type {Brands}; export type JavaScriptFragment = {readonly [hidden]: true};', true],
    ['type-only-interface-brand', 'declare const hidden: unique symbol; export interface Brands {readonly key:typeof hidden} export type JavaScriptFragment = {readonly [hidden]: true};', true],
    ['private-static-class-brand', 'declare const hidden: unique symbol; export declare class Brands {private static key:typeof hidden} export type JavaScriptFragment = {readonly [hidden]: true};', true],
    ['namespace-alias-brand', 'declare namespace Hidden { const key: unique symbol; } export {Hidden as Brands}; export type JavaScriptFragment = {readonly [Hidden.key]: true};', false],
    ['private-namespace-brand', 'declare namespace Hidden { const key: unique symbol; } export type JavaScriptFragment = {readonly [Hidden.key]: true};', true],
    ['type-only-namespace-brand', 'declare namespace Hidden { const key: unique symbol; } export type {Hidden as Brands}; export type JavaScriptFragment = {readonly [Hidden.key]: true};', true],
    ['well-known-symbol', 'export type JavaScriptFragment = {readonly [Symbol.toStringTag]: "fragment"};', false],
  ];
  for (const [name, fragment, expected] of cases) {
    check(`type:${name}`, () => {
      const root = path.join(directory, name);
      fs.mkdirSync(root);
      fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({
        name: 'devalue', type: 'module', types: './index.d.ts',
        exports: { '.': { types: './index.d.ts', import: './index.js' } },
      }));
      fs.writeFileSync(path.join(root, 'index.js'), 'export {};');
      fs.writeFileSync(path.join(root, 'index.d.ts'), surface(fragment));
      const gates = Object.fromEntries(Object.keys(TYPE_GATES).map(id => [id, typeSurfaceGate(root, id)]));
      if (expected) assert.ok(Object.values(gates).every(gate => gate.ok), name + ': ' + JSON.stringify(gates));
      assert.equal(gates['F2P-035'].ok, expected, name + ': ' + JSON.stringify(gates));
      assert.equal(gates['F2P-035'].outcomes.fragment_opaque, expected, name + ': ' + JSON.stringify(gates));
    });
  }
  check('type:ambient-and-alias', () => {
    const root = path.join(directory, 'ambient'); fs.mkdirSync(root);
    fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ name: 'devalue', type: 'module', types: './index.d.ts' }));
    fs.writeFileSync(path.join(root, 'index.d.ts'), "declare module 'devalue' {\n" +
      surface('const hidden: unique symbol; export type JavaScriptFragment = {readonly [hidden]: true};') + 'export {};\n}\n');
    const exported = compiler.declarationExports(root, './index.d.ts');
    assert.deepEqual(exported.diagnostics, []);
    for (const name of ['JavaScriptFragment', 'JavaScriptTag', 'UnevalReplacer']) assert.ok(exported.names.includes(name));
    for (const id of Object.keys(TYPE_GATES)) assert.ok(typeSurfaceGate(root, id).ok, id);
  });
  for (const typeOnly of [false, true]) check(`type:namespace-barrel:type-only=${typeOnly}`, () => {
    const root = path.join(directory, `namespace-barrel-${typeOnly}`); fs.mkdirSync(root);
    fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ name: 'devalue', type: 'module', types: './index.d.ts' }));
    fs.writeFileSync(path.join(root, 'brands.d.ts'), 'export declare const key: unique symbol;');
    fs.writeFileSync(path.join(root, 'index.d.ts'), surface(
      `import type * as Brands from './brands.js';\nexport ${typeOnly ? 'type ' : ''}* as Brands from './brands.js';\n` +
      'export type JavaScriptFragment = {readonly [Brands.key]: true};'));
    assert.deepEqual(compiler.declarationExports(root, './index.d.ts').diagnostics, []);
    const gate = typeSurfaceGate(root, 'F2P-035');
    assert.equal(gate.ok, typeOnly, JSON.stringify(gate));
    assert.equal(gate.outcomes.fragment_opaque, typeOnly);
    if (!typeOnly) {
      // A real consumer can forge the public namespace brand without a cast.
      const ts = compiler.loadTypeScript();
      const probe = path.join(root, 'probe.mts');
      fs.writeFileSync(probe, `import {Brands} from 'devalue';\nimport type {JavaScriptFragment,UnevalReplacer} from 'devalue';\n` +
        'const forged:JavaScriptFragment={[Brands.key]:true}; const replacer:UnevalReplacer=()=>forged;');
      const program = ts.createProgram([probe], { strict: true, noEmit: true, types: [],
        target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext, paths: { devalue: [path.join(root, 'index.d.ts')] } });
      assert.deepEqual(ts.getPreEmitDiagnostics(program).map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n')), []);
    }
  });

  for (const [name, source, expected] of [
    ['literal', '((a)=>[a,a])(7n)', 1],
    ['hex-literal', '((a)=>[a,a])(0x7n)', 1],
    ['binary-literal', '((a)=>[a,a])(0b111n)', 1],
    ['constructor', '((a)=>[a,a])(BigInt("7"))', 1],
    ['number-constructor', '((a)=>[a,a])(BigInt(7))', 1],
    ['hex-constructor', '((a)=>[a,a])(globalThis.BigInt("0x7"))', 1],
    ['alias-constructor', '(()=>{const B=globalThis.BigInt,a=B("7");return [a,a]})()', 1],
    ['destructured-constructor', '(()=>{const {BigInt}=globalThis;const a=BigInt("7");return [a,a]})()', 1],
    ['renamed-destructured-constructor', '(()=>{const {BigInt:B}=globalThis;const a=B("7");return [a,a]})()', 1],
    ['destructured-global-alias', '(()=>{const g=globalThis;const {BigInt:B}=g;const a=B("7");return [a,a]})()', 1],
    ['destructured-computed-key', '(()=>{const k="Big"+"Int";const {[k]:B}=globalThis;const a=B("7");return [a,a]})()', 1],
    ['destructured-alias-chain', '(()=>{const {BigInt:B}=globalThis;const C=B;const a=C("7");return [a,a]})()', 1],
    ['shadowed-global', '((globalThis)=>{const {BigInt:B}=globalThis;return [B("7"),B("7")]})({BigInt:()=>8n})', 0],
    ['non-builtin-object', '(()=>{const {BigInt:B}={BigInt:()=>8n};return [B("7"),B("7")]})()', 0],
    ['rest-is-not-constructor', '(()=>{const {...BigInt}=globalThis;return BigInt("7")})()', 0],
    ['mutable-is-not-builtin-alias', '(()=>{let {BigInt:B}=globalThis;B=()=>8n;return [B("7"),B("7")]})()', 0],
    ['arithmetic', '((a)=>[a,a])(3n+4n)', 1],
    ['negative', '((a)=>[a,a])(-(-7n))', 1],
    ['raw-duplicates', '[7n,7n]', 2],
    ['mixed-duplicates', '[7n,BigInt("0x7")]', 2],
    ['comment-spoof', '/*7n*/[BigInt("0x7"),BigInt("0x7")]', 2],
    ['string-spoof', '({note:"7n",items:[BigInt("0x7"),BigInt("0x7")]})', 2],
    ['marker-only-in-comment', '/*7n*/[1n,2n]', 0],
    ['unused-literal', '(()=>{const {BigInt:B}=globalThis;7n;return [B("7"),B("7")]})()', 2],
    ['unused-initializer', '(()=>{const unused=7n;return [BigInt("7"),BigInt("7")]})()', 2],
    ['unused-parameter', '((unused)=>[BigInt("7"),BigInt("7")])(7n)', 2],
    ['uncalled-function', '(()=>{const unused=()=>7n;return [BigInt("7"),BigInt("7")]})()', 2],
    ['uncalled-function-write', '(()=>{const a=[];function unused(){a.push(7n)}return [BigInt("7"),BigInt("7"),a]})()', 2],
    ['unused-comma-operand', '(7n,[BigInt("7"),BigInt("7")])', 2],
    ['unused-marker-only', '(()=>{7n;const unused=7n;return [1n,2n]})()', 0],
    ['later-assignment', '(()=>{let a;a=7n;return [a,a]})()', 1],
    ['destructured-array-value', '(()=>{const [a]=[7n];return [a,a]})()', 1],
    ['destructured-object-value', '(()=>{const {x:a}={x:7n};return [a,a]})()', 1],
    ['destructured-nested-value', '(()=>{const [{x:[a]}]=[{x:[7n]}];return [a,a]})()', 1],
    ['destructured-default-value', '(()=>{const [a=7n]=[];return [a,a]})()', 1],
    ['destructured-undefined-default', '(()=>{const {x:a=7n}={x:undefined};return [a,a]})()', 1],
    ['destructured-unused-default', '(()=>{const [a=7n]=[8n];return [a,a]})()', 0],
    ['destructured-unused-element', '(()=>{const [,a]=[7n,8n];return [a,a]})()', 0],
    ['destructured-array-rest', '(()=>{const [unused,...a]=[8n,7n];return [a,a]})()', 1],
    ['destructured-parameter', '(([a])=>[a,a])([7n])', 1],
    ['destructured-object-parameter', '(({x:a})=>[a,a])({x:7n})', 1],
    ['destructured-default-parameter', '(([a]=[7n])=>[a,a])()', 1],
    ['destructured-aliased-source', '(()=>{const x=[7n];const y=x;const [a]=y;return [a,a]})()', 1],
    ['initializer-side-effect', '(()=>{let a;function init(){a=7n}init();return [a,a]})()', 1],
    ['initializer-alias', '(()=>{let a;function init(){a=7n}const f=init;f();return [a,a]})()', 1],
    ['initializer-nested-call', '(()=>{let a;function set(){a=7n}function init(){set()}init();return [a,a]})()', 1],
    ['initializer-hidden-duplicate', '(()=>{let a;function init(){a=7n}init();return [7n,a]})()', 2],
    ['initializer-hidden-constructor', '(()=>{let a;function init(){a=BigInt("7")}init();return [7n,a]})()', 2],
    ['initializer-parameter-write', '(()=>{const a=[];function init(x){x.push(7n)}init(a);return a})()', 1],
    ['initializer-unused-return', '(()=>{function unused(){return 7n}unused();return [BigInt("7"),BigInt("7")]})()', 2],
    ['initializer-call', '(()=>{let a;function init(){a=7n}init.call(null);return [a,a]})()', 1],
    ['initializer-apply', '(()=>{let a;function init(v){a=v}init.apply(null,[7n]);return [a,a]})()', 1],
    ['false-branch-call', '(()=>{const a=[];function init(){a.push(7n)}if(false)init();return [BigInt("7"),BigInt("7"),a]})()', 2],
    ['false-branch-write', '(()=>{const a=[];if(false)a.push(7n);return [BigInt("7"),BigInt("7"),a]})()', 2],
    ['after-return-call', '(()=>{const a=[];function init(){a.push(7n)}return [BigInt("7"),BigInt("7"),a];init()})()', 2],
    ['short-circuit-call', '(()=>{const a=[];function init(){a.push(7n)}false&&init();return [BigInt("7"),BigInt("7"),a]})()', 2],
    ['conditional-unused-marker', '(false?7n:[BigInt("7"),BigInt("7")])', 2],
    ['comma-assignment', '(()=>{let a;return (a=7n,[a,a])})()', 1],
    ['called-function-alias', '(()=>{const f=a=>[a,a];return f(7n)})()', 1],
    ['function-declaration', '(()=>{function f(a){return [a,a]}return f(7n)})()', 1],
    ['default-parameter', '((a=7n)=>[a,a])()', 1],
    ['rest-parameter', '((...a)=>a)(7n)', 1],
    ['object-assignment', '(()=>{const a={};const b=7n;a.x=b;a.y=b;return a})()', 1],
    ['shorthand-property', '(()=>{const a=7n;return [{a},{a}]})()', 1],
    ['object-alias-mutation', '(()=>{const a=[];const b=a;b.push(7n);return a})()', 1],
    ['map-mutation', '(()=>{const a=new Map;const b=7n;a.set(b,b);return a})()', 1],
    ['computed-map-mutation', '(()=>{const a=new Map;const b=7n;a["set"](b,b);return a})()', 1],
    ['set-mutation', '(()=>{const a=new Set;const b=7n;a.add(b);return a})()', 1],
    ['array-mutation', '(()=>{const a=[];const b=7n;a.push(b,b);return a})()', 1],
    ['parameter-mutation', '(a=>{a.push(7n);return a})([])', 1],
    ['object-assign', '(()=>{const a={};const b=7n;Object.assign(a,{x:b,y:b});return a})()', 1],
    ['define-property', '(()=>{const a={};const b=7n;Object.defineProperty(a,"x",{value:b});return a})()', 1],
    ['circular-result', '(()=>{const a=[];const b=7n;a.push(b,b,a);return a})()', 1],
    ['invalid-expression', '[7n', -1],
  ]) check(`encoding:${name}`, () => assert.equal(count(source, 7n), expected, source));
  check('encoding:escaped-string', () => assert.equal(count('((a)=>[a,a])("\\u0061bc")', 'abc'), 1));
  check('encoding:folded-string', () => assert.equal(count('((a)=>[a,a])("a"+"bc")', 'abc'), 1));
  check('encoding:string-substring-and-comment', () => assert.equal(count('/*abc*/["xabcx"]', 'abc'), 0));
  check('encoding:bounded-folding', () => {
    const declarations = ['const s0="x";'];
    for (let i = 1; i < 40; i++) declarations.push(`const s${i}=s${i-1}+s${i-1};`);
    assert.equal(count(`(()=>{${declarations.join('')}return [7n,7n]})()`, 7n), 2);
  });

  // One serializer fixture handles arbitrary nested arrays, objects, Map/Set
  // and shared wrappers. Its selectable encoding is independent of the gate's
  // fixed scenario values. The negative mode duplicates every primitive and
  // writes its decimal spelling only in metadata comments.
  function emitter(mode, encodeString = false) {
    return { uneval(input) {
      const counts = new Map(), seen = new Set(), bindings = new Map();
      function walk(value) {
        if (typeof value === 'bigint' || typeof value === 'string' && value.length >= 128 || value instanceof String) {
          counts.set(value, (counts.get(value) ?? 0) + 1);
        }
        if (!value || typeof value !== 'object' || seen.has(value)) return;
        seen.add(value);
        if (value instanceof String) walk(String(value));
        else if (value instanceof Map) for (const [k,v] of value) { walk(k); walk(v); }
        else if (value instanceof Set) for (const v of value) walk(v);
        else for (const v of Object.values(value)) walk(v);
      }
      walk(input);
      const string = value => encodeString ? '"' + [...value].map(c => `\\u${c.charCodeAt(0).toString(16).padStart(4,'0')}`).join('') + '"' : JSON.stringify(value);
      const literal = value => typeof value === 'bigint'
        ? mode === 'literal' ? `${value}n` : mode === 'hex' ? `0x${value.toString(16)}n`
          : `${mode.startsWith('destructured') ? 'B' : 'BigInt'}("0x${value.toString(16)}")`
        : string(value);
      const definitions = mode.startsWith('destructured') ? ['const {BigInt:B}=globalThis;'] : [];
      if (mode === 'destructured-unused-marker') {
        for (const value of counts.keys()) if (typeof value === 'bigint') definitions.push(`${value}n;`);
      }
      for (const [value, n] of counts) if (n > 1 && !['duplicate', 'destructured-unused-marker'].includes(mode) && !(value instanceof String)) {
        const name = `v${bindings.size}`; bindings.set(value, name); definitions.push(`const ${name}=${literal(value)};`);
      }
      function emit(value) {
        if (bindings.has(value)) return bindings.get(value);
        if (value instanceof String) {
          const expression = `new String(${emit(String(value))})`;
          if ((counts.get(value) ?? 0) > 1) {
            const name = `v${bindings.size}`; bindings.set(value, name); definitions.push(`const ${name}=${expression};`); return name;
          }
          return expression;
        }
        if (typeof value === 'bigint' || typeof value === 'string') return literal(value);
        if (Array.isArray(value)) return `[${value.map(emit).join(',')}]`;
        if (value instanceof Map) return `new Map([${[...value].map(([k,v])=>`[${emit(k)},${emit(v)}]`).join(',')}])`;
        if (value instanceof Set) return `new Set([${[...value].map(emit).join(',')}])`;
        if (value && typeof value === 'object') return `{${Object.entries(value).map(([k,v])=>`${JSON.stringify(k)}:${emit(v)}`).join(',')}}`;
        return JSON.stringify(value);
      }
      const expression = emit(input);
      const metadata = mode === 'duplicate' ? `/* ${[...counts.keys()].filter(v=>typeof v==='bigint').map(v=>`${v}n`).join(',')} */` : '';
      return metadata + (definitions.length ? `(()=>{${definitions.join('')}return ${expression}})()` : expression);
    } };
  }
  for (const mode of ['literal', 'hex', 'constructor', 'duplicate', 'destructured', 'destructured-unused-marker']) {
    for (const gate of [repeatedBigIntEncoding, repeatedBigIntInCollections]) {
      check(`gate:${gate.name}:${mode}`, () => assert.equal(gate(emitter(mode)).ok,
        !['duplicate', 'destructured-unused-marker'].includes(mode)));
    }
  }
  for (const gate of [repeatedLongStringEncoding, repeatedLongStringInCollections, boxedPrimitiveDeduplication]) {
    for (const escaped of [false, true]) check(`gate:${gate.name}:escaped=${escaped}`, () =>
      assert.ok(gate(emitter('constructor', escaped)).ok));
  }
  // A root reducer may fail before any child promise is reached. If traversal
  // does reach that property, failing to handle its rejection must still be
  // observable. These mutants test both sides of the same scored scenario.
  const lazyReducerValue = asyncRejectionValue('reducer-failure');
  const reachedPromise = lazyReducerValue.p;
  reachedPromise.catch(() => {});
  check('async:root-reducer:stable-promise-identity', () => assert.equal(lazyReducerValue.p, reachedPromise));
  for (const reached of [false, true]) {
    let unhandled = 0;
    const listener = () => { unhandled++; };
    process.on('unhandledRejection', listener);
    try {
      const outcome = await captureAsyncOutcome({async stringifyAsync(value, reducers) {
        if (reached) void value.p;
        return reducers.Broken(value);
      }}, 'reducer-failure');
      await new Promise(resolve => setTimeout(resolve, 80));
      check(`async:root-reducer:reached=${reached}`, () => {
        assert.equal(outcome.status, 'rejected');
        assert.equal(outcome.marker, 'reducer');
        assert.equal(unhandled, reached ? 1 : 0);
      });
    } finally { process.removeListener('unhandledRejection', listener); }
  }
} finally {
  fs.rmSync(directory, { recursive: true, force: true });
}
console.log(JSON.stringify({ passed: results.length, checks: results }));
