#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const BATCH_ID = 'BATCH-DIFFERENTIAL-0002';
const OBSERVATION_IDS = ['DIFF-033', 'DIFF-034', 'DIFF-035', 'DIFF-036'];
const ADAPTABLE_SPEC_FILES = [
  'spec/setup-tests.ts',
  'spec/type-definitions/zod-extensions.test-d.ts',
  'spec/types/discriminated-union.spec.ts',
  'spec/types/recursive-schemas.spec.ts',
  'spec/modifiers/pipe.spec.ts',
  'spec/modifiers/meta.spec.ts',
];

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const defaultAssetRoot = path.resolve(scriptDir, '../../..');
const repoRoot = path.resolve(process.argv[2] ?? process.cwd());
const assetRoot = path.resolve(process.argv[3] ?? defaultAssetRoot);
const sharedDir = path.join(assetRoot, 'shared', 'batches', BATCH_ID);
const fixturesDir = path.join(assetRoot, 'fixtures', 'batches', BATCH_ID);

const corpus = readJson(path.join(sharedDir, 'corpus.json'));
const baseline = readJson(path.join(fixturesDir, 'baseline.json'));

const requireFromRepo = createRequire(path.join(repoRoot, 'package.json'));
const zod = requireFromRepo('zod');
const library = await import(
  pathToFileURL(path.join(repoRoot, 'dist', 'index.mjs')).href
);
library.extendZodWithOpenApi(zod);

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function canonicalize(value) {
  if (value === undefined) {
    return null;
  }
  if (typeof value === 'bigint') {
    return `${value}n`;
  }
  if (typeof value === 'number' && !Number.isFinite(value)) {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (value && typeof value === 'object') {
    if (value instanceof Error) {
      return { name: value.constructor.name, message: value.message };
    }
    const result = {};
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) {
        result[key] = canonicalize(value[key]);
      }
    }
    return result;
  }
  return value;
}

function canonicalJson(value) {
  return JSON.stringify(canonicalize(value));
}

