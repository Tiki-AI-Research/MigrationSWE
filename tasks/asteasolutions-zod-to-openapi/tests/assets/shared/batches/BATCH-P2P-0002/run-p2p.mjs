#!/usr/bin/env node
/*
 * BATCH-P2P-0002 runtime probe.
 *
 * Executes the repository's own Jest suite plus one behavior probe per
 * observation. The same script is run against the pre-migration source tree
 * and the reference target tree; every observation must produce an identical
 * normalized projection on both sides.
 *
 * Usage:
 *   node run-p2p.mjs --repo <repo-root> --deps <node_modules-dir> \
 *     --out <results.json> [--baseline <source-root>] [--skip-jest]
 */

import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const args = { skipJest: false };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (key === '--skip-jest') {
      args.skipJest = true;
    } else if (key.startsWith('--')) {
      args[key.slice(2)] = argv[++i];
    }
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
if (!args.repo || !args.deps) {
  console.error('usage: run-p2p.mjs --repo <root> --deps <node_modules> --out <file> [--baseline <root>] [--skip-jest]');
  process.exit(2);
}

const repo = path.resolve(args.repo);
const baseline = path.resolve(args.baseline ?? args.repo);
const deps = path.resolve(args.deps);
const require = createRequire(path.join(repo, 'package.json'));
const Module = require('module');

const observations = JSON.parse(
  fs.readFileSync(path.join(here, 'observations.json'), 'utf8')
);

const ts = require(path.join(deps, 'typescript'));
require.extensions['.ts'] = function (module, filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      esModuleInterop: true,
      allowSyntheticDefaultImports: true,
      experimentalDecorators: true
    },
    fileName: filename
  }).outputText;
  module._compile(output, filename);
};

const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, isMain, options) {
  try {
    return originalResolve.call(this, request, parent, isMain, options);
  } catch (error) {
    try {
      return require.resolve(request, { paths: [deps] });
    } catch (_) {
      throw error;
    }
  }
};

const lib = require(path.join(repo, 'src/index.ts'));
const z = require(require.resolve('zod', { paths: [deps] }));

const {
  extendZodWithOpenApi,
  OpenAPIRegistry,
  OpenApiGeneratorV3,
  OpenApiGeneratorV31,
  OpenApiGeneratorV32
} = lib;
extendZodWithOpenApi(z);

const docConfig = { openapi: '3.0.0', info: { title: 't', version: '1.0.0' } };
const v31Config = { ...docConfig, openapi: '3.1.0' };

function captureConsoleError(fn) {
  const original = console.error;
  const calls = [];
  console.error = (...callArgs) => {
    calls.push(callArgs);
  };
  try {
    const value = fn();
    return { value, calls };
  } finally {
    console.error = original;
  }
}

function errorInfo(fn) {
  try {
    fn();
    return { thrown: false };
  } catch (error) {
    return {
      thrown: true,
      name: error?.constructor?.name,
      message: error?.message
    };
  }
}

function componentsFor(schemas, Generator = OpenApiGeneratorV3, config = docConfig) {
  return new Generator(schemas, config.options).generateComponents();
}

