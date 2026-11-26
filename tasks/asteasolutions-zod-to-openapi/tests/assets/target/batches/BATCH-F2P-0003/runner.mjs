import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--repo') out.repo = argv[++i];
    else if (a === '--report') out.report = argv[++i];
    else if (a === '--only') out.only = argv[++i];
  }
  return out;
}
const args = parseArgs(process.argv.slice(2));
const repo = path.resolve(args.repo || process.env.MIGRATION_REPO_ROOT || process.cwd());
const requireRepo = createRequire(path.join(repo, 'package.json'));

function readZodVersion() {
  for (const p of [path.join(repo, 'node_modules/zod/package.json'), path.join(repo, 'node_modules/.pnpm/zod/package.json')]) {
    if (fs.existsSync(p)) return JSON.parse(fs.readFileSync(p, 'utf8')).version;
  }
  try { return requireRepo('zod/package.json').version; } catch {}
  try { return JSON.parse(fs.readFileSync(requireRepo.resolve('zod/package.json'), 'utf8')).version; } catch {}
  return 'unknown';
}

let libPromise;
let activeId = '';
function loadLib() {
  libPromise ??= (async () => {
    const mjs = path.join(repo, 'dist/index.mjs');
    if (fs.existsSync(mjs)) return import(pathToFileURL(mjs).href + '?case=' + encodeURIComponent(activeId));
    const cjs = path.join(repo, 'dist/index.cjs');
    if (fs.existsSync(cjs)) return requireRepo(cjs);
    throw new Error(`built library not found under ${repo}/dist`);
  })();
  return libPromise;
}
function loadZod() {
  return requireRepo('zod');
}

let typeAuditPromise;
function typeAudit() {
  typeAuditPromise ??= (() => {
    const ts = requireRepo('typescript');
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'f2p-types-'));
    const file = path.join(tmp, 'probe.ts');
    const rel = p => path.relative(tmp, p).split(path.sep).join('/');
    const dist = path.join(repo, 'dist/index');
    const code = [
      `import { z } from "zod";`,
      `import { extendZodWithOpenApi, OpenApiGeneratorV3, type ZodOpenAPIMetadata } from ${JSON.stringify(rel(dist))};`,
      `extendZodWithOpenApi(z);`,
      `const base = z.object({ a: z.string() });`,
      `const extended = base.safeExtend({ b: z.number() });`,
      `extended.openapi("Extended");`,
      `const typed: z.ZodObject<{ a: z.ZodString; b: z.ZodNumber }> = extended;`,
      `const meta: ZodOpenAPIMetadata = { description: "d" };`,
      `const one = z.string().openapi(meta);`,
      `const two = z.string().openapi("Ref", { description: "d" });`,
      `new OpenApiGeneratorV3([one, two, extended]).generateComponents();`,
    ].join('\n');
    fs.writeFileSync(file, code);
    const options = {
      noEmit: true, strict: true, skipLibCheck: true, target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.NodeNext, moduleResolution: ts.ModuleResolutionKind.NodeNext,
      baseUrl: repo,
      paths: { 'zod': ['node_modules/zod'], 'zod/*': ['node_modules/zod/*'], '*': ['node_modules/*'] },
      types: [],
    };
    const program = ts.createProgram([file], options);
    const diagnostics = ts.getPreEmitDiagnostics(program);
    if (diagnostics.length) {
      const text = diagnostics.slice(0, 8).map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n')).join(' | ');
      throw new Error(`type audit failed: ${text}`);
    }
    return true;
  })();
  return typeAuditPromise;
}

function assertTargetTechnology(z) {
  const version = readZodVersion();
  assert.equal(version, '4.1.0', `expected Zod 4.1.0 but loaded ${version}`);
  assert.equal(typeof z.object({}).safeExtend, 'function', 'safeExtend must be available on ZodObject');
  assert.equal(typeof z.string().meta, 'function', 'meta must be available on Zod schemas');
}

function defs(schemas) {
  return schemas.map(schema => ({ type: 'schema', schema }));
}
function genComponents(Generator, schemas, options) {
  return new Generator(defs(schemas), options).generateComponents();
}
function schemasOf(components) {
  return components?.components?.schemas ?? {};
}

