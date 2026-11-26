import assert from 'node:assert/strict';
import path from 'node:path';

const BATCH_ID = 'BATCH-P2P-0001';

const versionConfig = openapi => ({
  openapi,
  info: { version: '1.0.0', title: 'P2P preservation' },
});

const generatorFor = (lib, version) =>
  version === '3.2.0'
    ? lib.OpenApiGeneratorV32
    : version === '3.1.0'
      ? lib.OpenApiGeneratorV31
      : lib.OpenApiGeneratorV3;

const componentsFor = (lib, z, schemas, version = '3.0.0', options) => {
  const definitions = schemas.map(schema => ({ type: 'schema', schema }));
  const Generator = generatorFor(lib, version);
  return new Generator(definitions, options).generateComponents();
};

const documentFor = (lib, definitions, version = '3.0.0', options) => {
  const Generator = generatorFor(lib, version);
  return new Generator(definitions, options).generateDocument(
    versionConfig(version)
  );
};

const route = (overrides = {}) => ({
  method: 'get',
  path: '/',
  responses: { 200: { description: 'OK' } },
  ...overrides,
});

const captureError = action => {
  try {
    action();
  } catch (error) {
    return {
      constructor: error.constructor?.name ?? null,
      message: error.message ?? null,
      isError: error instanceof Error,
    };
  }
  return null;
};

const walkFiles = (fs, dir, prefix = '') => {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  return entries.flatMap(entry => {
    const relative = path.posix.join(prefix, entry.name);
    return entry.isDirectory()
      ? walkFiles(fs, path.join(dir, entry.name), relative)
      : [relative];
  });
};