const probes = {
  'P2P-025': () => {
    const s1 = z
      .string()
      .meta({ description: 'meta', deprecated: true })
      .openapi('S', { description: 'openapi' });
    const s2 = z.string().meta({ id: 'Meta' }).openapi('Open');
    const doc = componentsFor([s1, s2]);
    return {
      keys: Object.keys(doc.components.schemas),
      schemas: doc.components.schemas
    };
  },
  'P2P-026': () => {
    const category = z
      .object({
        name: z.string(),
        get subcategories() {
          return z.array(category);
        }
      })
      .openapi('Category');
    const node = z
      .object({
        id: z.string(),
        get child() {
          return node.optional();
        },
        get unionChild() {
          return z.union([node, z.string()]).optional();
        },
        get intersectChild() {
          return z.intersection(node, z.object({ extra: z.string() })).optional();
        }
      })
      .openapi('Node');
    return new OpenApiGeneratorV3([category, node]).generateComponents().components.schemas;
  },
  'P2P-027': () => {
    const s = z.templateLiteral(['id-', z.number()]).openapi('Template');
    return componentsFor([s]).components.schemas;
  },
  'P2P-028': () => {
    const walk = root => {
      const out = new Map();
      const visit = rel => {
        const abs = path.join(root, rel);
        for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
          const child = path.join(rel, entry.name);
          if (entry.isDirectory()) visit(child);
          else out.set(child.replaceAll(path.sep, '/'), fs.readFileSync(path.join(root, child), 'utf8'));
        }
      };
      if (fs.existsSync(path.join(root, 'spec'))) visit('spec');
      return out;
    };
    const before = walk(baseline);
    const after = walk(repo);
    const allowed = new Set([
      'spec/setup-tests.ts',
      'spec/type-definitions/zod-extensions.test-d.ts',
      'spec/types/discriminated-union.spec.ts',
      'spec/types/recursive-schemas.spec.ts',
      'spec/modifiers/pipe.spec.ts',
      'spec/modifiers/meta.spec.ts'
    ]);
    const files = [...new Set([...before.keys(), ...after.keys()])].sort();
    const changed = files.filter(file => before.get(file) !== after.get(file));
    const assertionCount = text => (text.match(/expect\s*\(/g) || []).length;
    const skipCount = text =>
      (text.match(/\b(?:it|test|describe)\s*\.\s*(?:skip|todo)\s*\(/g) || []).length;
    const integrity = changed.map(file => {
      const oldText = before.get(file) ?? '';
      const newText = after.get(file) ?? '';
      return {
        file,
        assertionsPreserved: assertionCount(newText) >= assertionCount(oldText),
        noNewSkips: skipCount(newText) <= skipCount(oldText),
        noTrivialHardcodedExpectation:
          !/expect\s*\(\s*(?:true|1|['"][^'"]*['"])\s*\)\s*\.\s*toBe\s*\(\s*(?:true|1|['"][^'"]*['"])\s*\)/.test(newText)
      };
    });
    return {
      changed,
      outOfScope: changed.filter(file => !allowed.has(file)),
      integrity
    };
  },
  'P2P-029': () => {
    const A = z.object({ kind: z.literal('a'), a: z.string() }).openapi('A');
    const B = z.object({ kind: z.literal('b'), b: z.number() }).openapi('B');
    const du = z.discriminatedUnion('kind', [A, B]).openapi('NoDisc');
    du._zod.def.discriminator = undefined;
    const { value, calls } = captureConsoleError(() =>
      new OpenApiGeneratorV3([A, B, du]).generateComponents()
    );
    const schema = value.components.schemas.NoDisc;
    return {
      messages: calls.map(callArgs => String(callArgs[0])),
      schemaArgPresent: calls.map(callArgs => Boolean(callArgs[1])),
      hasOneOf: Array.isArray(schema.oneOf),
      oneOfLength: schema.oneOf?.length,
      hasDiscriminator: Object.prototype.hasOwnProperty.call(schema, 'discriminator')
    };
  },
  'P2P-030': () => {
    const registry = new OpenAPIRegistry();
    const TestParam = registry.registerParameter(
      'TestParam',
      z.string().openapi({ param: { name: 'test', in: 'path' } })
    );
    registry.registerPath({
      method: 'get',
      path: '/test/{test}',
      request: { params: z.object({ test: TestParam }) },
      responses: { 200: { description: 'ok' } }
    });
    const doc = new OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig);
    return {
      docParameters: doc.components.parameters,
      pathParameters: doc.paths['/test/{test}'].get.parameters
    };
  },
  'P2P-031': () => {
    const B = z.object({ n: z.number() }).openapi('B');
    const A = z.object({ b: B, list: z.array(B), child: z.object({ b: B }) }).openapi('A');
    const components = new OpenApiGeneratorV3([A, B]).generateComponents();
    const serialized = JSON.stringify(components);
    return {
      serialized,
      refs: [...serialized.matchAll(/#\/components\/schemas\/[A-Za-z0-9_.-]+/g)].map(m => m[0])
    };
  },
  'P2P-032': () => {
    const recursive = z
      .lazy(() =>
        z.object({
          name: z.string(),
          get next() {
            return recursive.nullable();
          }
        })
      )
      .openapi('Recursive');
    const pendingParent = z
      .object({
        get child() {
          return pendingChild.nullable();
        }
      })
      .openapi('PendingParent');
    const pendingChild = z
      .object({
        get parent() {
          return pendingParent.nullable();
        }
      })
      .openapi('PendingChild');
    const already = z.string().openapi('Already').nullable();
    const holder = z
      .object({ a: already.nullable(), b: recursive.nullable() })
      .openapi('Holder');
    const schemas = [recursive, pendingParent, pendingChild, already, holder];
    return {
      v3: new OpenApiGeneratorV3(schemas).generateComponents().components.schemas,
      v31: new OpenApiGeneratorV31(schemas).generateComponents().components.schemas,
      v32: new OpenApiGeneratorV32(schemas).generateComponents().components.schemas
    };
  },
  'P2P-033': () => {
    const methods = Ctor =>
      Object.getOwnPropertyNames(Ctor.prototype)
        .filter(name => name !== 'constructor')
        .sort();
    return {
      exports: Object.keys(lib).sort(),
      registryMethods: methods(OpenAPIRegistry),
      v3Methods: methods(OpenApiGeneratorV3),
      v31Methods: methods(OpenApiGeneratorV31),
      v32Methods: methods(OpenApiGeneratorV32)
    };
  },
  'P2P-034': () => {
    const key = ['dyn', String(Date.now()).length].join('');
    const s = z.object({ [key]: z.string() }).openapi('Dynamic');
    return componentsFor([s]).components.schemas;
  },
  'P2P-035': () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(repo, 'package.json'), 'utf8'));
    const indexText = fs.readFileSync(path.join(repo, 'src/index.ts'), 'utf8');
    const exportStatements = [...indexText.matchAll(/export\s+(?:\*|\{[^}]*\})/g)]
      .map(match => match[0])
      .sort();
    return {
      main: pkg.main,
      module: pkg.module,
      types: pkg.types,
      moduleExports: Object.keys(lib).sort(),
      indexExportStatements: exportStatements
    };
  },
  'P2P-036': () => {
    const registry = new OpenAPIRegistry();
    registry.register('Schema', z.string());
    const paramRef = registry.registerParameter(
      'Param',
      z.string().openapi({ param: { name: 'p', in: 'query' } })
    );
    registry.registerPath({
      method: 'get',
      path: '/p',
      request: { query: z.object({ p: paramRef }) },
      responses: { 200: { description: 'ok' } }
    });
    registry.registerWebhook({
      method: 'post',
      path: '/hook',
      responses: { 200: { description: 'ok' } }
    });
    registry.registerComponent('headers', 'Header', {
      description: 'h',
      schema: { type: 'string' }
    });
    const doc = new OpenApiGeneratorV31(registry.definitions).generateDocument(v31Config);
    return {
      definitions: registry.definitions.map(definition => definition.type),
      schemas: doc.components.schemas,
      parameters: doc.components.parameters,
      headers: doc.components.headers,
      pathKeys: Object.keys(doc.paths).sort(),
      webhookKeys: Object.keys(doc.webhooks).sort()
    };
  },
  'P2P-037': () => {
    const unsupported = errorInfo(() =>
      new OpenApiGeneratorV3([z.symbol()]).generateComponents()
    );
    const missingName = errorInfo(() => {
      const registry = new OpenAPIRegistry();
      registry.registerPath({
        method: 'get',
        path: '/path',
        request: { headers: [z.string()] },
        responses: { 200: { description: 'ok' } }
      });
      return new OpenApiGeneratorV3(registry.definitions).generateComponents();
    });
    const missingLocation = errorInfo(() => {
      const registry = new OpenAPIRegistry();
      registry.registerParameter('P', z.string().openapi({ param: { name: 'p' } }));
      return new OpenApiGeneratorV3(registry.definitions).generateComponents();
    });
    const conflict = errorInfo(() => {
      const registry = new OpenAPIRegistry();
      const P = registry.registerParameter(
        'P',
        z.string().openapi({ param: { name: 'p', in: 'query' } })
      );
      registry.registerPath({
        method: 'get',
        path: '/p',
        request: { query: z.object({ other: P }) },
        responses: { 200: { description: 'ok' } }
      });
      return new OpenApiGeneratorV3(registry.definitions).generateComponents();
    });
    return { unsupported, missingName, missingLocation, conflict };
  },
  'P2P-038': () => {
    const registry = new OpenAPIRegistry();
    registry.registerWebhook({
      method: 'post',
      path: '/event',
      responses: { 200: { description: 'ok' } }
    });
    const doc = new OpenApiGeneratorV31(registry.definitions).generateDocument(v31Config);
    return {
      webhooks: doc.webhooks,
      versions: doc.openapi,
      hasPaths: Object.prototype.hasOwnProperty.call(doc, 'paths')
    };
  },
  'P2P-039': () => {
    const registry = new OpenAPIRegistry();
    registry.registerPath({
      method: 'get',
      path: '/',
      responses: {
        200: {
          description: 'Simple response',
          content: {
            'application/json': {
              schema: { type: 'string', example: 'test' }
            }
          }
        },
        404: {
          description: 'Missing object',
          content: {
            'application/json': {
              schema: { $ref: '#/components/schemas/SomeRef' }
            }
          }
        }
      }
    });
    return new OpenApiGeneratorV3(registry.definitions).generateDocument(docConfig)
      .paths['/'].get.responses;
  },
  'P2P-040': () => {
    const registry = new OpenAPIRegistry();
    registry.register('S', z.string().openapi('S'));
    registry.registerPath({
      method: 'get',
      path: '/',
      responses: { 200: { description: 'ok' } }
    });
    registry.registerWebhook({
      method: 'post',
      path: '/hook',
      responses: { 200: { description: 'ok' } }
    });
    const generator = new OpenApiGeneratorV3(registry.definitions);
    const doc = generator.generateDocument(docConfig);
    const components = generator.generateComponents();
    return {
      docKeys: Object.keys(doc).sort(),
      hasPaths: Object.prototype.hasOwnProperty.call(doc, 'paths'),
      hasComponents: Object.prototype.hasOwnProperty.call(doc, 'components'),
      hasWebhooks: Object.prototype.hasOwnProperty.call(doc, 'webhooks'),
      componentsKeys: Object.keys(components.components).sort(),
      version: doc.openapi
    };
  }
};

