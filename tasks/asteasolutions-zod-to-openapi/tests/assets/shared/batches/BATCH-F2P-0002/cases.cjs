'use strict';

const fs = require('fs');
const path = require('path');
const childProcess = require('child_process');

class AssertionFailure extends Error {}
class MissingCapability extends Error {}
class InfraError extends Error {}

const OBSERVATIONS = [
  'F2P-027', 'F2P-028', 'F2P-032', 'F2P-033', 'F2P-048',
  'F2P-025', 'F2P-026', 'F2P-029', 'F2P-030', 'F2P-031',
  'F2P-034', 'F2P-035', 'F2P-036', 'F2P-037', 'F2P-038',
  'F2P-039', 'F2P-040', 'F2P-041', 'F2P-042', 'F2P-043',
  'F2P-044', 'F2P-045', 'F2P-046', 'F2P-047'
];

const REQUIREMENTS = {
  'F2P-027': ['R004'],
  'F2P-028': ['R043'],
  'F2P-032': ['R002'],
  'F2P-033': ['R044'],
  'F2P-048': ['R001'],
  'F2P-025': ['R032'],
  'F2P-026': ['R032'],
  'F2P-029': ['R032'],
  'F2P-030': ['R032'],
  'F2P-031': ['R043'],
  'F2P-034': ['R032'],
  'F2P-035': ['R032'],
  'F2P-036': ['R032'],
  'F2P-037': ['R032'],
  'F2P-038': ['R032'],
  'F2P-039': ['R032'],
  'F2P-040': ['R004'],
  'F2P-041': ['R032'],
  'F2P-042': ['R032'],
  'F2P-043': ['R032'],
  'F2P-044': ['R032'],
  'F2P-045': ['R032'],
  'F2P-046': ['R032'],
  'F2P-047': ['R046']
};

const ZOD_410_INTEGRITY = 'sha512-UWxluYj2IDX9MHRXTMbB/2eeWrAMmmMSESM+MfT9MQw8U1qo9q5ASW08anoJh6AJ7pkt099fLdNFmfI+4aChHg==';

function assert(condition, message) {
  if (!condition) {
    throw new AssertionFailure(message);
  }
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function tail(text, limit) {
  const value = String(text || '');
  return value.length > (limit || 1500) ? value.slice(value.length - (limit || 1500)) : value;
}

function parseArgs(argv) {
  const parsed = { positional: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg.startsWith('--')) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        parsed[key] = true;
      } else {
        parsed[key] = next;
        i += 1;
      }
    } else {
      parsed.positional.push(arg);
    }
  }
  return parsed;
}