function equal(actual, expected, label) {
  const actualJson = canonicalJson(actual);
  const expectedJson = canonicalJson(expected);
  if (actualJson !== expectedJson) {
    throw new Error(`${label}: expected ${expectedJson}, received ${actualJson}`);
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function runCheck(name, action) {
  try {
    const value = action();
    return { name, ok: true, value: canonicalize(value) };
  } catch (error) {
    return {
      name,
      ok: false,
      error: error && error.message ? error.message : String(error),
    };
  }
}

function observationResult(checks) {
  return {
    ok: checks.every(check => check.ok),
    checks,
  };
}

function digest(text) {
  return `sha256:${crypto.createHash('sha256').update(text).digest('hex')}`;
}

function readText(relativePath) {
  return fs.readFileSync(path.join(repoRoot, relativePath), 'utf8');
}

function generateComponents(version, schemas, options) {
  const definitions = schemas.map(schema => ({ type: 'schema', schema }));
  const generator = new library[version](definitions, options);
  return generator.generateComponents().components;
}

function captureError(action) {
  try {
    action();
    return { name: null, message: null, prototypeChain: [] };
  } catch (error) {
    const prototypeChain = [];
    let prototype = Object.getPrototypeOf(error);
    while (prototype) {
      if (prototype.constructor && prototype.constructor.name) {
        prototypeChain.push(prototype.constructor.name);
      }
      prototype = Object.getPrototypeOf(prototype);
    }
    return {
      name: error && error.constructor ? error.constructor.name : String(error),
      message: error && error.message ? error.message : null,
      prototypeChain,
    };
  }
}

const checksByObservation = Object.fromEntries(
  OBSERVATION_IDS.map(id => [id, []])
);

const case33 = corpus.cases.find(item => item.observation_id === 'DIFF-033');
const case34 = corpus.cases.find(item => item.observation_id === 'DIFF-034');
const case35 = corpus.cases.find(item => item.observation_id === 'DIFF-035');
const case36 = corpus.cases.find(item => item.observation_id === 'DIFF-036');

checksByObservation['DIFF-033'].push(
  runCheck('runtime-created object keys drive generated properties', () => {
    const makeDynamicSchema = input => {
      const firstKey = input.first_key_parts.join('');
      const secondKey = String.fromCharCode(...input.second_key_codes);
      const schema = zod
        .object({
          [firstKey]: zod.string(),
          [secondKey]: zod.number().int().min(0),
        })
        .openapi(input.ref_id);
      return { firstKey, secondKey, schema };
    };

    const first = makeDynamicSchema(case33.inputs.first);
    const second = makeDynamicSchema(case33.inputs.second);
    const specialFirstKey = case33.inputs.special.first_key_parts.join('');
    const specialSecondKey = String.fromCharCode(...case33.inputs.special.second_key_codes);
    const special = zod
      .object({
        [specialFirstKey]: zod.string().email(),
        [specialSecondKey]: zod.boolean(),
      })
      .openapi(case33.inputs.special.ref_id);
    const results = {};

    for (const version of ['OpenApiGeneratorV3', 'OpenApiGeneratorV31', 'OpenApiGeneratorV32']) {
      results[version] = {
        first: generateComponents(version, [first.schema]).schemas,
        second: generateComponents(version, [second.schema]).schemas,
        special: generateComponents(version, [special]).schemas,
      };
    }

    const firstSchema = results.OpenApiGeneratorV3.first[case33.inputs.first.ref_id];
    const secondSchema = results.OpenApiGeneratorV3.second[case33.inputs.second.ref_id];
    const specialSchema = results.OpenApiGeneratorV3.special[case33.inputs.special.ref_id];
    equal(Object.keys(firstSchema.properties), [first.firstKey, first.secondKey], 'first dynamic properties');
    equal(Object.keys(secondSchema.properties), [second.firstKey, second.secondKey], 'second dynamic properties');
    equal(Object.keys(specialSchema.properties), [specialFirstKey, specialSecondKey], 'special dynamic properties');
    equal(firstSchema.properties[first.firstKey], { type: 'string' }, 'first string property');
    equal(firstSchema.properties[first.secondKey].type, 'integer', 'first integer property');
    equal(secondSchema.properties[second.firstKey], { type: 'string' }, 'second string property');
    equal(secondSchema.properties[second.secondKey].type, 'integer', 'second integer property');
    equal(specialSchema.properties[specialFirstKey].format, 'email', 'special email property');
    equal(specialSchema.properties[specialSecondKey].type, 'boolean', 'special boolean property');

    return {
      firstKeys: Object.keys(firstSchema.properties),
      secondKeys: Object.keys(secondSchema.properties),
      specialKeys: Object.keys(specialSchema.properties),
      allVersionsAgree: ['OpenApiGeneratorV3', 'OpenApiGeneratorV31', 'OpenApiGeneratorV32'].every(
        version => canonicalJson(results[version].first) === canonicalJson(results.OpenApiGeneratorV3.first)
      ),
      components: results,
    };
  })
);

checksByObservation['DIFF-034'].push(
  runCheck('documented runtime exports remain callable', () => {
    const names = Object.keys(library).sort();
    equal(names, baseline.runtime_exports, 'runtime exports');
    for (const name of baseline.runtime_exports) {
      if (name === 'zodToOpenAPIRegistry') {
        assert(typeof library[name] === 'object', `${name} is not a registry object`);
        for (const method of ['add', 'get', 'has', 'remove']) {
          assert(typeof library[name][method] === 'function', `${name}.${method} is not callable`);
        }
      } else {
        assert(typeof library[name] === 'function', `${name} is not callable`);
      }
    }
    return names;
  }),
  runCheck('registry methods and definitions getter remain callable', () => {
    const registry = new library.OpenAPIRegistry();
    const Pet = registry.register(
      'Pet',
      zod.object({ name: zod.string(), age: zod.number().int().min(0) })
    );
    const PetId = registry.registerParameter(
      'PetId',
      zod.string().openapi({ param: { name: 'petId', in: 'path' } })
    );
    registry.registerPath({
      method: 'get',
      path: '/pets/{petId}',
      request: { params: zod.object({ petId: PetId }) },
      responses: {
        200: {
          description: 'ok',
          content: { 'application/json': { schema: Pet } },
        },
      },
    });
    registry.registerWebhook({
      method: 'post',
      path: '/pet-created',
      request: { body: { content: { 'application/json': { schema: Pet } } } },
      responses: { 200: { description: 'ok' } },
    });
    const rawComponent = registry.registerComponent('schemas', 'Manual', {
      type: 'object',
      properties: { value: { type: 'string' } },
    });

    const methods = [
      'register',
      'registerParameter',
      'registerPath',
      'registerWebhook',
      'registerComponent',
    ];
    for (const method of methods) {
      assert(typeof registry[method] === 'function', `${method} is not callable`);
    }
    assert(Array.isArray(registry.definitions), 'definitions is not an array');

    return {
      methods,
      definitionTypes: registry.definitions.map(definition => definition.type),
      rawComponent,
      definitionsLength: registry.definitions.length,
    };
  }),
  runCheck('generator constructors and document methods remain callable', () => {
    const registry = new library.OpenAPIRegistry();
    const Pet = registry.register('Pet', zod.object({ name: zod.string() }));
    registry.registerPath({
      method: 'get',
      path: '/pets',
      responses: {
        200: {
          description: 'ok',
          content: { 'application/json': { schema: Pet } },
        },
      },
    });
    registry.registerWebhook({
      method: 'post',
      path: '/pet-created',
      responses: { 200: { description: 'ok' } },
    });

    const result = {};
    for (const version of ['OpenApiGeneratorV3', 'OpenApiGeneratorV31', 'OpenApiGeneratorV32']) {
      const generator = new library[version](registry.definitions);
      assert(typeof generator.generateDocument === 'function', `${version}.generateDocument`);
      assert(typeof generator.generateComponents === 'function', `${version}.generateComponents`);
      const config = {
        openapi: version === 'OpenApiGeneratorV3' ? '3.0.0' : version === 'OpenApiGeneratorV31' ? '3.1.0' : '3.2.0',
        info: { title: 'capabilities', version: '1.0.0' },
      };
      const document = generator.generateDocument(config);
      const components = generator.generateComponents();
      result[version] = {
        hasPaths: Object.prototype.hasOwnProperty.call(document, 'paths'),
        hasWebhooks: Object.prototype.hasOwnProperty.call(document, 'webhooks'),
        webhookPaths: document.webhooks ? Object.keys(document.webhooks) : [],
        schemaNames: Object.keys(components.components.schemas).sort(),
        parameterNames: Object.keys(components.components.parameters).sort(),
      };
    }
    return result;
  }),
  runCheck('generator options remain callable', () => {
    const union = zod.union([zod.string(), zod.number()]).openapi('Union');
    const preferredOneOf = generateComponents('OpenApiGeneratorV3', [union], {
      unionPreferredType: 'oneOf',
    }).schemas.Union;
    const preferredAnyOf = generateComponents('OpenApiGeneratorV3', [union], {
      unionPreferredType: 'anyOf',
    }).schemas.Union;
    const overridden = zod
      .union([zod.string(), zod.number()])
      .openapi('OverriddenUnion', { description: 'schema option' }, { unionPreferredType: 'oneOf' });
    const overriddenSchema = generateComponents('OpenApiGeneratorV3', [overridden], {
      unionPreferredType: 'anyOf',
    }).schemas.OverriddenUnion;
    const overriddenAnyOf = zod
      .union([zod.string(), zod.number()])
      .openapi('OverriddenAnyOf', { description: 'schema option' }, { unionPreferredType: 'anyOf' });
    const overriddenAnyOfSchema = generateComponents('OpenApiGeneratorV3', [overriddenAnyOf], {
      unionPreferredType: 'oneOf',
    }).schemas.OverriddenAnyOf;

    const sortedRegistry = new library.OpenAPIRegistry();
    sortedRegistry.register('Zulu', zod.object({ z: zod.string() }));
    sortedRegistry.register('Alpha', zod.object({ a: zod.string() }));
    sortedRegistry.register('Mike', zod.object({ m: zod.string() }));
    const sorted = new library.OpenApiGeneratorV3(sortedRegistry.definitions, {
      sortComponents: 'alphabetically',
    }).generateComponents().components.schemas;

    equal(Object.keys(preferredOneOf), ['oneOf'], 'generator unionPreferredType oneOf');
    equal(Object.keys(preferredAnyOf), ['anyOf'], 'generator unionPreferredType anyOf');
    equal(Object.keys(overriddenSchema), ['oneOf', 'description'], 'schema unionPreferredType oneOf override');
    equal(Object.keys(overriddenAnyOfSchema), ['anyOf', 'description'], 'schema unionPreferredType anyOf override');
    equal(Object.keys(sorted), ['Alpha', 'Mike', 'Zulu'], 'sortComponents alphabetically');

    return {
      generatorOneOf: Object.keys(preferredOneOf),
      generatorAnyOf: Object.keys(preferredAnyOf),
      schemaOverrideOneOf: Object.keys(overriddenSchema),
      schemaOverrideAnyOf: Object.keys(overriddenAnyOfSchema),
      sortedComponents: Object.keys(sorted),
    };
  }),
  runCheck('README-listed Zod mappings remain available', () => {
    const NumericEnum = { FIRST: 1, SECOND: 2 };
    const mixedEnum = { FIRST: 1, SECOND: 'two' };

    const schemas = {
      NumberFloor: zod.number().int().min(0).openapi('NumberFloor'),
      Email: zod.string().email().openapi('Email'),
      Catchall: zod.object({}).catchall(zod.string()).openapi('Catchall'),
      Record: zod.record(zod.string(), zod.number()).openapi('Record'),
      Intersection: zod.intersection(
        zod.object({ left: zod.string() }),
        zod.object({ right: zod.number() })
      ).openapi('Intersection'),
      Literal: zod.literal('fixed').openapi('Literal'),
      TemplateLiteral: zod.templateLiteral([zod.literal('v'), zod.number()]).openapi('TemplateLiteral'),
      Date: zod.date().openapi('Date'),
      BigInt: zod.bigint().openapi('BigInt'),
      Array: zod.array(zod.string()).openapi('Array'),
      Union: zod.union([zod.string(), zod.number()]).openapi('Union'),
      NativeEnum: zod.nativeEnum(NumericEnum).openapi('NativeEnum'),
    };

    const components = generateComponents('OpenApiGeneratorV3', Object.values(schemas));
    const mappingValues = Object.fromEntries(
      Object.keys(schemas).map(name => [name, components.schemas[name]])
    );
    equal(mappingValues.NumberFloor, { type: 'integer', minimum: 0 }, 'number mapping');
    equal(mappingValues.Email, { type: 'string', format: 'email' }, 'email mapping');
    equal(mappingValues.Catchall.additionalProperties, { type: 'string' }, 'catchall mapping');
    equal(mappingValues.Literal, { type: 'string', enum: ['fixed'] }, 'literal mapping');
    equal(mappingValues.Date, { type: 'string', format: 'date-time' }, 'date mapping');
    assert(mappingValues.Record.type === 'object', 'record is not an object');
    assert(mappingValues.Intersection.allOf, 'intersection has no allOf');
    assert(mappingValues.TemplateLiteral.type === 'string', 'template literal is not a string');
    equal(mappingValues.BigInt, { type: 'string', pattern: '^\\d+$' }, 'bigint mapping');
    assert(mappingValues.Array.type === 'array', 'array is not an array');
    assert(mappingValues.Union.anyOf, 'union has no anyOf');
    equal(mappingValues.NativeEnum, { type: 'integer', enum: [1, 2] }, 'numeric enum mapping');

    const mixedEnumError = captureError(() =>
      generateComponents('OpenApiGeneratorV3', [
        zod.nativeEnum(mixedEnum).openapi('MixedEnum'),
      ])
    );
    equal(mixedEnumError.name, 'ZodToOpenAPIError', 'mixed enum error class');

    return { mappingValues, mixedEnumError };
  }),
  runCheck('parameter and unsupported type errors keep identity and messages', () => {
    const unknown = captureError(() =>
      generateComponents('OpenApiGeneratorV3', [zod.symbol().openapi('Symbol')])
    );

    const missingParameter = captureError(() => {
      const generator = new library.OpenApiGeneratorV3([
        {
          type: 'route',
          route: {
            method: 'get',
            path: '/path',
            request: { headers: [zod.string()] },
            responses: { 200: { description: 'ok' } },
          },
        },
      ]);
      generator.generateDocument({ openapi: '3.0.0', info: { title: 't', version: '1' } });
    });

    const TestHeader = zod
      .string()
      .openapi({ param: { name: 'test', in: 'header' } });
    const conflict = captureError(() => {
      const registry = new library.OpenAPIRegistry();
      const header = registry.registerParameter('TestHeader', TestHeader);
      registry.registerPath({
        method: 'get',
        path: '/',
        request: { query: zod.object({ test: header }) },
        responses: { 200: { description: 'ok' } },
      });
      new library.OpenApiGeneratorV3(registry.definitions).generateDocument({
        openapi: '3.0.0',
        info: { title: 't', version: '1' },
      });
    });

    equal(unknown.name, 'UnknownZodTypeError', 'unknown type error class');
    equal(
      unknown.message,
      'Unknown zod object type, please specify `type` and other OpenAPI props using `schema.openapi`.',
      'unknown type error message'
    );
    equal(missingParameter.name, 'MissingParameterDataError', 'missing parameter error class');
    assert(
      missingParameter.message.startsWith('Missing parameter data, please specify `name`') ||
        missingParameter.message.startsWith('Missing parameter data, please specify `in`'),
      `unexpected missing parameter message: ${missingParameter.message}`
    );
    equal(conflict.name, 'ConflictError', 'conflict error class');
    assert(conflict.message.startsWith('Conflicting location'), 'unexpected conflict message');

    return { unknown, missingParameter, conflict };
  })
);

checksByObservation['DIFF-035'].push(
  runCheck('protected spec files are unchanged', () => {
    const expectedProtected = baseline.protected_spec_hashes;
    const actualProtected = {};
    const missing = [];
    const changed = [];
    for (const [relativePath, expectedHash] of Object.entries(expectedProtected)) {
      const absolutePath = path.join(repoRoot, relativePath);
      if (!fs.existsSync(absolutePath)) {
        missing.push(relativePath);
        continue;
      }
      actualProtected[relativePath] = digest(readText(relativePath));
      if (actualProtected[relativePath] !== expectedHash) {
        changed.push(relativePath);
      }
    }
    equal(missing, [], 'missing protected spec files');
    equal(changed, [], 'changed protected spec files');
    return {
      protectedFileCount: Object.keys(expectedProtected).length,
      missing,
      changed,
    };
  }),
  runCheck('adaptable spec files retain assertions and test titles', () => {
    const results = {};
    const failures = [];

    for (const relativePath of ADAPTABLE_SPEC_FILES) {
      const expected = baseline.adaptable_specs[relativePath];
      const absolutePath = path.join(repoRoot, relativePath);
      if (!fs.existsSync(absolutePath)) {
        failures.push(`${relativePath}: missing`);
        continue;
      }
      const text = readText(relativePath);
      const assertionCount = (text.match(/\b(?:expect|expectSchema|expectType)\s*\(/g) || []).length;
      const skipCount = (text.match(/\.(?:skip|todo)\s*\(/g) || []).length;
      const testTitles = Array.from(
        text.matchAll(/^\s*(?:it|test|describe)(?:\.\w+)?\s*\(\s*(['"`])(.*?)\1/gm)
      ).map(match => match[2]);
      const missingTitles = expected.test_titles.filter(title => !testTitles.includes(title));
      if (assertionCount < expected.assertions) {
        failures.push(`${relativePath}: assertions ${assertionCount} < ${expected.assertions}`);
      }
      if (skipCount > expected.skip_markers) {
        failures.push(`${relativePath}: skip markers ${skipCount} > ${expected.skip_markers}`);
      }
      if (missingTitles.length > 0) {
        failures.push(`${relativePath}: missing titles ${missingTitles.join(', ')}`);
      }
      results[relativePath] = {
        assertions: assertionCount,
        baselineAssertions: expected.assertions,
        skipMarkers: skipCount,
        baselineSkipMarkers: expected.skip_markers,
        missingTitles,
      };
    }

    equal(failures, [], 'adaptable spec integrity failures');
    return results;
  }),
  runCheck('no unexpected spec files were introduced', () => {
    const expectedPaths = new Set([
      ...Object.keys(baseline.protected_spec_hashes),
      ...ADAPTABLE_SPEC_FILES,
    ]);
    const actualPaths = [];
    const walk = relativeDirectory => {
      const absoluteDirectory = path.join(repoRoot, relativeDirectory);
      for (const entry of fs.readdirSync(absoluteDirectory, { withFileTypes: true })) {
        const relativePath = path.posix.join(relativeDirectory, entry.name);
        if (entry.isDirectory()) {
          walk(relativePath);
        } else {
          actualPaths.push(relativePath);
        }
      }
    };
    walk('spec');
    const unexpected = actualPaths.filter(relativePath => !expectedPaths.has(relativePath)).sort();
    const missing = [...expectedPaths].filter(relativePath => !actualPaths.includes(relativePath)).sort();
    // Additional regression files do not modify the protected baseline.
    // Report their paths for scoped comparison; native tests and the hard
    // gate still check their outcomes, assertions and skip markers.
    equal(missing, [], 'missing spec files');
    return { fileCount: actualPaths.length, unexpected, missing };
  })
);

checksByObservation['DIFF-036'].push(
  runCheck('protected example files are byte-identical to baseline', () => {
    const results = {};
    const changed = [];
    for (const [relativePath, expectedHash] of Object.entries(baseline.protected_example_hashes)) {
      const actualHash = digest(readText(relativePath));
      results[relativePath] = actualHash;
      if (actualHash !== expectedHash) {
        changed.push(relativePath);
      }
    }
    equal(changed, [], 'changed example files');
    return results;
  }),
  runCheck('public exports and built entry points are preserved', () => {
    equal(Object.keys(library).sort(), baseline.runtime_exports, 'runtime exports');
    const packageJson = readJson(path.join(repoRoot, 'package.json'));
    const entries = {
      main: packageJson.main,
      module: packageJson.module,
      types: packageJson.types,
      hasCjs: fs.existsSync(path.join(repoRoot, 'dist/index.cjs')),
      hasMjs: fs.existsSync(path.join(repoRoot, 'dist/index.mjs')),
      hasTypes: fs.existsSync(path.join(repoRoot, 'dist/index.d.ts')),
    };
    equal(
      { main: entries.main, module: entries.module, types: entries.types },
      baseline.package_entries,
      'package entries'
    );
    assert(entries.hasCjs && entries.hasMjs && entries.hasTypes, 'missing built entry point');
    return { exports: Object.keys(library).sort(), entries };
  }),
  runCheck('generated documents and observable error text are stable', () => {
    const Pet = zod
      .object({
        id: zod.string(),
        age: zod.number().int().min(0),
        tags: zod.array(zod.string()),
        kind: zod.literal('pet'),
      })
      .openapi('Pet');
    const route = {
      method: 'get',
      path: '/pets/{id}',
      request: { params: zod.object({ id: zod.string() }) },
      responses: {
        200: {
          description: 'ok',
          content: { 'application/json': { schema: Pet } },
        },
      },
    };
    const definitions = [
      { type: 'schema', schema: Pet },
      { type: 'route', route },
    ];
    const documents = {};
    for (const version of ['OpenApiGeneratorV3', 'OpenApiGeneratorV31', 'OpenApiGeneratorV32']) {
      const openapi = version === 'OpenApiGeneratorV3' ? '3.0.0' : version === 'OpenApiGeneratorV31' ? '3.1.0' : '3.2.0';
      const generator = new library[version](definitions);
      documents[version] = generator.generateDocument({
        openapi,
        info: { title: 'artifact', version: '1.0.0' },
      });
    }

    const unknown = captureError(() =>
      generateComponents('OpenApiGeneratorV3', [zod.symbol().openapi('Symbol')])
    );
    const mixedEnum = captureError(() =>
      generateComponents('OpenApiGeneratorV3', [
        zod.nativeEnum({ FIRST: 1, SECOND: 'two' }).openapi('MixedEnum'),
      ])
    );

    return {
      documentSha256: Object.fromEntries(
        Object.entries(documents).map(([version, document]) => [version, digest(canonicalJson(document))])
      ),
      documents,
      errors: { unknown, mixedEnum },
    };
  })
);

const observations = Object.fromEntries(
  OBSERVATION_IDS.map(observationId => [
    observationId,
    observationResult(checksByObservation[observationId]),
  ])
);

const result = {
  schema_version: 'migration-differential-probe:v1',
  batch_id: BATCH_ID,
  observations,
  ok: OBSERVATION_IDS.every(observationId => observations[observationId].ok),
};

process.stdout.write(`${JSON.stringify(canonicalize(result), null, 2)}\n`);
if (!result.ok) {
  process.exitCode = 1;
}