function jestCase(file, name) {
  return async () => {
    const bin = path.join(repo, 'node_modules/.bin/jest');
    if (!fs.existsSync(bin)) throw new Error(`jest binary not found at ${bin}`);
    const reportDir = fs.mkdtempSync(path.join(os.tmpdir(), 'f2p-jest-'));
    try {
      const reportPath = path.join(reportDir, 'results.json');
      const r = spawnSync(bin, ['--runTestsByPath', path.join(repo, file.split('::')[0]), '-t', name, '--runInBand', '--ci', '--silent', '--json', '--outputFile', reportPath], { cwd: repo, encoding: 'utf8', env: { ...process.env, CI: '1' } });
      if (r.error) throw r.error;
      if (r.status !== 0) throw new Error(`jest ${file} -t ${JSON.stringify(name)} failed (${r.status}): ${(r.stderr || r.stdout).slice(-2000)}`);
      // Jest exits successfully when -t matches no tests or only skipped tests.
      // A passing observation requires evidence that a test actually ran.
      const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
      assert.equal(report.success, true, `jest ${file}: test report did not succeed`);
      assert.ok(report.numPassedTests > 0, `jest ${file}: no matching test passed for ${JSON.stringify(name)}`);
    } finally {
      fs.rmSync(reportDir, { recursive: true, force: true });
    }
    const mod = loadZod(); const z = mod.z ?? mod;
    assertTargetTechnology(z); await typeAudit();
    return { jest: file, test: name };
  };
}

const cases = [];
function add(id, run) { cases.push({ id, run }); }

add('F2P-049', async () => {
  const z = loadZod().z ?? loadZod();
  const lib = await loadLib();
  lib.extendZodWithOpenApi(z);
  const s = z.string().meta({ description: 'meta', deprecated: true }).openapi('S', { description: 'openapi' });
  const cs = schemasOf(genComponents(lib.OpenApiGeneratorV3, [s]));
  assert.equal(cs.S.description, 'openapi');
  assert.equal(cs.S.deprecated, true);
  const s2 = z.string().meta({ id: 'Meta' }).openapi('Open');
  const cs2 = schemasOf(genComponents(lib.OpenApiGeneratorV3, [s2]));
  assert.ok(cs2.Open, 'Open component must exist');
  assert.ok(!cs2.Meta, 'Meta component must not exist');
  // Check the reverse call order as well. The existing .openapi().meta()
  // wrapper retains the later id as schema metadata while the OpenAPI refId
  // still wins for registration. Removing that field changes public output.
  const laterMeta = z.string().openapi('OpenBeforeMeta').meta({
    id: 'LaterMetaId', description: 'later metadata'
  });
  const expectedLaterMeta = {
    OpenBeforeMeta: { type: 'string', id: 'LaterMetaId', description: 'later metadata' }
  };
  for (const Generator of [lib.OpenApiGeneratorV3, lib.OpenApiGeneratorV31, lib.OpenApiGeneratorV32]) {
    const generated = schemasOf(genComponents(Generator, [laterMeta]));
    assert.deepEqual(generated, expectedLaterMeta, `${Generator.name}: .openapi().meta() output changed`);
    assert.equal(JSON.stringify(generated), JSON.stringify(expectedLaterMeta), `${Generator.name}: metadata field order changed`);
  }
  assertTargetTechnology(z); await typeAudit();
  return { components: Object.keys(cs), precedence: Object.keys(cs2) };
});

add('F2P-050', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib();
  lib.extendZodWithOpenApi(z);
  const one = z.string().openapi({ description: 'd' });
  const two = z.string().openapi('Ref', { description: 'd' });
  assert.equal(typeof one.openapi, 'function');
  assert.equal(typeof two.openapi, 'function');
  const extended = z.object({ a: z.string() }).safeExtend({ b: z.number() });
  assert.deepEqual(extended.parse({ a: 'x', b: 1 }), { a: 'x', b: 1 });
  assert.equal(typeof extended.openapi, 'function');
  assertTargetTechnology(z); await typeAudit();
  return { overloads: ['metadata', 'refId+metadata'], safeExtend: true };
});