function localBin(ctx, name) {
  const candidates = [
    path.join(ctx.depsBin, name),
    path.join(ctx.repo, 'node_modules', '.bin', name)
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  throw new InfraError('missing executable ' + name + ' (looked in ' + candidates.join(', ') + ')');
}

function runCommand(ctx, command, args, options) {
  const result = childProcess.spawnSync(command, args, {
    cwd: ctx.repo,
    encoding: 'utf8',
    timeout: (options && options.timeout) || 180000,
    env: Object.assign({}, process.env, {
      CI: '1',
      NODE_PATH: [ctx.depsBin && path.dirname(ctx.depsBin), path.join(ctx.repo, 'node_modules')].filter(Boolean).join(path.delimiter)
    })
  });
  if (result.error) {
    throw new InfraError('failed to execute ' + command + ': ' + result.error.message);
  }
  return result;
}

function ensureBuilt(ctx) {
  const entry = path.join(ctx.distDir, 'index.cjs');
  if (fs.existsSync(entry)) {
    return;
  }
  const result = runCommand(ctx, 'npm', ['run', 'build'], { timeout: 300000 });
  if (result.status !== 0 || !fs.existsSync(entry)) {
    throw new InfraError('build failed before loading dist/index.cjs: ' + tail(result.stderr || result.stdout));
  }
}

function createContext(options) {
  const repo = path.resolve(options.repo || process.env.MIGRATION_REPO || '/workspace/repo');
  const distDir = path.resolve(options.dist || path.join(repo, 'dist'));
  const depsBin = path.resolve(options.depsBin || path.join(repo, 'node_modules', '.bin'));
  const zodRoot = path.resolve(options.zod || path.join(repo, 'node_modules', 'zod'));
  const fixtures = path.resolve(options.fixtures || path.resolve(__dirname, '../../../fixtures/batches/BATCH-F2P-0002/documents.json'));

  if (!fs.existsSync(path.join(zodRoot, 'package.json'))) {
    throw new InfraError('Zod package is not installed at ' + zodRoot);
  }

  const zodPackage = readJson(path.join(zodRoot, 'package.json'));
  const ctx = {
    repo,
    side: options.side || 'target',
    distDir,
    depsBin,
    zodRoot,
    fixtures,
    zodPackage,
    zodVersion: zodPackage.version,
    targetTechnology: zodPackage.version === '4.1.0',
    packageJson: readJson(path.join(repo, 'package.json')),
    packageLock: readJson(path.join(repo, 'package-lock.json')),
    z: require(zodRoot)
  };

  ensureBuilt(ctx);
  ctx.lib = require(path.join(distDir, 'index.cjs'));
  if (typeof ctx.lib.extendZodWithOpenApi !== 'function') {
    throw new InfraError('dist/index.cjs does not export extendZodWithOpenApi');
  }
  ctx.lib.extendZodWithOpenApi(ctx.z);

  try {
    const semverRoot = path.join(repo, 'node_modules', 'semver');
    ctx.semver = require(semverRoot);
  } catch (error) {
    ctx.semver = null;
  }

  return ctx;
}

function deepEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function runJest(ctx, file, testName) {
  const args = ['--runInBand', '--silent', '--ci', '--runTestsByPath', file];
  if (testName) {
    args.push('-t', testName);
  }
  const result = runCommand(ctx, localBin(ctx, 'jest'), args, { timeout: 240000 });
  if (result.status !== 0) {
    throw new AssertionFailure('jest entry failed for ' + file + (testName ? ' :: ' + testName : '') + ': ' + tail(result.stderr || result.stdout));
  }
}

function runTsd(ctx) {
  // Every research observation in this batch invokes the same complete tsd
  // suite against the same immutable candidate tree.  Run it once per batch:
  // repeating the identical process needlessly multiplies runtime and can turn
  // a successful candidate type check into an infrastructure timeout.
  if (ctx.tsdPassed) {
    return;
  }
  const typings = path.relative(ctx.repo, path.join(ctx.distDir, 'index.d.ts')) || 'index.d.ts';
  const result = runCommand(ctx, localBin(ctx, 'tsd'), [
    '--files', 'spec/type-definitions/zod-extensions.test-d.ts',
    '--typings', typings
  ], { timeout: 240000 });
  if (result.status !== 0) {
    throw new AssertionFailure('tsd type tests failed: ' + tail(result.stderr || result.stdout));
  }
  ctx.tsdPassed = true;
}

function runTypecheck(ctx, source) {
  const tempFile = path.join(ctx.repo, '.batch-f2p-0002-probe.ts');
  const distSpecifier = './' + path.relative(ctx.repo, ctx.distDir).split(path.sep).join('/') + '/index';
  const body = source.replace('__DIST_SPECIFIER__', distSpecifier);
  try {
    fs.writeFileSync(tempFile, body);
    const result = runCommand(ctx, localBin(ctx, 'tsc'), [
      '--noEmit', '--strict', '--skipLibCheck', '--target', 'ES2020',
      '--module', 'commonjs', '--moduleResolution', 'node', '--esModuleInterop',
      path.basename(tempFile)
    ], { timeout: 240000 });
    if (result.status !== 0) {
      throw new AssertionFailure('TypeScript type surface check failed: ' + tail(result.stdout || result.stderr));
    }
  } finally {
    try {
      fs.unlinkSync(tempFile);
    } catch (error) {
      void error;
    }
  }
}

function safeExtendTypeSurface(ctx) {
  runTypecheck(ctx, [
    "import { z } from 'zod';",
    "import { extendZodWithOpenApi } from '__DIST_SPECIFIER__';",
    'extendZodWithOpenApi(z);',
    'type Equal<X, Y> = (<T>() => T extends X ? 1 : 2) extends <T>() => T extends Y ? 1 : 2 ? true : false;',
    'const schema = z.object({ a: z.string() }).safeExtend({ b: z.number() });',
    'type Inferred = z.infer<typeof schema>;',
    'const typeCheck: Equal<Inferred, { a: string; b: number }> = true;',
    "const withOpenapi = schema.openapi('S');",
    'void typeCheck;',
    'void withOpenapi;',
    ''
  ].join('\n'));
}

function safeExtendRuntime(ctx) {
  const z = ctx.z;
  let schema;
  try {
    schema = z.object({ a: z.string() }).safeExtend({ b: z.number() });
  } catch (error) {
    throw new MissingCapability('Zod 4.1.0 safeExtend capability is absent: ' + error.message);
  }
  assert(typeof schema.safeExtend === 'function', 'safeExtend is not callable on the extended object schema');
  const parsed = schema.parse({ a: 'x', b: 1 });
  assert(deepEqual(parsed, { a: 'x', b: 1 }), 'safeExtend schema parsed the wrong value: ' + JSON.stringify(parsed));
  const augmented = schema.openapi('S');
  assert(typeof augmented.openapi === 'function', 'safeExtend result lost the openapi augmentation');
}

function metadataRegistryScenario(ctx) {
  const z = ctx.z;
  const schema = z.string().meta({ id: 'X' });
  const refId = ctx.lib.getRefId(schema);
  assert(refId === 'X', 'Metadata.getRefId(.meta({ id: "X" })) returned ' + JSON.stringify(refId) + ' instead of "X"');
  const components = new ctx.lib.OpenApiGeneratorV3([schema]).generateComponents();
  assert(components && components.components && components.components.schemas && components.components.schemas.X, 'metadata refId X was not registered as a component');
  return refId;
}

function dependencyPolicy(ctx) {
  const pkg = ctx.packageJson;
  const lock = ctx.packageLock;
  const devPin = pkg.devDependencies && pkg.devDependencies.zod;
  assert(devPin === '^4.1.0', 'package.json devDependencies.zod is ' + JSON.stringify(devPin) + ' instead of "^4.1.0"');

  const locked = lock.packages && lock.packages['node_modules/zod'];
  assert(locked, 'package-lock.json lacks packages["node_modules/zod"]');
  assert(locked.version === '4.1.0', 'package-lock.json resolves node_modules/zod to ' + JSON.stringify(locked.version) + ' instead of "4.1.0"');
  assert(locked.integrity === ZOD_410_INTEGRITY, 'package-lock.json zod integrity is not the Zod 4.1.0 integrity');
  assert(String(locked.resolved || '').includes('zod-4.1.0.tgz'), 'package-lock.json zod tarball is not zod-4.1.0.tgz');

  const peer = pkg.peerDependencies && pkg.peerDependencies.zod;
  assert(typeof peer === 'string' && peer.length > 0, 'package.json lacks peerDependencies.zod');
  if (ctx.semver) {
    assert(ctx.semver.satisfies('4.1.0', peer), 'peerDependencies.zod ' + JSON.stringify(peer) + ' does not include 4.1.0');
  } else {
    assert(peer === '^4.0.0' || peer.includes('4.1.0'), 'peerDependencies.zod does not include 4.1.0');
  }
}

function fixtureDocument(ctx) {
  const z = ctx.z;
  const lib = ctx.lib;
  const registry = new lib.OpenAPIRegistry();
  registry.register('Zeta', z.object({ alpha: z.string(), beta: z.number() }).openapi('Zeta', { description: 'zeta' }));
  registry.register('Alpha', z.string());
  registry.registerPath({
    method: 'get',
    path: '/items/{id}',
    request: { params: z.object({ id: z.string().openapi({ param: { name: 'id', in: 'path' } }) }) },
    responses: {
      200: {
        description: 'ok',
        content: { 'application/json': { schema: z.array(z.object({ id: z.string(), ref: z.string() })).openapi('ItemList') } }
      }
    }
  });
  registry.registerWebhook({
    method: 'post',
    path: 'eventCreated',
    responses: {
      200: {
        description: 'created',
        content: { 'application/json': { schema: z.object({ event: z.string() }).openapi('Event') } }
      }
    }
  });
  return new lib.OpenApiGeneratorV31(registry.definitions).generateDocument({
    openapi: '3.1.0',
    info: { title: 'Batch F2P fixture', version: '1.0.0' }
  });
}

function fixtureSerialization(ctx) {
  const fixture = readJson(ctx.fixtures);
  const document = fixtureDocument(ctx);
  const serialized = JSON.stringify(document);
  assert(serialized === fixture.expected_json_string, 'generated document JSON serialization differs from the fixed baseline fixture');
  assert(deepEqual(Object.keys(document), fixture.expected_top_level_order), 'generated document top-level property order changed: ' + JSON.stringify(Object.keys(document)));
  assert(deepEqual(Object.keys(document.components.schemas), fixture.expected_schema_order), 'generated components.schemas order changed: ' + JSON.stringify(Object.keys(document.components.schemas)));
  const refs = serialized.match(/#\/components\/schemas\/[A-Za-z0-9_]+/g) || [];
  assert(refs.length >= 2 && refs.includes('#/components/schemas/ItemList') && refs.includes('#/components/schemas/Event'), 'expected $ref strings are missing from the serialized document');
}

function orderingScenario(ctx) {
  const z = ctx.z;
  const lib = ctx.lib;
  const registry = new lib.OpenAPIRegistry();
  registry.register('Zed', z.string());
  registry.register('Alpha', z.string());
  registry.registerParameter('ZParam', z.string().openapi({ param: { name: 'z', in: 'query' } }));
  registry.registerParameter('AParam', z.string().openapi({ param: { name: 'a', in: 'query' } }));

  const sorted = new lib.OpenApiGeneratorV3(registry.definitions, { sortComponents: 'alphabetically' }).generateComponents();
  const registration = new lib.OpenApiGeneratorV3(registry.definitions).generateComponents();

  assert(deepEqual(Object.keys(sorted.components.schemas), ['Alpha', 'AParam', 'Zed', 'ZParam']), 'sortComponents did not sort components.schemas alphabetically: ' + JSON.stringify(Object.keys(sorted.components.schemas)));
  assert(deepEqual(Object.keys(sorted.components.parameters), ['AParam', 'ZParam']), 'sortComponents did not sort components.parameters alphabetically: ' + JSON.stringify(Object.keys(sorted.components.parameters)));
  assert(deepEqual(Object.keys(registration.components.schemas), ['Zed', 'Alpha', 'ZParam', 'AParam']), 'components.schemas did not preserve registration order without sortComponents: ' + JSON.stringify(Object.keys(registration.components.schemas)));
  assert(deepEqual(Object.keys(registration.components.parameters), ['ZParam', 'AParam']), 'components.parameters did not preserve registration order without sortComponents: ' + JSON.stringify(Object.keys(registration.components.parameters)));
}

function metaIdComponentScenario(ctx) {
  const z = ctx.z;
  const schema = z.string().meta({ id: 'MetaOnly' });
  const components = new ctx.lib.OpenApiGeneratorV3([schema]).generateComponents();
  assert(components && components.components && components.components.schemas, 'generateComponents returned no schemas');
  assert(deepEqual(components.components.schemas.MetaOnly, { type: 'string' }), 'components.schemas.MetaOnly missing or wrong: ' + JSON.stringify(components.components.schemas));
}

function metadataPrecedenceScenario(ctx) {
  const z = ctx.z;
  const lib = ctx.lib;
  const merged = z.string().meta({ description: 'meta', deprecated: true }).openapi('S', { description: 'openapi' });
  const mergedComponents = new lib.OpenApiGeneratorV3([merged]).generateComponents();
  assert(deepEqual(mergedComponents.components.schemas.S, { type: 'string', description: 'openapi', deprecated: true }), 'metadata precedence output changed: ' + JSON.stringify(mergedComponents.components.schemas.S));

  const named = z.string().meta({ id: 'Meta' }).openapi('Open');
  const namedComponents = new lib.OpenApiGeneratorV3([named]).generateComponents();
  assert(namedComponents.components.schemas.Open, 'openapi refId did not take precedence over .meta() id');
  assert(!namedComponents.components.schemas.Meta, 'component was registered under the .meta() id instead of the .openapi() refId');
}

function recursiveLazyScenario(ctx) {
  const z = ctx.z;
  const NodeSchema = z.lazy(() => z.object({
    value: z.string(),
    children: z.array(NodeSchema).optional(),
    parent: NodeSchema.nullable().optional()
  }).openapi('Node'));
  const components = new ctx.lib.OpenApiGeneratorV31([NodeSchema]).generateComponents();
  const serialized = JSON.stringify(components);
  assert(serialized.includes('#/components/schemas/Node'), 'recursive lazy schema did not emit a self $ref');
  assert(components.components.schemas.Node && components.components.schemas.Node.properties.children.items.$ref === '#/components/schemas/Node', 'recursive array did not reference the Node component');
}

function modifierInheritanceScenario(ctx) {
  const z = ctx.z;
  const schema = z.string().default('x').openapi('S');
  const components = new ctx.lib.OpenApiGeneratorV3([schema]).generateComponents();
  assert(deepEqual(components.components.schemas.S, { type: 'string', default: 'x' }), 'default modifier inheritance output changed: ' + JSON.stringify(components.components.schemas.S));
}

function v31PlainSchemaAndV32Webhooks(ctx) {
  const z = ctx.z;
  const lib = ctx.lib;
  const plain = new lib.OpenApiGeneratorV31([z.string().openapi('S')]).generateComponents();
  assert(deepEqual(plain.components.schemas.S, { type: 'string' }), 'V31 plain schema component missing or wrong: ' + JSON.stringify(plain.components.schemas));

  const registry = new lib.OpenAPIRegistry();
  registry.registerWebhook({ method: 'post', path: 'eventCreated', responses: { 200: { description: 'ok' } } });

  const v32 = new lib.OpenApiGeneratorV32(registry.definitions);
  assert(typeof v32.generateDocument === 'function' && typeof v32.generateComponents === 'function', 'OpenApiGeneratorV32 lost generateDocument/generateComponents');
  const v32Document = v32.generateDocument({ openapi: '3.2.0', info: { title: 'v32', version: '1.0.0' } });
  assert(v32Document.webhooks && v32Document.webhooks.eventCreated && v32Document.webhooks.eventCreated.post, 'V32 generateDocument did not populate webhooks');

  const v31Document = new lib.OpenApiGeneratorV31(registry.definitions).generateDocument({ openapi: '3.1.0', info: { title: 'v31', version: '1.0.0' } });
  assert(v31Document.webhooks && v31Document.webhooks.eventCreated, 'V31 generateDocument did not populate webhooks');

  const v3Document = new lib.OpenApiGeneratorV3(registry.definitions).generateDocument({ openapi: '3.0.0', info: { title: 'v3', version: '1.0.0' } });
  assert(!('webhooks' in v3Document), 'V3 document must not emit webhooks');
}

function dependencyAndMetadataScenario(ctx) {
  dependencyPolicy(ctx);
  metadataRegistryScenario(ctx);
}

function dependencyAndSafeExtendScenario(ctx) {
  dependencyPolicy(ctx);
  safeExtendRuntime(ctx);
}

function safeExtendScenario(ctx) {
  safeExtendRuntime(ctx);
  safeExtendTypeSurface(ctx);
}

function metadataAndSafeExtendScenario(ctx) {
  metadataPrecedenceScenario(ctx);
  safeExtendRuntime(ctx);
  safeExtendTypeSurface(ctx);
}

function researchCase(entry, extras) {
  return function run(ctx) {
    if (entry && entry.file && entry.file !== 'spec/type-definitions/zod-extensions.test-d.ts') {
      if (entry.file === 'spec/setup-tests.ts') {
        runJest(ctx, 'spec/types/recursive-schemas.spec.ts', null);
      } else {
        runJest(ctx, entry.file, entry.test);
      }
    }
    runTsd(ctx);
    for (const extra of extras || []) {
      if (extra === 'recursive-lazy') {
        recursiveLazyScenario(ctx);
      } else if (extra === 'modifier-inheritance') {
        modifierInheritanceScenario(ctx);
      } else if (extra === 'metadata-precedence') {
        metadataPrecedenceScenario(ctx);
      }
    }
  };
}

const CASES = {
  'F2P-025': { entry: 'spec/types/recursive-schemas.spec.ts :: supports mutual recursion between schemas using getters', run: researchCase({ file: 'spec/types/recursive-schemas.spec.ts', test: 'supports mutual recursion between schemas using getters' }) },
  'F2P-026': { entry: 'spec/modifiers/pipe.spec.ts :: recursive lazy schema generation', run: researchCase({ file: 'spec/modifiers/pipe.spec.ts', test: null }, ['recursive-lazy']) },
  'F2P-027': { entry: 'fixed fixture document serialization plus Metadata.getRefId(.meta({ id }))', run: function run(ctx) { fixtureSerialization(ctx); metadataRegistryScenario(ctx); } },
  'F2P-028': { entry: 'TypeScript type surface of .safeExtend on an augmented ZodObject', run: safeExtendScenario },
  'F2P-029': { entry: 'spec/types/recursive-schemas.spec.ts :: supports recursive schemas with unions using getters', run: researchCase({ file: 'spec/types/recursive-schemas.spec.ts', test: 'supports recursive schemas with unions using getters' }) },
  'F2P-030': { entry: 'spec/modifiers/meta.spec.ts :: should preserve metadata through the meta modifier', run: researchCase({ file: 'spec/modifiers/meta.spec.ts', test: 'should preserve metadata through the meta modifier' }) },
  'F2P-031': { entry: 'metadata merge and precedence between .meta() and .openapi() plus .safeExtend type surface', run: metadataAndSafeExtendScenario },
  'F2P-032': { entry: 'package-lock.json node_modules/zod resolution plus metadata registry', run: dependencyAndMetadataScenario },
  'F2P-033': { entry: 'components generated from .meta({ id }) without an .openapi() call', run: metaIdComponentScenario },
  'F2P-034': { entry: 'spec/types/recursive-schemas.spec.ts :: handles simple recursive schema using getters', run: researchCase({ file: 'spec/types/recursive-schemas.spec.ts', test: 'handles simple recursive schema using getters' }) },
  'F2P-035': { entry: 'spec/modifiers/pipe.spec.ts :: pipe', run: researchCase({ file: 'spec/modifiers/pipe.spec.ts', test: 'can generate schema for pipes' }) },
  'F2P-036': { entry: 'spec/modifiers/meta.spec.ts :: discriminated union schema and discriminator mapping output', run: researchCase({ file: 'spec/modifiers/meta.spec.ts', test: null }) },
  'F2P-037': { entry: 'recursive lazy schema generation through the jest setup-tests path', run: researchCase({ file: 'spec/setup-tests.ts', test: null }, ['recursive-lazy']) },
  'F2P-038': { entry: 'spec/types/recursive-schemas.spec.ts :: supports recursive schemas in unions with getters', run: researchCase({ file: 'spec/types/recursive-schemas.spec.ts', test: 'supports recursive schemas in unions with getters' }) },
  'F2P-039': { entry: 'spec/types/discriminated-union.spec.ts :: metadata and schema inheritance through modifiers', run: researchCase({ file: 'spec/types/discriminated-union.spec.ts', test: null }, ['modifier-inheritance']) },
  'F2P-040': { entry: 'components.schemas and components.parameters ordering plus metadata registry', run: function run(ctx) { orderingScenario(ctx); metadataRegistryScenario(ctx); } },
  'F2P-041': { entry: 'spec/types/recursive-schemas.spec.ts :: supports recursive schemas in nested arrays and objects using getters', run: researchCase({ file: 'spec/types/recursive-schemas.spec.ts', test: 'supports recursive schemas in nested arrays and objects using getters' }) },
  'F2P-042': { entry: 'spec/type-definitions/zod-extensions.test-d.ts :: recursive lazy schema generation', run: researchCase({ file: 'spec/type-definitions/zod-extensions.test-d.ts', test: null }, ['recursive-lazy']) },
  'F2P-043': { entry: 'spec/types/discriminated-union.spec.ts :: creates a discriminator mapping when a registered object uses a zodEnum as the discriminator', run: researchCase({ file: 'spec/types/discriminated-union.spec.ts', test: 'creates a discriminator mapping when a registered object uses a zodEnum as the discriminator' }) },
  'F2P-044': { entry: 'spec/modifiers/pipe.spec.ts :: metadata merge and precedence between .meta() and .openapi()', run: researchCase({ file: 'spec/modifiers/pipe.spec.ts', test: null }, ['metadata-precedence']) },
  'F2P-045': { entry: 'spec/types/discriminated-union.spec.ts :: supports nested discriminated unions', run: researchCase({ file: 'spec/types/discriminated-union.spec.ts', test: 'supports nested discriminated unions' }) },
  'F2P-046': { entry: 'spec/types/recursive-schemas.spec.ts :: basic recursive functionality', run: researchCase({ file: 'spec/types/recursive-schemas.spec.ts', test: 'basic recursive functionality' }) },
  'F2P-047': { entry: 'OpenApiGeneratorV31 plain schema classification and OpenApiGeneratorV32 webhooks', run: v31PlainSchemaAndV32Webhooks },
  'F2P-048': { entry: 'package.json devDependencies zod ^4.1.0 plus .safeExtend runtime behavior', run: dependencyAndSafeExtendScenario }
};

function classify(error) {
  if (error instanceof AssertionFailure || error instanceof MissingCapability) {
    return 'FAIL';
  }
  return 'INFRA_ERROR';
}

function runObservation(ctx, observationId) {
  const definition = CASES[observationId];
  const started = Date.now();
  if (ctx.z && ctx.z.globalRegistry && typeof ctx.z.globalRegistry.clear === 'function') {
    ctx.z.globalRegistry.clear();
  }
  if (ctx.lib && ctx.lib.zodToOpenAPIRegistry && typeof ctx.lib.zodToOpenAPIRegistry.clear === 'function') {
    ctx.lib.zodToOpenAPIRegistry.clear();
  }
  let status = 'INFRA_ERROR';
  let detail = '';
  let behavior = 'NOT_RUN';
  try {
    definition.run(ctx);
    behavior = 'PASS';
  } catch (error) {
    behavior = classify(error);
    detail = error && error.message ? error.message : String(error);
    if (behavior === 'INFRA_ERROR' && error && error.stack) {
      detail = error.stack;
    }
  }

  if (behavior === 'PASS' && ctx.targetTechnology) {
    status = 'PASS';
  } else if (behavior === 'PASS' && !ctx.targetTechnology) {
    status = 'FAIL';
    detail = 'required target technology Zod 4.1.0 is not loaded; loaded Zod ' + ctx.zodVersion;
  } else {
    status = behavior;
  }

  return {
    observation_id: observationId,
    requirement_ids: REQUIREMENTS[observationId],
    status,
    behavior,
    entry: definition.entry,
    side: ctx.side,
    technology: {
      name: 'Zod',
      loaded_version: ctx.zodVersion,
      required_version: '4.1.0',
      satisfied: ctx.targetTechnology
    },
    duration_ms: Date.now() - started,
    detail
  };
}

function buildReport(ctx) {
  return {
    schema_version: 'migration-probe-report:v1',
    batch_id: 'BATCH-F2P-0002',
    side: ctx.side,
    repo: ctx.repo,
    technology: {
      name: 'Zod',
      loaded_version: ctx.zodVersion,
      required_version: '4.1.0',
      satisfied: ctx.targetTechnology
    },
    observations: OBSERVATIONS.map(function map(id) { return runObservation(ctx, id); })
  };
}

function runAll(options) {
  const args = parseArgs(options.argv || []);
  const positional = args.positional || [];
  const reportPath = args.out || args.report || positional.find(function find(value) { return /\.json$/i.test(value); });
  const repoArg = args.repo || positional.find(function find(value) { return !/\.json$/i.test(value); });

  let report;
  try {
    const ctx = createContext({
      repo: repoArg,
      side: options.side,
      dist: args.dist,
      depsBin: args['deps-bin'] || args.depsBin,
      fixtures: args.fixtures,
      zod: args.zod
    });
    report = buildReport(ctx);
  } catch (error) {
    report = {
      schema_version: 'migration-probe-report:v1',
      batch_id: 'BATCH-F2P-0002',
      side: options.side,
      repo: repoArg || process.env.MIGRATION_REPO || '/workspace/repo',
      error: error && error.stack ? error.stack : String(error),
      observations: OBSERVATIONS.map(function map(id) {
        return {
          observation_id: id,
          requirement_ids: REQUIREMENTS[id],
          status: 'INFRA_ERROR',
          behavior: 'INFRA_ERROR',
          side: options.side,
          detail: error && error.message ? error.message : String(error)
        };
      })
    };
  }

  const serialized = JSON.stringify(report, null, 2);
  if (reportPath) {
    fs.writeFileSync(path.resolve(reportPath), serialized + '\n');
  }
  process.stdout.write(serialized + '\n');
  return report;
}

module.exports = {
  OBSERVATIONS,
  REQUIREMENTS,
  runAll
};