function runJest() {
  if (args.skipJest) {
    return { ok: true, skipped: true, summary: null, tests: new Map(), error: null };
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'p2p-jest-'));
  const configPath = path.join(tmp, 'jest.config.cjs');
  const outputPath = path.join(tmp, 'jest-results.json');
  fs.writeFileSync(
    configPath,
    `module.exports = {
  rootDir: ${JSON.stringify(repo)},
  testEnvironment: 'node',
  setupFilesAfterEnv: [${JSON.stringify(path.join(repo, 'spec/setup-tests.ts'))}],
  transform: {
    '^.+\\\\.tsx?$': [${JSON.stringify(path.join(deps, 'ts-jest'))}, { tsconfig: ${JSON.stringify(path.join(repo, 'tsconfig.json'))}, diagnostics: false }]
  },
  modulePaths: [${JSON.stringify(deps)}],
  testMatch: ['**/?(*.)+(spec|test).ts']
};
`
  );
  const result = spawnSync(
    process.execPath,
    [
      path.join(deps, 'jest', 'bin', 'jest.js'),
      '--config',
      configPath,
      '--runInBand',
      '--json',
      `--outputFile=${outputPath}`
    ],
    {
      cwd: repo,
      encoding: 'utf8',
      env: { ...process.env, CI: '1' },
      timeout: 600000
    }
  );
  let report = null;
  try {
    report = JSON.parse(fs.readFileSync(outputPath, 'utf8'));
  } catch (error) {
    return {
      ok: false,
      skipped: false,
      summary: null,
      tests: new Map(),
      error: `jest report unavailable: ${error.message}; stderr=${(result.stderr || '').slice(-2000)}`
    };
  }
  const tests = new Map();
  for (const suite of report.testResults) {
    const spec = path
      .relative(repo, suite.name)
      .replaceAll(path.sep, '/');
    for (const assertion of suite.assertionResults) {
      tests.set(`${spec}::${assertion.fullName}`, assertion.status);
    }
  }
  const summary = {
    suites: report.numTotalTestSuites,
    passedSuites: report.numPassedTestSuites,
    failedSuites: report.numFailedTestSuites,
    tests: report.numTotalTests,
    passedTests: report.numPassedTests,
    failedTests: report.numFailedTests,
    skippedTests: report.numPendingTests,
    success: report.success
  };
  return { ok: report.success === true, skipped: false, summary, tests, error: null };
}