add('F2P-051', jestCase('spec/modifiers/meta.spec.ts::meta', 'meta'));
add('F2P-052', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib();
  lib.extendZodWithOpenApi(z);
  const s = z.string().meta({ description: 'meta', deprecated: true }).openapi('S', { description: 'openapi' });
  const cs = schemasOf(genComponents(lib.OpenApiGeneratorV3, [s]));
  assert.equal(cs.S.description, 'openapi'); assert.equal(cs.S.deprecated, true);
  assertTargetTechnology(z); await typeAudit();
  return { S: cs.S };
});
add('F2P-053', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const recursive = z.object({ value: z.string(), get next() { return recursive.nullable(); } }).openapi('NullableRecursive');
  const v3 = schemasOf(genComponents(lib.OpenApiGeneratorV3, [recursive])).NullableRecursive;
  assert.deepEqual(v3.properties.next, { allOf: [{ $ref: '#/components/schemas/NullableRecursive' }, { nullable: true }] });
  const v31 = schemasOf(genComponents(lib.OpenApiGeneratorV31, [recursive])).NullableRecursive;
  assert.deepEqual(v31.properties.next, { oneOf: [{ $ref: '#/components/schemas/NullableRecursive' }, { type: 'null' }] });
  const already = z.string().nullable().openapi('Already');
  const wrapped = z.object({ key: already.nullable() }).openapi('Wrapper');
  const all = schemasOf(genComponents(lib.OpenApiGeneratorV31, [already, wrapped]));
  assert.deepEqual(all.Wrapper.properties.key, { $ref: '#/components/schemas/Already' });
  assertTargetTechnology(z); await typeAudit();
  return { v3: v3.properties.next, v31: v31.properties.next, alreadyNullable: all.Wrapper.properties.key };
});
add('F2P-054', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const A = z.object({ kind: z.literal('a') }).openapi('A');
  const B = z.object({ kind: z.literal('b') }).openapi('B');
  const du = z.discriminatedUnion('kind', [A, B]).openapi('DU');
  const d = schemasOf(genComponents(lib.OpenApiGeneratorV3, [A, B, du])).DU;
  assert.equal(d.oneOf.length, 2);
  assert.equal(d.discriminator.propertyName, 'kind');
  assert.deepEqual(d.discriminator.mapping, { a: '#/components/schemas/A', b: '#/components/schemas/B' });
  const duplicated = z.discriminatedUnion('kind', [A, B]).nullable().openapi('NullableDU');
  const nd = schemasOf(genComponents(lib.OpenApiGeneratorV3, [A, B, duplicated])).NullableDU;
  assert.ok(!('discriminator' in nd));
  assert.ok(nd.oneOf);
  // TypeScript numeric enums contain reverse-name entries. Those names are
  // not valid discriminator values and must not enter the OpenAPI mapping.
  const numericEntries = { 0: 'Zero', 1: 'One', Zero: 0, One: 1 };
  const Numeric = z.object({ kind: z.nativeEnum(numericEntries) }).openapi('NumericKind');
  const Text = z.object({ kind: z.literal('text') }).openapi('TextKind');
  assert.equal(Numeric.safeParse({ kind: 'Zero' }).success, false);
  assert.equal(Numeric.safeParse({ kind: 0 }).success, true);
  const numericUnion = z.discriminatedUnion('kind', [Numeric, Text]).openapi('NumericKindDU');
  for (const Generator of [lib.OpenApiGeneratorV3, lib.OpenApiGeneratorV31, lib.OpenApiGeneratorV32]) {
    const generated = schemasOf(genComponents(Generator, [Numeric, Text, numericUnion])).NumericKindDU;
    assert.equal(generated.oneOf.length, 2);
    assert.deepEqual(generated.discriminator, {
      propertyName: 'kind',
      mapping: { text: '#/components/schemas/TextKind' }
    });
  }
  // Removing an invalid entry must preserve the insertion order of every
  // surviving entry. A reverse name may also be a valid value in another
  // option; in that case its final reference comes from the actual value.
  const CollisionText = z.object({ kind: z.enum(['text', 'Zero']) }).openapi('CollisionText');
  const Nested = z.discriminatedUnion('variant', [
    z.object({ kind: z.nativeEnum(numericEntries), variant: z.literal('numeric') }),
    z.object({ kind: z.literal('nested'), variant: z.literal('text') })
  ]).openapi('NestedNumeric');
  // These are actual accepted string values, despite also being reverse
  // names. The enum transformer has its existing mixed-enum boundary, so
  // supply the same explicit metadata override supported by the baseline.
  const mixedEntries = { ...numericEntries, First: 'One', Second: 'Zero' };
  const Mixed = z.object({ kind: z.enum(mixedEntries).openapi({ type: 'string' }) }).openapi('MixedAlias');
  assert.equal(Mixed.safeParse({ kind: 'One' }).success, true);
  assert.equal(Mixed.safeParse({ kind: 'Zero' }).success, true);
  const mappingCases = [
    {
      name: 'CollisionDU', options: [Numeric, CollisionText],
      mapping: { Zero: '#/components/schemas/CollisionText', text: '#/components/schemas/CollisionText' }
    },
    {
      name: 'NestedCollisionDU', options: [Nested, CollisionText],
      mapping: {
        Zero: '#/components/schemas/CollisionText',
        nested: '#/components/schemas/NestedNumeric',
        text: '#/components/schemas/CollisionText'
      }
    },
    {
      name: 'MixedAliasDU', options: [Mixed, Text],
      mapping: {
        Zero: '#/components/schemas/MixedAlias',
        One: '#/components/schemas/MixedAlias',
        text: '#/components/schemas/TextKind'
      }
    },
    { name: 'NumericOnlyDU', options: [Numeric], mapping: {} }
  ];
  const refs = new Map([
    [Numeric, 'NumericKind'], [CollisionText, 'CollisionText'],
    [Nested, 'NestedNumeric'], [Mixed, 'MixedAlias'], [Text, 'TextKind']
  ]);
  for (const { name, options, mapping } of mappingCases) {
    const union = z.discriminatedUnion('kind', options).openapi(name);
    const expected = {
      oneOf: options.map(option => ({ $ref: `#/components/schemas/${refs.get(option)}` })),
      discriminator: { propertyName: 'kind', mapping }
    };
    for (const Generator of [lib.OpenApiGeneratorV3, lib.OpenApiGeneratorV31, lib.OpenApiGeneratorV32]) {
      const generated = schemasOf(genComponents(Generator, [...options, union]))[name];
      assert.deepEqual(generated, expected, `${Generator.name}: ${name} values or references changed`);
      assert.equal(JSON.stringify(generated), JSON.stringify(expected), `${Generator.name}: ${name} mapping order changed`);
    }
  }
  // Preserve the existing mapping boundary: only direct literal/enum
  // discriminator properties contribute values. Adding wrapper unwrapping
  // would introduce new mappings, contrary to the output-preservation and
  // no-new-public-features requirements. These remain valid union inputs.
  const wrappedDiscriminators = [
    ['DefaultLiteral', () => z.literal('wrapped').default('wrapped')],
    ['OptionalLiteral', () => z.literal('wrapped').optional()],
    ['ReadonlyEnum', () => z.enum(['wrapped', 'other-wrapped']).readonly()]
  ];
  for (const [suffix, makeDiscriminator] of wrappedDiscriminators) {
    const wrappedId = `Wrapped${suffix}`;
    const plainId = `Plain${suffix}`;
    const unionId = `Union${suffix}`;
    const Wrapped = z.object({ kind: makeDiscriminator() }).openapi(wrappedId);
    const Plain = z.object({ kind: z.literal('plain') }).openapi(plainId);
    const union = z.discriminatedUnion('kind', [Wrapped, Plain]).openapi(unionId);
    assert.equal(union.safeParse({ kind: 'wrapped' }).success, true);
    const expected = {
      oneOf: [
        { $ref: `#/components/schemas/${wrappedId}` },
        { $ref: `#/components/schemas/${plainId}` }
      ],
      discriminator: {
        propertyName: 'kind',
        mapping: { plain: `#/components/schemas/${plainId}` }
      }
    };
    for (const Generator of [lib.OpenApiGeneratorV3, lib.OpenApiGeneratorV31, lib.OpenApiGeneratorV32]) {
      const generated = schemasOf(genComponents(Generator, [Wrapped, Plain, union]))[unionId];
      assert.deepEqual(generated, expected, `${Generator.name}: ${suffix} discriminator mapping changed`);
      assert.equal(JSON.stringify(generated), JSON.stringify(expected), `${Generator.name}: ${suffix} serialization changed`);
    }
  }
  assertTargetTechnology(z); await typeAudit();
  return { oneOf: d.oneOf.length, discriminator: d.discriminator, nullableHasDiscriminator: 'discriminator' in nd };
});
add('F2P-055', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const plain = z.string().openapi('S');
  const cs = schemasOf(genComponents(lib.OpenApiGeneratorV31, [plain]));
  assert.deepEqual(cs, { S: { type: 'string' } });
  const registry = new lib.OpenAPIRegistry();
  registry.register('ReverseSchema', z.string());
  registry.registerParameter('ReverseParameter', z.string().openapi({ param: { in: 'query' } }));
  registry.registerComponent('schemas', 'ReverseComponent', { type: 'object' });
  registry.registerPath({ method: 'get', path: '/reverse', responses: { 200: { description: 'ok' } } });
  registry.registerWebhook({ method: 'get', path: '/reverse-hook', responses: { 200: { description: 'ok' } } });
  const doc = new lib.OpenApiGeneratorV31(registry.definitions).generateDocument({ openapi: '3.1.0', info: { title: 't', version: '1' } });
  assert.ok(doc.components.schemas.S || doc.components.schemas.ReverseComponent);
  assertTargetTechnology(z); await typeAudit();
  return { plain: cs, hasWebhook: Boolean(doc.webhooks?.['/reverse-hook']) };
});
add('F2P-056', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const s = z.string().meta({ id: 'X' });
  const components = genComponents(lib.OpenApiGeneratorV3, [s]);
  assert.equal(components.components.schemas.X.type, 'string');
  const roundTrip = JSON.parse(JSON.stringify(components));
  assert.deepEqual(roundTrip, components);
  const nested = z.object({ id: z.string().openapi('NestedId') }).openapi('Nested');
  const doc = new lib.OpenApiGeneratorV3([{ type: 'schema', schema: nested }]).generateDocument({ openapi: '3.0.0', info: { title: 't', version: '1' } });
  assert.ok(doc.paths); assert.ok(doc.components);
  assert.equal(typeof JSON.stringify(doc), 'string');
  // In Zod 4.0 partialRecord used a union key and therefore generated
  // additionalProperties. Zod 4.1 uses an enum clone without _zod.values;
  // this internal change must not turn partial records into fixed properties.
  for (const Generator of [lib.OpenApiGeneratorV3, lib.OpenApiGeneratorV31, lib.OpenApiGeneratorV32]) {
    const numericKeys = { 0: 'Zero', 1: 'One', Zero: 0, One: 1 };
    const partials = [
      z.partialRecord(z.enum(['left', 'right']), z.number()),
      z.partialRecord(z.nativeEnum(numericKeys), z.number())
    ];
    for (const [index, partial] of partials.entries()) {
      const plainName = `Partial${index}`;
      const nullableName = `NullablePartial${index}`;
      const generated = schemasOf(genComponents(Generator, [
        partial.openapi(plainName), partial.nullable().openapi(nullableName)
      ]));
      const expected = { type: 'object', additionalProperties: { type: 'number' } };
      const nullableType = Generator === lib.OpenApiGeneratorV3
        ? { type: 'object', nullable: true }
        : { type: ['object', 'null'] };
      const expectedNullable = { ...nullableType, additionalProperties: { type: 'number' } };
      assert.deepEqual(generated[plainName], expected, `${Generator.name}: partial enum record changed`);
      assert.equal(JSON.stringify(generated[plainName]), JSON.stringify(expected));
      assert.deepEqual(generated[nullableName], expectedNullable, `${Generator.name}: nullable partial record changed`);
      assert.equal(JSON.stringify(generated[nullableName]), JSON.stringify(expectedNullable));
    }
    const node = z.object({
      name: z.string(),
      get children() { return z.partialRecord(z.enum(['left', 'right']), node); }
    }).openapi('PartialNode');
    const generated = schemasOf(genComponents(Generator, [node]));
    const expectedNode = {
      type: 'object',
      properties: {
        name: { type: 'string' },
        children: { type: 'object', additionalProperties: { $ref: '#/components/schemas/PartialNode' } }
      },
      required: ['name', 'children']
    };
    assert.deepEqual(generated.PartialNode, expectedNode, `${Generator.name}: recursive partial record changed`);
    assert.equal(JSON.stringify(generated.PartialNode), JSON.stringify(expectedNode));
    // Ordinary exhaustive enum-key records keep their existing properties.
    const exhaustive = z.record(z.enum(['left', 'right']), z.number()).openapi('Exhaustive');
    assert.deepEqual(schemasOf(genComponents(Generator, [exhaustive])).Exhaustive, {
      type: 'object', properties: { left: { type: 'number' }, right: { type: 'number' } }
    });
  }
  assertTargetTechnology(z); await typeAudit();
  return { metaComponent: components.components.schemas.X, jsonRoundTrip: true };
});
add('F2P-057', jestCase('spec/types/discriminated-union.spec.ts::can automatically register discriminated union items', 'can automatically register discriminated union items'));
add('F2P-058', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const A = z.object({ kind: z.literal('a') });
  const B = z.object({ kind: z.literal('b') });
  const du = z.discriminatedUnion('kind', [A, B]);
  du._zod.def.discriminator = undefined;
  const schema = du.openapi('NoDiscriminator');
  const original = console.error; const calls = [];
  console.error = (...a) => calls.push(a);
  let components;
  try { components = genComponents(lib.OpenApiGeneratorV3, [schema]); } finally { console.error = original; }
  assert.equal(calls[0][0], 'No discriminator found for discriminated union');
  assert.equal(calls[0][1], schema);
  assert.ok(schemasOf(components).NoDiscriminator.oneOf);
  assertTargetTechnology(z); await typeAudit();
  return { message: calls[0][0], oneOf: schemasOf(components).NoDiscriminator.oneOf.length };
});
add('F2P-059', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const s = z.string().meta({ description: 'meta', deprecated: true }).openapi('S', { description: 'openapi' });
  assert.equal(schemasOf(genComponents(lib.OpenApiGeneratorV3, [s])).S.description, 'openapi');
  assertTargetTechnology(z); await typeAudit();
  return { precedence: 'openapi' };
});
add('F2P-060', jestCase('spec/modifiers/pipe.spec.ts::can generate schema for pipes', 'can generate schema for pipes'));
add('F2P-061', jestCase('spec/types/recursive-schemas.spec.ts::handles multiple independent recursive schemas efficiently using getters', 'handles multiple independent recursive schemas efficiently using getters'));
add('F2P-062', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const du = z.discriminatedUnion('kind', [z.object({ kind: z.literal('a') }), z.object({ kind: z.literal('b') })]);
  du._zod.def.discriminator = undefined;
  const schema = du.openapi('NoDiscriminatorType');
  const original = console.error; const calls = [];
  console.error = (...a) => calls.push(a);
  let components;
  try { components = genComponents(lib.OpenApiGeneratorV3, [schema]); } finally { console.error = original; }
  assert.equal(calls[0]?.[0], 'No discriminator found for discriminated union');
  assert.ok(schemasOf(components).NoDiscriminatorType.oneOf);
  assertTargetTechnology(z); await typeAudit();
  return { message: calls[0]?.[0] };
});
add('F2P-063', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const registry = new lib.OpenAPIRegistry();
  const generator = new lib.OpenApiGeneratorV31([
    { type: 'schema', schema: z.string().meta({ id: 'X' }) },
    { type: 'webhook', webhook: { method: 'get', path: '/hook', responses: { 200: { description: 'ok' } } } },
  ]);
  const components = generator.generateComponents();
  assert.ok(schemasOf(components).X);
  const doc = generator.generateDocument({ openapi: '3.1.0', info: { title: 't', version: '1' } });
  assert.ok(doc.webhooks && doc.webhooks['/hook']);
  assertTargetTechnology(z); await typeAudit();
  return { component: schemasOf(components).X, webhook: Boolean(doc.webhooks['/hook']) };
});
add('F2P-064', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const s1 = z.string().default('x').openapi('Defaults');
  assert.deepEqual(schemasOf(genComponents(lib.OpenApiGeneratorV3, [s1])).Defaults, { type: 'string', default: 'x' });
  const s2 = z.string().pipe(z.string().min(1)).openapi('Piped');
  assert.deepEqual(schemasOf(genComponents(lib.OpenApiGeneratorV3, [s2])).Piped, { type: 'string' });
  const s3 = z.lazy(() => z.string()).openapi('Lazy');
  assert.deepEqual(schemasOf(genComponents(lib.OpenApiGeneratorV3, [s3])).Lazy, { type: 'string' });
  assertTargetTechnology(z); await typeAudit();
  return { default: 'x', pipe: true, lazy: true };
});
add('F2P-065', jestCase('spec/types/recursive-schemas.spec.ts::OpenAPI version compatibility with recursive getters', 'OpenAPI version compatibility with recursive getters'));
add('F2P-066', jestCase('spec/types/recursive-schemas.spec.ts::complex recursive scenarios with getters', 'complex recursive scenarios with getters'));
add('F2P-067', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const s = z.string().meta({ id: 'MetaPriority' }).openapi('OpenPriority');
  const cs = schemasOf(genComponents(lib.OpenApiGeneratorV3, [s]));
  assert.ok(cs.OpenPriority); assert.ok(!cs.MetaPriority);
  assertTargetTechnology(z); await typeAudit();
  return { components: Object.keys(cs) };
});
add('F2P-068', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const A = z.object({ kind: z.literal('a') }).openapi('A');
  const B = z.object({ kind: z.literal('b') }).openapi('B');
  const d = schemasOf(genComponents(lib.OpenApiGeneratorV3, [A, B, z.discriminatedUnion('kind', [A, B]).openapi('DU')])).DU;
  assert.deepEqual(d.discriminator.mapping, { a: '#/components/schemas/A', b: '#/components/schemas/B' });
  assertTargetTechnology(z); await typeAudit();
  return { discriminator: d.discriminator };
});
add('F2P-069', jestCase('spec/modifiers/meta.spec.ts::should override metadata when calling .meta', 'should override metadata when calling .meta'));
add('F2P-070', jestCase('spec/types/discriminated-union.spec.ts::does not create a discriminator mapping when the discrimnated union is nullable', 'does not create a discriminator mapping when the discrimnated union is nullable'));
add('F2P-071', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const du = z.discriminatedUnion('kind', [z.object({ kind: z.literal('a') }), z.object({ kind: z.literal('b') })]);
  du._zod.def.discriminator = undefined;
  const schema = du.openapi('NoDiscriminatorRecursive');
  const original = console.error; const calls = [];
  console.error = (...a) => calls.push(a);
  let components;
  try { components = genComponents(lib.OpenApiGeneratorV3, [schema]); } finally { console.error = original; }
  assert.equal(calls[0]?.[0], 'No discriminator found for discriminated union');
  assert.ok(schemasOf(components).NoDiscriminatorRecursive.oneOf);
  assertTargetTechnology(z); await typeAudit();
  return { message: calls[0]?.[0] };
});
add('F2P-072', async () => {
  const mod = loadZod(); const z = mod.z ?? mod;
  const lib = await loadLib(); lib.extendZodWithOpenApi(z);
  const s = z.string().default('x').openapi('ModifierDefault');
  assert.deepEqual(schemasOf(genComponents(lib.OpenApiGeneratorV3, [s])).ModifierDefault, { type: 'string', default: 'x' });
  const piped = z.string().pipe(z.string().min(2)).openapi('ModifierPipe');
  assert.deepEqual(schemasOf(genComponents(lib.OpenApiGeneratorV3, [piped])).ModifierPipe, { type: 'string' });
  assertTargetTechnology(z); await typeAudit();
  return { default: 'x', pipe: true };
});

