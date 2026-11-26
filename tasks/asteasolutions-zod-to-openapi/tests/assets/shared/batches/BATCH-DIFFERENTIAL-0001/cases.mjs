import fs from 'node:fs';
import path from 'node:path';

export const BATCH_ID = 'BATCH-DIFFERENTIAL-0001';

export const OBSERVATION_IDS = [
  'DIFF-001','DIFF-002','DIFF-003','DIFF-004','DIFF-005','DIFF-006',
  'DIFF-007','DIFF-008','DIFF-009','DIFF-010','DIFF-011','DIFF-012',
  'DIFF-013','DIFF-014','DIFF-015','DIFF-016','DIFF-017','DIFF-018',
  'DIFF-019','DIFF-020','DIFF-021','DIFF-022','DIFF-023','DIFF-024',
  'DIFF-025','DIFF-026','DIFF-027','DIFF-028','DIFF-029','DIFF-030',
  'DIFF-031','DIFF-032'
];

export function stable(value) {
  if (value === undefined) return null;
  if (value instanceof Date) return value.toISOString();
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') {
    const out = {};
    for (const key of Object.keys(value).sort()) out[key] = stable(value[key]);
    return out;
  }
  return value;
}

export function stableStringify(value) {
  return JSON.stringify(stable(value));
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function generatorFor(lib, version, definitions, options) {
  if (version === '3.0.0') return new lib.OpenApiGeneratorV3(definitions, options);
  if (version === '3.1.0') return new lib.OpenApiGeneratorV31(definitions, options);
  if (version === '3.2.0') return new lib.OpenApiGeneratorV32(definitions, options);
  throw new Error(`unsupported version ${version}`);
}

function componentsFor(lib, version, zodSchemas, options) {
  const definitions = zodSchemas.map(schema => ({ type: 'schema', schema }));
  const generator = generatorFor(lib, version, definitions, options);
  return plain(generator.generateComponents());
}

function docConfig(version, extra = {}) {
  return {
    openapi: version,
    info: { title: 'Differential corpus', version: '1.0.0' },
    ...extra
  };
}

function route(overrides = {}) {
  return {
    method: 'get',
    path: '/items/{id}',
    responses: { 200: { description: 'OK' } },
    ...overrides
  };
}

function captureConsoleError(fn) {
  const calls = [];
  const original = console.error;
  console.error = (...args) => {
    calls.push(args.map(arg => {
      if (arg && typeof arg === 'object') {
        return { type: arg.def?.type ?? arg._zod?.def?.type ?? 'object' };
      }
      return arg;
    }));
  };
  try {
    const value = fn();
    return { value: plain(value), calls };
  } finally {
    console.error = original;
  }
}

function errorDescriptor(fn) {
  try {
    fn();
    return { threw: false };
  } catch (error) {
    return {
      threw: true,
      name: error?.constructor?.name ?? typeof error,
      message: String(error?.message ?? error),
      isZodToOpenAPIError: error?.constructor?.name === 'ZodToOpenAPIError'
    };
  }
}

function schemaKeys(components) {
  return Object.keys(components?.components?.schemas ?? {});
}

export function createCases(ctx) {
  const { z, lib, repoRoot } = ctx;
  lib.extendZodWithOpenApi(z);

  const cases = {};

  cases['DIFF-001'] = () => {
    const { OpenAPIRegistry, OpenApiGeneratorV3, OpenApiGeneratorV31, OpenApiGeneratorV32 } = lib;
    const registry = new OpenAPIRegistry();
    const Thing = registry.register('Thing', z.object({
      id: z.string().uuid(),
      count: z.number().int().min(0),
      active: z.boolean().optional(),
      tags: z.array(z.string()).default([])
    }));
    registry.register('Catchall', z.object({}).catchall(z.string()));
    registry.register('RecordMap', z.record(z.string(), z.number()));
    registry.register('Intersected', z.intersection(z.object({ a: z.string() }), z.object({ b: z.number() })));
    registry.register('Literal', z.literal('fixed'));
    registry.register('Pattern', z.string().regex(/^[a-z]+$/));
    registry.register('DateValue', z.date());
    registry.register('BigValue', z.bigint());
    registry.register('UnionValue', z.union([z.string(), z.number()]));
    registry.registerParameter('ThingId', z.string().openapi({ param: { in: 'path', description: 'thing id' } }));
    registry.registerPath(route({
      request: {
        params: z.object({ id: z.string().openapi({ param: { in: 'path' } }) }),
        query: z.object({ filter: z.string().optional() }),
        body: { content: { 'application/json': { schema: Thing } } }
      },
      responses: {
        200: { description: 'OK', content: { 'application/json': { schema: Thing } } },
        404: { description: 'Missing' }
      }
    }));
    registry.registerWebhook(route({
      method: 'post',
      path: '/thing-changed',
      request: { body: { content: { 'application/json': { schema: Thing } } } },
      responses: { 200: { description: 'accepted' } }
    }));
    registry.registerComponent('responses', 'NotFound', { description: 'Not found' });
    const definitions = registry.definitions;
    return {
      v3: plain(new OpenApiGeneratorV3(definitions).generateDocument(docConfig('3.0.0'))),
      v31: plain(new OpenApiGeneratorV31(definitions).generateDocument(docConfig('3.1.0'))),
      v32: plain(new OpenApiGeneratorV32(definitions).generateDocument(docConfig('3.2.0')))
    };
  };

  cases['DIFF-002'] = () => {
    const expected = [
      'OpenAPIRegistry','OpenApiGeneratorV3','OpenApiGeneratorV31','OpenApiGeneratorV32',
      'RouteConfig','ResponseConfig','ZodContentObject','ZodMediaTypeObject','ZodOpenAPIMetadata',
      'ZodRequestBody','extendZodWithOpenApi','zodToOpenAPIRegistry'
    ];
    const generatorMethods = name => Object.getOwnPropertyNames(lib[name].prototype).sort();
    const registryMethods = Object.getOwnPropertyNames(lib.OpenAPIRegistry.prototype).sort();
    return {
      exports: Object.keys(lib).sort(),
      expectedExports: expected,
      registryMethods,
      v3Methods: generatorMethods('OpenApiGeneratorV3'),
      v31Methods: generatorMethods('OpenApiGeneratorV31'),
      v32Methods: generatorMethods('OpenApiGeneratorV32')
    };
  };

  cases['DIFF-003'] = () => {
    const names = Object.keys(lib).sort();
    return names.map(name => ({ name, typeof: typeof lib[name] }));
  };

  cases['DIFF-004'] = () => {
    const overloadOne = z.string().openapi({ description: 'd' });
    const overloadTwo = z.string().openapi('Ref', { description: 'd' });
    const components = componentsFor(lib, '3.1.0', [overloadTwo], undefined);
    return {
      prototypeOpenapi: typeof z.ZodType.prototype.openapi,
      oneReturnsOpenapi: typeof overloadOne.openapi,
      twoReturnsOpenapi: typeof overloadTwo.openapi,
      oneType: overloadOne.constructor.name,
      twoType: overloadTwo.constructor.name,
      components
    };
  };

  cases['DIFF-005'] = () => {
    const registry = new lib.OpenAPIRegistry();
    registry.register('A', z.string());
    registry.registerParameter('P', z.string().openapi({ param: { in: 'query' } }));
    registry.registerPath(route({ path: '/a' }));
    registry.registerWebhook(route({ path: '/hook', method: 'post' }));
    registry.registerComponent('schemas', 'Raw', { type: 'object' });
    return registry.definitions.map(definition => ({
      type: definition.type,
      componentType: definition.componentType,
      name: definition.name,
      refId: definition.schema ? lib.zodToOpenAPIRegistry.get(definition.schema)?._internal?.refId : undefined,
      path: definition.route?.path ?? definition.webhook?.path,
      method: definition.route?.method ?? definition.webhook?.method
    }));
  };

  cases['DIFF-006'] = () => {
    const definitions = [
      { type: 'webhook', webhook: route({ path: '/hook', method: 'post' }) },
      { type: 'route', route: route({ path: '/route' }) },
      { type: 'component', componentType: 'schemas', name: 'Raw', component: { type: 'object' } },
      { type: 'parameter', schema: z.string().openapi('Param', { param: { in: 'query' } }) },
      { type: 'schema', schema: z.string().openapi('Schema') }
    ];
    new lib.OpenApiGeneratorV31(definitions);
    return definitions.map(definition => definition.type);
  };

  cases['DIFF-007'] = () => {
    const registry = new lib.OpenAPIRegistry();
    registry.register('S', z.string());
    registry.registerWebhook(route({ path: '/hook', method: 'post' }));
    const document = plain(new lib.OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig('3.0.0')));
    return {
      hasPaths: 'paths' in document,
      hasComponents: 'components' in document,
      hasWebhooks: Object.prototype.hasOwnProperty.call(document, 'webhooks'),
      webhooksValue: document.webhooks ?? null
    };
  };

  cases['DIFF-008'] = () => {
    const registry = new lib.OpenAPIRegistry();
    registry.register('S', z.string());
    registry.registerWebhook(route({ path: '/hook', method: 'post', responses: { 200: { description: 'ok' } } }));
    const document = plain(new lib.OpenApiGeneratorV31(registry.definitions).generateDocument(docConfig('3.1.0')));
    return {
      hasPaths: 'paths' in document,
      hasComponents: 'components' in document,
      webhookPaths: Object.keys(document.webhooks ?? {}),
      webhookMethods: Object.keys(document.webhooks?.['/hook'] ?? {})
    };
  };

  cases['DIFF-009'] = () => {
    const registry = new lib.OpenAPIRegistry();
    registry.register('S', z.string());
    registry.registerWebhook(route({ path: '/hook', method: 'post', responses: { 200: { description: 'ok' } } }));
    const document = plain(new lib.OpenApiGeneratorV32(registry.definitions).generateDocument(docConfig('3.2.0')));
    return {
      hasPaths: 'paths' in document,
      hasComponents: 'components' in document,
      webhookPaths: Object.keys(document.webhooks ?? {}),
      webhookMethods: Object.keys(document.webhooks?.['/hook'] ?? {})
    };
  };

  cases['DIFF-010'] = () => {
    const child = z.object({ value: z.string() }).openapi('Child');
    const parent = z.object({ child, children: z.array(child) }).openapi('Parent');
    const components = componentsFor(lib, '3.1.0', [parent, child], undefined);
    const text = JSON.stringify(components);
    const reparsed = JSON.parse(text);
    return {
      roundTrips: stableStringify(reparsed) === stableStringify(components),
      parent: components.components.schemas.Parent,
      child: components.components.schemas.Child,
      refs: JSON.stringify(components).match(/#\/components\/schemas\/[A-Za-z0-9_]+/g) ?? []
    };
  };

  cases['DIFF-011'] = () => {
    const unknown = errorDescriptor(() => componentsFor(lib, '3.0.0', [z.symbol().openapi('Symbol')], undefined));
    const missing = errorDescriptor(() => {
      const registry = new lib.OpenAPIRegistry();
      registry.registerPath(route({ request: { query: z.string() }, responses: { 200: { description: 'ok' } } }));
      return new lib.OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig('3.0.0'));
    });
    const conflict = errorDescriptor(() => {
      const registry = new lib.OpenAPIRegistry();
      registry.registerPath(route({
        request: { query: z.object({ test: z.string().openapi({ param: { name: 'another' } }) }) },
        responses: { 200: { description: 'ok' } }
      }));
      return new lib.OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig('3.0.0'));
    });
    return { unknown, missing, conflict };
  };

  cases['DIFF-012'] = () => {
    const dist = path.join(repoRoot, 'dist');
    const entries = ['index.cjs', 'index.mjs', 'index.d.ts'];
    const files = Object.fromEntries(entries.map(name => {
      const file = path.join(dist, name);
      const exists = fs.existsSync(file);
      return [name, { exists, nonEmpty: exists && fs.statSync(file).size > 0 }];
    }));
    const pkg = JSON.parse(fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf8'));
    const declaration = files['index.d.ts'].exists
      ? fs.readFileSync(path.join(dist, 'index.d.ts'), 'utf8')
      : '';
    const declarationExports = [];
    for (const match of declaration.matchAll(/export\s*\{([^}]+)\}/g)) {
      for (const part of match[1].split(',')) {
        const name = part.trim().split(/\s+as\s+/).pop().trim();
        if (name) declarationExports.push(name);
      }
    }
    for (const match of declaration.matchAll(/export\s+\*\s+from\s+'([^']+)'/g)) {
      declarationExports.push('*:' + match[1]);
    }
    return {
      files,
      packageEntryFields: { main: pkg.main, module: pkg.module, types: pkg.types },
      declarationExports: declarationExports.sort(),
      declarationHasExportBlock: /export\s*\{/.test(declaration)
    };
  };

  cases['DIFF-013'] = () => {
    const schemas = [
      z.number().int().min(0).openapi('NumericBounds'),
      z.string().email().openapi('Email'),
      z.string().regex(/^abc$/).openapi('Pattern'),
      z.object({}).catchall(z.string()).openapi('Catchall'),
      z.record(z.string(), z.number()).openapi('Record'),
      z.intersection(z.object({ a: z.string() }), z.object({ b: z.number() })).openapi('Intersection'),
      z.literal('literal-value').openapi('Literal'),
      z.templateLiteral(['prefix-', z.string()]).openapi('Template'),
      z.date().openapi('Date'),
      z.bigint().openapi('BigInt'),
      z.array(z.string()).openapi('Array'),
      z.union([z.string(), z.number()]).openapi('Union')
    ];
    return componentsFor(lib, '3.1.0', schemas, undefined);
  };

  cases['DIFF-014'] = () => {
    const make = version => {
      const components = componentsFor(lib, version, [z.object({ value: z.string().nullable() }).openapi('Inline')], undefined);
      return components.components.schemas.Inline.properties.value;
    };
    return { v3: make('3.0.0'), v31: make('3.1.0'), v32: make('3.2.0') };
  };

  cases['DIFF-015'] = () => {
    const make = version => {
      const StringSchema = z.string().openapi('String');
      const TestSchema = z.object({ key: StringSchema.nullable(), already: z.string().openapi('Already').nullable().nullable() }).openapi('Test');
      const components = componentsFor(lib, version, [StringSchema, TestSchema], undefined);
      return {
        string: components.components.schemas.String,
        key: components.components.schemas.Test.properties.key,
        already: components.components.schemas.Test.properties.already
      };
    };
    return { v3: make('3.0.0'), v31: make('3.1.0'), v32: make('3.2.0') };
  };

  cases['DIFF-016'] = () => {
    const make = version => {
      const recursive = z.object({ value: z.string(), get next() { return recursive.nullable(); } }).openapi('NullableRecursive');
      const Already = z.object({ name: z.string() }).openapi('AlreadyNullable').nullable();
      const holder = z.object({ node: recursive, already: Already }).openapi('Holder');
      const components = componentsFor(lib, version, [recursive, Already, holder], undefined);
      return {
        next: components.components.schemas.NullableRecursive.properties.next,
        already: components.components.schemas.Holder.properties.already
      };
    };
    return { v3: make('3.0.0'), v31: make('3.1.0'), v32: make('3.2.0') };
  };

  cases['DIFF-017'] = () => {
    const make = version => componentsFor(lib, version, [z.tuple([z.string(), z.number()]).openapi('Tuple')], undefined).components.schemas.Tuple;
    return { v3: make('3.0.0'), v31: make('3.1.0'), v32: make('3.2.0') };
  };

  cases['DIFF-018'] = () => {
    const nullable = componentsFor(lib, '3.1.0', [z.enum(['a', 'b']).nullable().openapi('NullableEnum')], undefined).components.schemas.NullableEnum;
    const numeric = componentsFor(lib, '3.1.0', [z.nativeEnum({ One: 1, Two: 2 }).openapi('NumericEnum')], undefined).components.schemas.NumericEnum;
    const strings = componentsFor(lib, '3.1.0', [z.nativeEnum({ A: 'a', B: 'b' }).openapi('StringEnum')], undefined).components.schemas.StringEnum;
    const mixed = errorDescriptor(() => componentsFor(lib, '3.1.0', [z.nativeEnum({ A: 1, B: 'b' }).openapi('MixedEnum')], undefined));
    return { nullable, numeric, strings, mixed };
  };

  cases['DIFF-019'] = () => {
    const A = z.object({ kind: z.literal('a'), value: z.string() }).openapi('A');
    const B = z.object({ kind: z.literal('b'), value: z.number() }).openapi('B');
    const union = z.discriminatedUnion('kind', [A, B]).openapi('Union');
    const nullable = z.discriminatedUnion('kind', [A, B]).nullable().openapi('NullableUnion');
    const C = z.object({ kind: z.literal('c') });
    const missingRef = z.discriminatedUnion('kind', [A, C]).openapi('MissingRefUnion');
    const nestedInner = z.discriminatedUnion('kind', [A, B]);
    const nested = z.discriminatedUnion('kind', [nestedInner, z.object({ kind: z.literal('c') }).openapi('C')]).openapi('NestedUnion');
    const numberKind = z.discriminatedUnion('kind', [
      z.object({ kind: z.literal(1) }).openapi('One'),
      z.object({ kind: z.literal(2) }).openapi('Two')
    ]).openapi('NumberKindUnion');
    return componentsFor(lib, '3.1.0', [A, B, C, union, nullable, missingRef, nested, numberKind], undefined);
  };

  cases['DIFF-020'] = () => {
    const A = z.object({ kind: z.literal('a') }).openapi('A');
    const B = z.object({ kind: z.literal('b') }).openapi('B');
    const union = z.discriminatedUnion('kind', [A, B]);
    union._zod.def.discriminator = undefined;
    const schema = union.openapi('NoDiscriminator');
    const captured = captureConsoleError(() => componentsFor(lib, '3.1.0', [A, B, schema], undefined));
    return {
      calls: captured.calls,
      schema: captured.value.components.schemas.NoDiscriminator
    };
  };

  cases['DIFF-021'] = () => {
    const registry = new lib.OpenAPIRegistry();
    registry.register('Zeta', z.string());
    registry.register('Alpha', z.string());
    registry.registerParameter('ZParam', z.string().openapi({ param: { in: 'query' } }));
    registry.registerParameter('AParam', z.string().openapi({ param: { in: 'query' } }));
    const sorted = plain(new lib.OpenApiGeneratorV31(registry.definitions, { sortComponents: 'alphabetically' }).generateComponents());
    const unsortedRegistry = new lib.OpenAPIRegistry();
    unsortedRegistry.register('Zeta', z.string());
    unsortedRegistry.register('Alpha', z.string());
    unsortedRegistry.registerParameter('ZParam', z.string().openapi({ param: { in: 'query' } }));
    unsortedRegistry.registerParameter('AParam', z.string().openapi({ param: { in: 'query' } }));
    const unsorted = plain(new lib.OpenApiGeneratorV31(unsortedRegistry.definitions).generateComponents());
    return {
      sortedSchemas: Object.keys(sorted.components.schemas),
      sortedParameters: Object.keys(sorted.components.parameters),
      registrationSchemas: Object.keys(unsorted.components.schemas),
      registrationParameters: Object.keys(unsorted.components.parameters)
    };
  };

  cases['DIFF-022'] = () => {
    const generatorOption = componentsFor(lib, '3.1.0', [z.union([z.string(), z.number()]).openapi('GeneratorUnion')], { unionPreferredType: 'oneOf' });
    const perSchema = componentsFor(lib, '3.1.0', [z.union([z.string(), z.number()]).openapi('SchemaUnion', {}, { unionPreferredType: 'anyOf' })], undefined);
    return {
      generator: generatorOption.components.schemas.GeneratorUnion,
      perSchema: perSchema.components.schemas.SchemaUnion
    };
  };

  cases['DIFF-023'] = () => {
    const merged = z.string().meta({ description: 'meta', deprecated: true }).openapi('S', { description: 'openapi' });
    const idPrecedence = z.string().meta({ id: 'Meta' }).openapi('Open');
    const components = componentsFor(lib, '3.1.0', [merged, idPrecedence], undefined);
    return {
      schemas: components.components.schemas,
      hasMetaKey: Object.prototype.hasOwnProperty.call(components.components.schemas, 'Meta')
    };
  };

  cases['DIFF-024'] = () => {
    const schemas = [
      z.string().default('x').openapi('Defaulted'),
      z.string().prefault('x').openapi('Prefaulted'),
      z.string().pipe(z.string()).openapi('Piped'),
      z.string().refine(value => value.length > 0).openapi('Refined'),
      z.string().transform(value => value.length).openapi('Transformed'),
      z.lazy(() => z.string()).openapi('Lazy')
    ];
    return componentsFor(lib, '3.1.0', schemas, undefined);
  };

  cases['DIFF-025'] = () => {
    const Tree = z.object({
      name: z.string(),
      get child() { return Tree.nullable().optional(); },
      get children() { return z.array(Tree); },
      get either() { return z.union([z.string(), Tree]); },
      get combined() { return z.intersection(z.object({ x: z.string() }), Tree); }
    }).openapi('Tree');
    return componentsFor(lib, '3.1.0', [Tree], undefined);
  };

  cases['DIFF-026'] = () => {
    const routeConfig = {
      method: 'query',
      path: '/search',
      request: {
        body: {
          content: {
            'application/json': {
              itemSchema: z.string().openapi('Item'),
              itemEncoding: { contentType: 'text/plain' },
              prefixEncoding: [{ contentType: 'text/plain' }]
            }
          }
        }
      },
      responses: {
        200: {
          summary: 'Search results',
          description: 'OK',
          content: { 'application/json': { schema: z.array(z.string()) } }
        }
      }
    };
    const document = plain(new lib.OpenApiGeneratorV32([{ type: 'route', route: routeConfig }]).generateDocument(docConfig('3.2.0')));
    return document;
  };

  cases['DIFF-027'] = () => {
    const unknown = errorDescriptor(() => componentsFor(lib, '3.0.0', [z.symbol().openapi('Unsupported')], undefined));
    const missing = errorDescriptor(() => {
      const registry = new lib.OpenAPIRegistry();
      registry.registerPath(route({ request: { headers: [z.string()] }, responses: { 200: { description: 'ok' } } }));
      return new lib.OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig('3.0.0'));
    });
    const conflict = errorDescriptor(() => {
      const registry = new lib.OpenAPIRegistry();
      registry.registerPath(route({
        request: { query: z.object({ test: z.string().openapi({ param: { in: 'header' } }) }) },
        responses: { 200: { description: 'ok' } }
      }));
      return new lib.OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig('3.0.0'));
    });
    return { unknown, missing, conflict };
  };

  cases['DIFF-028'] = () => {
    const registry = new lib.OpenAPIRegistry();
    const Pet = registry.register('Pet', z.object({ name: z.string(), age: z.number().int().optional() }));
    registry.registerPath(route({
      path: '/pets',
      method: 'get',
      responses: { 200: { description: 'ok', content: { 'application/json': { schema: z.array(Pet) } } } }
    }));
    const document = new lib.OpenApiGeneratorV31(registry.definitions).generateDocument(docConfig('3.1.0'));
    return {
      serialized: JSON.stringify(document),
      topLevelKeys: Object.keys(document),
      componentKeys: Object.keys(document.components),
      schemaKeys: Object.keys(document.components.schemas)
    };
  };

  cases['DIFF-029'] = () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf8'));
    const indexFile = path.join(repoRoot, 'src', 'index.ts');
    return {
      main: pkg.main,
      module: pkg.module,
      types: pkg.types,
      hasIndex: fs.existsSync(indexFile),
      exports: Object.keys(lib).sort()
    };
  };

  cases['DIFF-030'] = () => {
    const build = (version, Ctor) => {
      const registry = new lib.OpenAPIRegistry();
      registry.register('S', z.string());
      registry.registerWebhook(route({ path: '/hook', method: 'post' }));
      const document = plain(new Ctor(registry.definitions).generateDocument(docConfig(version)));
      return {
        openapi: document.openapi,
        hasWebhooks: Object.prototype.hasOwnProperty.call(document, 'webhooks'),
        webhookPaths: Object.keys(document.webhooks ?? {})
      };
    };
    return {
      v3: build('3.0.0', lib.OpenApiGeneratorV3),
      v31: build('3.1.0', lib.OpenApiGeneratorV31),
      v32: build('3.2.0', lib.OpenApiGeneratorV32)
    };
  };

  cases['DIFF-031'] = () => {
    const A = z.object({ kind: z.literal('a') }).openapi('A');
    const B = z.object({ kind: z.literal('b') }).openapi('B');
    const union = z.discriminatedUnion('kind', [A, B]);
    union._zod.def.discriminator = undefined;
    const captured = captureConsoleError(() => componentsFor(lib, '3.1.0', [A, B, union.openapi('NoDisc')], undefined));
    const missing = errorDescriptor(() => {
      const registry = new lib.OpenAPIRegistry();
      registry.registerPath(route({ request: { headers: [z.string()] }, responses: { 200: { description: 'ok' } } }));
      return new lib.OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig('3.0.0'));
    });
    const conflict = errorDescriptor(() => {
      const registry = new lib.OpenAPIRegistry();
      registry.registerPath(route({
        request: { query: z.object({ test: z.string().openapi({ param: { name: 'other' } }) }) },
        responses: { 200: { description: 'ok' } }
      }));
      return new lib.OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig('3.0.0'));
    });
    const unknown = errorDescriptor(() => componentsFor(lib, '3.0.0', [z.symbol().openapi('Nope')], undefined));
    return { consoleError: captured.calls, missing, conflict, unknown };
  };

  cases['DIFF-032'] = () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf8'));
    const srcDir = path.join(repoRoot, 'src');
    const walk = dir => fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
      const full = path.join(dir, entry.name);
      return entry.isDirectory() ? walk(full) : [path.relative(repoRoot, full)];
    });
    const sourceFiles = walk(srcDir).sort();
    const extensions = [...new Set(sourceFiles.map(file => path.extname(file)))].sort();
    const suspicious = sourceFiles.filter(file => /\.(jsx|tsx|vue|svelte|css|scss|html)$/.test(file));
    const dependencyNames = Object.keys({ ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) }).sort();
    const uiDependencies = dependencyNames.filter(name => /^(react|react-dom|vue|svelte|@angular|next|nuxt|vite|webpack|tailwindcss)/.test(name));
    return {
      sourceExtensions: extensions,
      suspiciousFiles: suspicious,
      dependencyNames,
      uiDependencies,
      hasEntryHtml: fs.existsSync(path.join(repoRoot, 'index.html')),
      main: pkg.main
    };
  };

  return cases;
}

export function runCases(ctx) {
  const cases = createCases(ctx);
  const results = {};
  for (const id of OBSERVATION_IDS) {
    try {
      results[id] = { pass: true, normalized: stable(cases[id]()) };
    } catch (error) {
      results[id] = {
        pass: false,
        error: { name: error?.constructor?.name ?? typeof error, message: String(error?.message ?? error) }
      };
    }
  }
  return results;
}

export function runReport(ctx) {
  const results = runCases(ctx);
  return {
    schema_version: 'migration-differential-probe:v1',
    batch_id: BATCH_ID,
    kind: 'DIFFERENTIAL',
    passed: OBSERVATION_IDS.every(id => results[id].pass),
    results
  };
}