export async function runCases({ lib, z, repoRoot, fs, crypto }) {
  lib.extendZodWithOpenApi(z);
  const results = {};
  const hash = file =>
    crypto
      .createHash('sha256')
      .update(fs.readFileSync(path.join(repoRoot, file)))
      .digest('hex');

  const run = (id, action) => {
    try {
      const value = action();
      results[id] = { status: 'PASS', value };
    } catch (error) {
      results[id] = {
        status: 'FAIL',
        value: { message: error?.message ?? String(error) },
      };
    }
  };

  run('P2P-001', () => {
    const Pet = z
      .object({ id: z.string(), name: z.string().optional() })
      .openapi('Pet');
    const Query = z.string().openapi({ param: { name: 'filter', in: 'query' } });
    const registry = new lib.OpenAPIRegistry();
    registry.register('Pet', Pet);
    registry.registerParameter('Filter', Query);
    registry.registerComponent('schemas', 'Raw', { type: 'string' });
    registry.registerPath(
      route({
        method: 'get',
        path: '/pets',
        request: { query: z.object({ filter: Query }) },
        responses: {
          200: {
            description: 'OK',
            content: { 'application/json': { schema: Pet } },
          },
        },
      })
    );
    registry.registerWebhook(
      route({
        method: 'post',
        path: 'petCreated',
        responses: { 200: { description: 'OK' } },
      })
    );

    const docV3 = documentFor(lib, registry.definitions, '3.0.0');
    const docV31 = documentFor(lib, registry.definitions, '3.1.0');
    const docV32 = documentFor(lib, registry.definitions, '3.2.0');
    assert.deepEqual(Object.keys(docV3).sort(), ['components', 'info', 'openapi', 'paths']);
    assert.equal('webhooks' in docV3, false);
    assert.equal('webhooks' in docV31, true);
    assert.equal('webhooks' in docV32, true);
    assert.deepEqual(docV3.components.schemas.Pet, {
      type: 'object',
      properties: { id: { type: 'string' }, name: { type: 'string' } },
      required: ['id'],
    });
    return {
      v3: JSON.stringify(docV3),
      v31: JSON.stringify(docV31),
      v32: JSON.stringify(docV32),
    };
  });

  run('P2P-002', () => {
    const metadataOnly = z.string().openapi({ description: 'd' });
    const withRef = z.string().openapi('Ref', { description: 'd' });
    assert.equal(typeof metadataOnly.openapi, 'function');
    assert.equal(typeof withRef.openapi, 'function');
    assert.deepEqual(lib.getOpenApiMetadata(metadataOnly), { description: 'd' });
    assert.equal(lib.getRefId(metadataOnly), undefined);
    assert.deepEqual(lib.getOpenApiMetadata(withRef), { description: 'd' });
    assert.equal(lib.getRefId(withRef), 'Ref');
    return {
      augmented: typeof metadataOnly.openapi === 'function',
      metadata: lib.getOpenApiMetadata(metadataOnly),
      refId: lib.getRefId(withRef),
    };
  });

  run('P2P-003', () => {
    const Base = z
      .object({ name: z.string(), type: z.enum(['dog', 'cat']).optional() })
      .openapi('Base');
    const Picked = Base.pick({ name: true });
    const Other = z.object({ pick: Picked }).openapi('Other');
    const { components } = componentsFor(lib, z, [Base, Other]);
    assert.deepEqual(components.schemas.Other, {
      properties: {
        pick: {
          properties: { name: { type: 'string' } },
          required: ['name'],
          type: 'object',
        },
      },
      required: ['pick'],
      type: 'object',
    });
    return components.schemas;
  });

  run('P2P-004', () => {
    const defaultSchema = z.string().default('x').openapi('WithDefault');
    const prefaultSchema = z
      .string()
      .prefault('prefaulted')
      .openapi('WithPrefault');
    const refined = z
      .number()
      .openapi({ example: 42 })
      .refine(value => value % 2 === 0)
      .openapi('Refined');
    const transformed = z
      .number()
      .openapi({ example: 42 })
      .transform(value => String(value))
      .openapi('Transformed');
    const piped = z
      .number()
      .or(z.string())
      .pipe(z.transform(value => Number(value)))
      .openapi('Piped');
    const lazy = z.object({ key: z.lazy(() => z.string()) }).openapi('Lazy');
    const { components } = componentsFor(lib, z, [
      defaultSchema,
      prefaultSchema,
      refined,
      transformed,
      piped,
      lazy,
    ]);
    assert.deepEqual(components.schemas.WithDefault, {
      type: 'string',
      default: 'x',
    });
    assert.deepEqual(components.schemas.WithPrefault, {
      type: 'string',
      default: 'prefaulted',
    });
    assert.deepEqual(components.schemas.Refined, {
      type: 'number',
      example: 42,
    });
    assert.deepEqual(components.schemas.Transformed, {
      type: 'number',
      example: 42,
    });
    assert.deepEqual(components.schemas.Lazy, {
      type: 'object',
      properties: { key: { type: 'string' } },
      required: ['key'],
    });
    return components.schemas;
  });

  run('P2P-005', () => {
    const exampleIndex = hash('example/index.ts');
    const exampleDocs = hash('example/openapi-docs.yml');
    const indexText = fs.readFileSync(
      path.join(repoRoot, 'src/index.ts'),
      'utf8'
    );
    assert.match(indexText, /export .*OpenApiGeneratorV32/);
    return { exampleIndex, exampleDocs };
  });

  run('P2P-006', () => {
    const Schema = z.string().openapi('Item');
    const Generator = lib.OpenApiGeneratorV32;
    const generator = new Generator([{ type: 'schema', schema: Schema }]);
    assert.equal(typeof generator.generateDocument, 'function');
    assert.equal(typeof generator.generateComponents, 'function');
    const document = generator.generateDocument(versionConfig('3.2.0'));
    const components = new Generator([
      { type: 'schema', schema: Schema },
    ]).generateComponents();
    assert.equal(document.openapi, '3.2.0');
    assert.deepEqual(components.components.schemas.Item, { type: 'string' });
    return {
      documentKeys: Object.keys(document).sort(),
      components: components.components,
    };
  });

  run('P2P-007', () => {
    const expectedExports = [
      'OpenAPIRegistry',
      'OpenApiGeneratorV3',
      'OpenApiGeneratorV31',
      'OpenApiGeneratorV32',
      'extendZodWithOpenApi',
      'getOpenApiMetadata',
      'getRefId',
      'zodToOpenAPIRegistry',
    ];
    assert.deepEqual(Object.keys(lib).sort(), expectedExports);
    const registryMethods = [
      'register',
      'registerParameter',
      'registerPath',
      'registerWebhook',
      'registerComponent',
    ];
    for (const method of registryMethods) {
      assert.equal(typeof lib.OpenAPIRegistry.prototype[method], 'function');
    }
    const definitionsDescriptor = Object.getOwnPropertyDescriptor(
      lib.OpenAPIRegistry.prototype,
      'definitions'
    );
    assert.equal(typeof definitionsDescriptor.get, 'function');
    for (const name of [
      'OpenApiGeneratorV3',
      'OpenApiGeneratorV31',
      'OpenApiGeneratorV32',
    ]) {
      assert.equal(typeof lib[name].prototype.generateDocument, 'function');
      assert.equal(typeof lib[name].prototype.generateComponents, 'function');
    }
    return {
      exports: Object.keys(lib).sort(),
      registryMethods,
      generators: ['OpenApiGeneratorV3', 'OpenApiGeneratorV31', 'OpenApiGeneratorV32'],
    };
  });

  run('P2P-008', () => {
    const error = captureError(() =>
      documentFor(lib, [
        {
          type: 'route',
          route: route({
            path: '/parameters',
            request: {
              query: z.object({
                test: z.string().openapi({ param: { name: 'another' } }),
              }),
            },
          }),
        },
      ])
    );
    assert.equal(error.constructor, 'ConflictError');
    assert.match(error.message, /^Conflicting name/);
    return error;
  });

  run('P2P-009', () => {
    const StringSchema = z.string().openapi('String');
    const Test = z.object({ key: StringSchema.nullable() }).openapi('Test');
    const v3 = componentsFor(lib, z, [StringSchema, Test], '3.0.0');
    const v31 = componentsFor(lib, z, [StringSchema, Test], '3.1.0');
    assert.deepEqual(v3.components.schemas.Test.properties.key, {
      allOf: [{ $ref: '#/components/schemas/String' }, { nullable: true }],
    });
    assert.deepEqual(v31.components.schemas.Test.properties.key, {
      allOf: [
        { $ref: '#/components/schemas/String' },
        { type: ['string', 'null'] },
      ],
    });
    return { v3: v3.components.schemas, v31: v31.components.schemas };
  });

  run('P2P-010', () => {
    const nullableEnum = z
      .enum(['option1', 'option2'])
      .nullable()
      .openapi('NullableEnum');
    const numericEnum = z
      .nativeEnum({ OPTION: 1, ANOTHER: 42 })
      .openapi('NumericEnum');
    const stringEnum = z
      .nativeEnum({ OPTION: 'Option', ANOTHER: 'Another' })
      .openapi('StringEnum');
    const { components } = componentsFor(lib, z, [
      nullableEnum,
      numericEnum,
      stringEnum,
    ]);
    const mixed = captureError(() =>
      componentsFor(lib, z, [
        z.nativeEnum({ OPTION: 1, ANOTHER: '42' }).openapi('MixedEnum'),
      ])
    );
    assert.deepEqual(components.schemas.NullableEnum, {
      type: 'string',
      nullable: true,
      enum: ['option1', 'option2', null],
    });
    assert.deepEqual(components.schemas.NumericEnum, {
      type: 'integer',
      enum: [1, 42],
    });
    assert.deepEqual(components.schemas.StringEnum, {
      type: 'string',
      enum: ['Option', 'Another'],
    });
    assert.match(mixed.message, /^Enum has mixed string and number values/);
    return { schemas: components.schemas, mixedError: mixed };
  });

  run('P2P-011', () => {
    const packageJson = JSON.parse(
      fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf8')
    );
    const files = walkFiles(fs, path.join(repoRoot, 'src'));
    const extensions = [...new Set(files.map(file => path.posix.extname(file)))].sort();
    const dependencyNames = Object.keys({
      ...(packageJson.dependencies ?? {}),
      ...(packageJson.devDependencies ?? {}),
    }).sort();
    const forbidden = dependencyNames.filter(name =>
      /^(react|react-dom|vue|svelte|@angular\/core|next|vite)$/i.test(name)
    );
    assert.deepEqual(forbidden, []);
    assert.equal(packageJson.main, 'dist/index.cjs');
    assert.equal(packageJson.module, 'dist/index.mjs');
    assert.equal(packageJson.types, 'dist/index.d.ts');
    assert.ok(files.includes('index.ts'));
    assert.deepEqual(extensions, ['.ts']);
    return {
      main: packageJson.main,
      module: packageJson.module,
      types: packageJson.types,
      sourceExtensions: extensions,
      forbiddenDependencies: forbidden,
    };
  });

  run('P2P-012', () => {
    const registry = new lib.OpenAPIRegistry();
    const schema = registry.registerParameter(
      'referenceName',
      z.string().openapi({ param: { name: 'actualName' } })
    );
    assert.equal(lib.getRefId(schema), 'referenceName');
    assert.deepEqual(lib.getOpenApiMetadata(schema), {
      param: { name: 'actualName' },
    });
    return {
      refId: lib.getRefId(schema),
      metadata: lib.getOpenApiMetadata(schema),
    };
  });

  run('P2P-013', () => {
    const unknown = captureError(() =>
      componentsFor(lib, z, [z.symbol().openapi('Unsupported')])
    );
    const missingName = captureError(() =>
      documentFor(lib, [{ type: 'parameter', schema: z.string() }])
    );
    const missingLocation = captureError(() =>
      documentFor(lib, [
        {
          type: 'parameter',
          schema: z.string().openapi({ param: { name: 'test' } }),
        },
      ])
    );
    const conflict = captureError(() =>
      documentFor(lib, [
        {
          type: 'route',
          route: route({
            request: {
              query: z.object({
                test: z.string().openapi({ param: { name: 'other' } }),
              }),
            },
          }),
        },
      ])
    );
    assert.equal(unknown.constructor, 'UnknownZodTypeError');
    assert.equal(missingName.constructor, 'MissingParameterDataError');
    assert.equal(missingLocation.constructor, 'MissingParameterDataError');
    assert.equal(conflict.constructor, 'ConflictError');
    return { unknown, missingName, missingLocation, conflict };
  });

  run('P2P-014', () => {
    const registry = new lib.OpenAPIRegistry();
    registry.register('Zeta', z.string());
    registry.register('Alpha', z.string());
    registry.registerParameter('Beta', z.number().openapi({ param: { in: 'query' } }));
    const definitions = registry.definitions;
    const plain = new lib.OpenApiGeneratorV3(definitions).generateComponents();
    const sorted = new lib.OpenApiGeneratorV3(definitions, {
      sortComponents: 'alphabetically',
    }).generateComponents();
    assert.deepEqual(Object.keys(plain.components.schemas), ['Zeta', 'Alpha', 'Beta']);
    assert.deepEqual(Object.keys(plain.components.parameters), ['Beta']);
    assert.deepEqual(Object.keys(sorted.components.schemas), ['Alpha', 'Beta', 'Zeta']);
    assert.deepEqual(Object.keys(sorted.components.parameters), ['Beta']);
    return {
      registrationOrder: {
        schemas: Object.keys(plain.components.schemas),
        parameters: Object.keys(plain.components.parameters),
      },
      alphabetical: {
        schemas: Object.keys(sorted.components.schemas),
        parameters: Object.keys(sorted.components.parameters),
      },
    };
  });

  run('P2P-015', () => {
    const expected = [
      'OpenAPIRegistry',
      'OpenApiGeneratorV3',
      'OpenApiGeneratorV31',
      'OpenApiGeneratorV32',
      'extendZodWithOpenApi',
      'getOpenApiMetadata',
      'getRefId',
      'zodToOpenAPIRegistry',
    ];
    assert.deepEqual(Object.keys(lib).sort(), expected);
    return { exports: Object.keys(lib).sort() };
  });

  run('P2P-016', () => {
    const schema = z.tuple([z.string(), z.number()]).openapi('Tuple');
    const v31 = componentsFor(lib, z, [schema], '3.1.0');
    const v32 = componentsFor(lib, z, [schema], '3.2.0');
    const document = documentFor(
      lib,
      [{ type: 'schema', schema }],
      '3.2.0'
    );
    assert.equal(document.openapi, '3.2.0');
    assert.deepEqual(v31.components.schemas.Tuple.prefixItems, [
      { type: 'string' },
      { type: 'number' },
    ]);
    assert.deepEqual(v32.components.schemas.Tuple.prefixItems, [
      { type: 'string' },
      { type: 'number' },
    ]);
    return {
      openapi: document.openapi,
      v31: v31.components.schemas.Tuple,
      v32: v32.components.schemas.Tuple,
    };
  });

  run('P2P-017', () => {
    const Pet = z.object({ id: z.string(), tag: z.string().optional() }).openapi('Pet');
    const document = documentFor(
      lib,
      [
        { type: 'schema', schema: Pet },
        {
          type: 'route',
          route: route({
            path: '/pets/{id}',
            request: { params: z.object({ id: z.string() }) },
            responses: {
              200: {
                description: 'OK',
                content: { 'application/json': { schema: Pet } },
              },
            },
          }),
        },
      ],
      '3.0.0'
    );
    const serialized = JSON.stringify(document);
    assert.equal(
      serialized,
      JSON.stringify(JSON.parse(serialized))
    );
    return { serialized };
  });

  run('P2P-018', () => {
    const registry = new lib.OpenAPIRegistry();
    registry.registerPath(
      route({
        method: 'query',
        path: '/search',
        responses: {
          200: {
            summary: 'A short summary',
            content: {
              'application/jsonl': {
                itemSchema: z.object({ value: z.number() }),
                prefixEncoding: [{ contentType: 'text/plain' }],
                itemEncoding: { contentType: 'image/png' },
              },
            },
          },
        },
      })
    );
    const document = new lib.OpenApiGeneratorV32(
      registry.definitions
    ).generateDocument(versionConfig('3.2.0'));
    const operation = document.paths['/search'].query;
    const content = operation.responses['200'].content['application/jsonl'];
    assert.deepEqual(content, {
      itemSchema: {
        type: 'object',
        properties: { value: { type: 'number' } },
        required: ['value'],
      },
      prefixEncoding: [{ contentType: 'text/plain' }],
      itemEncoding: { contentType: 'image/png' },
    });
    assert.equal(operation.responses['200'].summary, 'A short summary');
    return {
      summary: operation.responses['200'].summary,
      content,
      method: 'query',
    };
  });

  run('P2P-019', () => {
    const registry = new lib.OpenAPIRegistry();
    registry.register('S', z.string());
    registry.registerWebhook(
      route({
        method: 'post',
        path: 'happened',
        responses: { 200: { description: 'OK' } },
      })
    );
    const v3 = documentFor(lib, registry.definitions, '3.0.0');
    const v31 = documentFor(lib, registry.definitions, '3.1.0');
    const v32 = documentFor(lib, registry.definitions, '3.2.0');
    assert.equal(v3.openapi, '3.0.0');
    assert.equal(v31.openapi, '3.1.0');
    assert.equal(v32.openapi, '3.2.0');
    assert.equal('webhooks' in v3, false);
    assert.deepEqual(Object.keys(v31.webhooks), ['happened']);
    assert.deepEqual(Object.keys(v32.webhooks), ['happened']);
    return {
      v3: { openapi: v3.openapi, hasWebhooks: 'webhooks' in v3 },
      v31: { openapi: v31.openapi, webhooks: Object.keys(v31.webhooks) },
      v32: { openapi: v32.openapi, webhooks: Object.keys(v32.webhooks) },
    };
  });

  run('P2P-020', () => {
    const mixed = captureError(() =>
      componentsFor(lib, z, [
        z.nativeEnum({ A: 1, B: '2' }).openapi('Mixed'),
      ])
    );
    const conflict = captureError(() =>
      documentFor(lib, [
        {
          type: 'route',
          route: route({
            request: {
              query: z.object({
                test: z.string().openapi({ param: { name: 'other' } }),
              }),
            },
          }),
        },
      ])
    );
    const missing = captureError(() =>
      documentFor(lib, [
        {
          type: 'parameter',
          schema: z.string().openapi({ param: { name: 'test' } }),
        },
      ])
    );
    const unknown = captureError(() =>
      componentsFor(lib, z, [z.symbol().openapi('Unsupported')])
    );
    const firstMixed = captureError(() =>
      componentsFor(lib, z, [
        z.nativeEnum({ A: 1, B: '2' }).openapi('MixedAgain'),
      ])
    );
    assert.equal(mixed.constructor, firstMixed.constructor);
    assert.equal(conflict.constructor, 'ConflictError');
    assert.equal(missing.constructor, 'MissingParameterDataError');
    assert.equal(unknown.constructor, 'UnknownZodTypeError');
    return { mixed, conflict, missing, unknown };
  });

  run('P2P-021', () => {
    const schemas = [
      z.number().int().min(0).openapi('Integer'),
      z.string().email().openapi('Email'),
      z.object({}).catchall(z.string()).openapi('Catchall'),
      z.record(z.string(), z.number()).openapi('Record'),
      z.intersection(z.object({ a: z.string() }), z.object({ b: z.number() })).openapi('Intersection'),
      z.literal('fixed').openapi('Literal'),
      z.templateLiteral(['id-', z.string(), '-end']).openapi('Template'),
      z.date().openapi('Date'),
      z.bigint().openapi('BigInt'),
      z.array(z.string()).openapi('Array'),
      z.union([z.string(), z.number()]).openapi('Union'),
    ];
    const { components } = componentsFor(lib, z, schemas);
    assert.deepEqual(components.schemas.Integer, {
      type: 'integer',
      minimum: 0,
    });
    assert.deepEqual(components.schemas.Email, {
      type: 'string',
      format: 'email',
    });
    assert.deepEqual(components.schemas.Catchall, {
      type: 'object',
      properties: {},
      additionalProperties: { type: 'string' },
    });
    assert.deepEqual(components.schemas.Date, {
      type: 'string',
      format: 'date-time',
    });
    assert.deepEqual(components.schemas.BigInt, {
      type: 'string',
      pattern: '^\\d+$',
    });
    return components.schemas;
  });

  run('P2P-022', () => {
    const union = z.union([z.string(), z.number()]).openapi('Union');
    const oneOf = componentsFor(lib, z, [union], '3.0.0', {
      unionPreferredType: 'oneOf',
    });
    const perSchema = z
      .union([z.string(), z.number()])
      .openapi('PerSchema', {}, { unionPreferredType: 'anyOf' });
    const anyOf = componentsFor(lib, z, [perSchema], '3.0.0', {
      unionPreferredType: 'oneOf',
    });
    assert.deepEqual(oneOf.components.schemas.Union.oneOf, [
      { type: 'string' },
      { type: 'number' },
    ]);
    assert.deepEqual(anyOf.components.schemas.PerSchema.anyOf, [
      { type: 'string' },
      { type: 'number' },
    ]);
    return {
      generatorOption: oneOf.components.schemas.Union,
      schemaOption: anyOf.components.schemas.PerSchema,
    };
  });

  run('P2P-023', () => {
    const document = documentFor(lib, [
      {
        type: 'route',
        route: route({
          path: '/described',
          request: {
            query: z.object({
              test: z.string().describe('Some parameter').optional(),
            }),
          },
        }),
      },
    ]);
    const parameters = document.paths['/described'].get.parameters;
    assert.deepEqual(parameters, [
      {
        in: 'query',
        name: 'test',
        description: 'Some parameter',
        required: false,
        schema: { description: 'Some parameter', type: 'string' },
      },
    ]);
    return { parameters };
  });

  run('P2P-024', () => {
    const definitions = [
      {
        type: 'webhook',
        webhook: route({
          method: 'post',
          path: 'last',
          responses: { 200: { description: 'OK' } },
        }),
      },
      {
        type: 'route',
        route: route({ path: '/fourth' }),
      },
      {
        type: 'component',
        componentType: 'schemas',
        name: 'Third',
        component: { type: 'string' },
      },
      {
        type: 'parameter',
        schema: z.string().openapi({ param: { name: 'second', in: 'query' } }),
      },
      { type: 'schema', schema: z.string().openapi('First') },
    ];
    const generator = new lib.OpenApiGeneratorV3(definitions);
    const order = generator.generator.definitions.map(definition => definition.type);
    assert.deepEqual(order, [
      'schema',
      'parameter',
      'component',
      'route',
      'webhook',
    ]);
    return { order };
  });

  return { batch_id: BATCH_ID, observations: results };
}