const selected = args.only ? cases.filter(c => args.only.split(',').includes(c.id)) : cases;
const results = [];
if (!args.only) {
  for (const c of cases) {
    const r = spawnSync(process.execPath, [process.argv[1], '--repo', repo, '--only', c.id], { cwd: repo, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024, env: process.env });
    try {
      const child = JSON.parse(r.stdout);
      results.push(child.results[0]);
    } catch {
      results.push({ observation_id: c.id, status: 'INFRA_ERROR', message: (r.error || r.stderr || r.stdout || 'child process failed').toString().slice(0, 4000) });
    }
  }
} else {
  for (const c of selected) {
    activeId = c.id;
    try {
      const evidence = await c.run();
      results.push({ observation_id: c.id, status: 'PASS', evidence });
    } catch (e) {
      const message = e && e.stack ? e.stack : (e && e.message) || String(e);
      const infra = /Cannot find module|ERR_MODULE_NOT_FOUND|built library not found|jest binary not found|ENOENT/.test(message);
      results.push({ observation_id: c.id, status: infra ? 'INFRA_ERROR' : 'FAIL', message: message.slice(0, 4000) });
    }
  }
}
const report = { repo, zod_version: readZodVersion(), results };
const json = JSON.stringify(report, null, 2);
if (args.report) fs.writeFileSync(args.report, json);
console.log(json);