const jest = runJest();

const results = {};
for (const observation of observations.observations) {
  const selectedTests = [];
  for (const selector of observation.jest ?? []) {
    const requested = selector.titles
      ? selector.titles.map(title => `${selector.spec}::${title}`)
      : [...jest.tests.keys()].filter(key => key.startsWith(`${selector.spec}::`));
    for (const key of requested) {
      const status = jest.tests.get(key);
      if (status !== undefined) {
        const [spec, fullName] = key.split('::');
        selectedTests.push({ spec, fullName, status });
      }
    }
  }
  selectedTests.sort((a, b) =>
    `${a.spec}::${a.fullName}`.localeCompare(`${b.spec}::${b.fullName}`)
  );

  let value = null;
  let probeStatus = 'PASS';
  let probeError = null;
  try {
    value = probes[observation.id]();
  } catch (error) {
    probeStatus = 'FAIL';
    probeError = String((error && error.stack) || error);
  }

  const testsOk =
    !jest.error &&
    (selectedTests.length > 0 || (observation.jest ?? []).length === 0) &&
    selectedTests.every(test => test.status === 'passed');
  const status = probeStatus === 'PASS' && testsOk && jest.ok ? 'PASS' : 'FAIL';

  results[observation.id] = {
    status,
    projection: {
      value,
      tests: selectedTests
    },
    ...(probeError ? { probe_error: probeError } : {})
  };
}

const payload = {
  schema_version: 'migration-p2p-results:v1',
  batch_id: observations.batch_id,
  repo,
  baseline,
  dependencies: deps,
  jest: {
    skipped: jest.skipped,
    summary: jest.summary,
    error: jest.error
  },
  observations: results
};

const text = JSON.stringify(payload, null, 2) + '\n';
if (args.out) {
  fs.mkdirSync(path.dirname(path.resolve(args.out)), { recursive: true });
  fs.writeFileSync(path.resolve(args.out), text);
}
process.stdout.write(text);

const allPassed = Object.values(results).every(result => result.status === 'PASS');
process.exit(allPassed ? 0 : 1);
