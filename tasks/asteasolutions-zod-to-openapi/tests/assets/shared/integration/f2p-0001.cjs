'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const childProcess = require('node:child_process');

const BATCH_ID = 'BATCH-F2P-0001';
const OBSERVATION_IDS = [
  'F2P-001', 'F2P-005', 'F2P-010', 'F2P-011', 'F2P-020',
  'F2P-003', 'F2P-015', 'F2P-002', 'F2P-004', 'F2P-006',
  'F2P-007', 'F2P-008', 'F2P-009', 'F2P-012', 'F2P-013',
  'F2P-014', 'F2P-016', 'F2P-017', 'F2P-018', 'F2P-019',
  'F2P-021', 'F2P-022', 'F2P-023', 'F2P-024'
];

const SPEC_FILES = [
  'spec/types/discriminated-union.spec.ts',
  'spec/types/recursive-schemas.spec.ts',
  'spec/modifiers/meta.spec.ts',
  'spec/modifiers/pipe.spec.ts'
];

const TEST_REQUIREMENTS = {
  'F2P-001': { file: 'spec/types/discriminated-union.spec.ts', whole: true },
  'F2P-002': { file: 'spec/types/discriminated-union.spec.ts', whole: true },
  'F2P-003': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'supports basic recursive lazy schemas with getters'
  },
  'F2P-004': { file: 'spec/modifiers/pipe.spec.ts', whole: true },
  'F2P-006': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'supports optional recursive properties with getters'
  },
  'F2P-007': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'supports recursive schemas with OpenAPI 3.0 using getters'
  },
  'F2P-008': {
    file: 'spec/modifiers/meta.spec.ts',
    title: 'should use the refId from .openapi as priority over the one in .meta'
  },
  'F2P-009': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'supports recursive schemas with OpenAPI 3.1 using getters'
  },
  'F2P-012': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'supports recursive schemas in arrays with getters'
  },
  'F2P-013': { file: 'spec/types/discriminated-union.spec.ts', whole: true },
  'F2P-014': { file: 'spec/types/discriminated-union.spec.ts', whole: true },
  'F2P-015': {
    file: 'spec/types/discriminated-union.spec.ts',
    title: 'creates a discriminator mapping when all objects in the discriminated union contain a registered schema'
  },
  'F2P-016': { file: 'spec/types/discriminated-union.spec.ts', whole: true },
  'F2P-018': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'supports deeply nested recursive tree structures with getters'
  },
  'F2P-019': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'handles deep recursive nesting without stack overflow using getters'
  },
  'F2P-022': {
    file: 'spec/types/discriminated-union.spec.ts',
    title: 'does not create a discriminator mapping when only some objects in the discriminated union contain a registered schema'
  },
  'F2P-023': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'supports recursive schemas with OpenAPI metadata using getters'
  },
  'F2P-024': {
    file: 'spec/types/recursive-schemas.spec.ts',
    title: 'supports recursive schemas in intersections with getters'
  }
};

const REQUIREMENTS = {
  'F2P-001': ['R032'],
  'F2P-002': ['R032'],
  'F2P-003': ['R032'],
  'F2P-004': ['R032'],
  'F2P-005': ['R042'],
  'F2P-006': ['R032'],
  'F2P-007': ['R032'],
  'F2P-008': ['R032'],
  'F2P-009': ['R032'],
  'F2P-010': ['R046'],
  'F2P-011': ['R003'],
  'F2P-012': ['R032'],
  'F2P-013': ['R032'],
  'F2P-014': ['R032'],
  'F2P-015': ['R032'],
  'F2P-016': ['R032'],
  'F2P-017': ['R032'],
  'F2P-018': ['R032'],
  'F2P-019': ['R032'],
  'F2P-020': ['R045'],
  'F2P-021': ['R032'],
  'F2P-022': ['R032'],
  'F2P-023': ['R032'],
  'F2P-024': ['R032']
};

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function tail(text, limit) {
  const value = String(text || '');
  return value.length > limit ? value.slice(value.length - limit) : value;
}

function localBin(ctx, name) {
  const candidates = [
    path.join(ctx.depsDir, '.bin', name),
    path.join(ctx.repoRoot, 'node_modules', '.bin', name)
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

function runCommand(ctx, command, args, timeout) {
  return childProcess.spawnSync(command, args, {
    cwd: ctx.repoRoot,
    encoding: 'utf8',
    timeout,
    maxBuffer: 32 * 1024 * 1024,
    env: Object.assign({}, process.env, {
      CI: '1',
      NODE_PATH: [ctx.depsDir, path.join(ctx.repoRoot, 'node_modules')].join(path.delimiter)
    })
  });
}

function ensureBuilt(ctx) {
  const cjs = path.join(ctx.repoRoot, 'dist', 'index.cjs');
  const mjs = path.join(ctx.repoRoot, 'dist', 'index.mjs');
  if (fs.existsSync(cjs) || fs.existsSync(mjs)) {
    return;
  }
  const rollup = localBin(ctx, 'rollup');
  const result = rollup
    ? runCommand(ctx, rollup, ['-c'], 300000)
    : runCommand(ctx, 'npm', ['run', 'build'], 300000);
  if (result.error || (result.status !== 0 && !fs.existsSync(cjs) && !fs.existsSync(mjs))) {
    throw new Error(
      'build failed before F2P-0001 probes: ' +
        tail((result.error && result.error.message) || result.stderr || result.stdout, 2000)
    );
  }
}

function runJest(ctx) {
  const jest = localBin(ctx, 'jest');
  if (!jest) {
    return { ok: false, error: 'jest binary not found', tests: new Map() };
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'f2p-0001-jest-'));
  const output = path.join(tmp, 'results.json');
  const result = runCommand(
    ctx,
    jest,
    [
      '--runInBand',
      '--ci',
      '--silent',
      '--json',
      '--outputFile=' + output,
      '--runTestsByPath',
      ...SPEC_FILES.map(file => path.join(ctx.repoRoot, file))
    ],
    600000
  );
  let report = null;
  try {
    report = readJson(output);
  } catch (error) {
    return {
      ok: false,
      error: 'jest report unavailable: ' + tail((result.stderr || result.stdout || error.message), 2000),
      tests: new Map()
    };
  }
  const tests = new Map();
  let failed = 0;
  for (const suite of report.testResults || []) {
    const spec = path
      .relative(ctx.repoRoot, suite.name)
      .split(path.sep)
      .join('/');
    for (const assertion of suite.assertionResults || []) {
      tests.set(spec + '::' + assertion.fullName, assertion.status);
      if (assertion.status === 'failed') {
        failed += 1;
      }
    }
  }
  return {
    ok: report.success === true && failed === 0,
    error: report.success === true && failed === 0 ? null : 'one or more Jest assertions failed',
    tests
  };
}

function runTsd(ctx) {
  const tsd = localBin(ctx, 'tsd');
  if (!tsd) {
    return { ok: false, error: 'tsd binary not found' };
  }
  const typings = path.relative(ctx.repoRoot, path.join(ctx.repoRoot, 'dist', 'index.d.ts')) || 'index.d.ts';
  const result = runCommand(
    ctx,
    tsd,
    ['--files', 'spec/type-definitions/zod-extensions.test-d.ts', '--typings', typings],
    300000
  );
  return {
    ok: result.status === 0,
    error: result.status === 0 ? null : tail(result.stderr || result.stdout, 2000)
  };
}

function resetRegistries(ctx) {
  try {
    if (ctx.z && ctx.z.globalRegistry && typeof ctx.z.globalRegistry.clear === 'function') {
      ctx.z.globalRegistry.clear();
    }
  } catch (error) {
    void error;
  }
  try {
    if (ctx.lib && ctx.lib.zodToOpenAPIRegistry && typeof ctx.lib.zodToOpenAPIRegistry.clear === 'function') {
      ctx.lib.zodToOpenAPIRegistry.clear();
    }
  } catch (error) {
    void error;
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

function deepEqual(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function componentSchemas(ctx, Generator, schemas) {
  return new Generator(schemas).generateComponents().components.schemas;
}

function objectExtensionMetadataScenario(ctx) {
  const z = ctx.z;
  const base = z.object({ a: z.string() }).openapi('ExtensionBase', {
    description: 'base metadata',
    deprecated: true
  });
  // Preserve the pre-migration extension contract. safeExtend is new in Zod
  // 4.1; adding OpenAPI inheritance to it is not required by instruction.md.
  const extended = base.extend({ b: z.number() });
  assert(typeof extended.extend === 'function', 'extend is not callable on the extended schema');
  assert(deepEqual(extended.parse({ a: 'x', b: 1 }), { a: 'x', b: 1 }), 'extend parsed the wrong shape');
  const named = extended.openapi('ExtensionExtended');
  const schemas = componentSchemas(ctx, ctx.lib.OpenApiGeneratorV3, [named]);
  const output = schemas.ExtensionExtended;
  assert(output && Array.isArray(output.allOf), 'extended schema did not reference its base with allOf');
  assert(output.allOf[0] && output.allOf[0].$ref === '#/components/schemas/ExtensionBase', 'base $ref was not preserved');
  assert(output.description === 'base metadata', 'description metadata was not inherited through extend');
  assert(output.deprecated === true, 'deprecated metadata was not inherited through extend');
}

function plainSchemaClassificationScenario(ctx) {
  const z = ctx.z;
  for (const name of ['OpenApiGeneratorV3', 'OpenApiGeneratorV31', 'OpenApiGeneratorV32']) {
    const schema = z.string().openapi('Plain' + name);
    const schemas = componentSchemas(ctx, ctx.lib[name], [schema]);
    assert(deepEqual(schemas['Plain' + name], { type: 'string' }), name + ' did not classify a plain Zod schema');
  }
  const registry = new ctx.lib.OpenAPIRegistry();
  registry.register('RegisteredPlain', z.object({ value: z.string() }));
  const ordered = new ctx.lib.OpenApiGeneratorV3(registry.definitions).generateComponents().components.schemas;
  assert(ordered.RegisteredPlain, 'plain schema was not generated from a registry definition');
}

function peerDependencyScenario(ctx) {
  const pkg = readJson(path.join(ctx.repoRoot, 'package.json'));
  const peer = pkg.peerDependencies && pkg.peerDependencies.zod;
  assert(typeof peer === 'string' && peer.length > 0, 'package.json has no zod peer dependency range');
  const semverRoot = path.join(ctx.depsDir, 'semver');
  let semver = null;
  try {
    semver = require(semverRoot);
  } catch (error) {
    void error;
  }
  if (semver && typeof semver.satisfies === 'function') {
    assert(semver.satisfies('4.1.0', peer), 'peer dependency range ' + JSON.stringify(peer) + ' excludes 4.1.0');
  } else {
    assert(peer === '^4.0.0' || peer === '^4.1.0' || peer.includes('>=4.1.0'), 'peer dependency range does not include 4.1.0');
  }
}

function titleMetadataScenario(ctx) {
  const z = ctx.z;
  const schema = z.string().meta({ id: 'TitleMetadata', title: 'Title description' });
  const schemas = componentSchemas(ctx, ctx.lib.OpenApiGeneratorV3, [schema]);
  assert(schemas.TitleMetadata, '.meta({ id }) did not register a component');
  assert(schemas.TitleMetadata.description === 'Title description', '.meta({ title }) was not mapped to description');
}

function modifierInheritanceScenario(ctx) {
  const z = ctx.z;
  const withDefault = z.string().default('x').openapi('ModifierDefault');
  const defaultSchemas = componentSchemas(ctx, ctx.lib.OpenApiGeneratorV3, [withDefault]);
  assert(
    deepEqual(defaultSchemas.ModifierDefault, { type: 'string', default: 'x' }),
    'default modifier inheritance changed: ' + JSON.stringify(defaultSchemas.ModifierDefault)
  );

  const piped = z.string().pipe(z.string().min(1)).openapi('ModifierPipe');
  const pipeSchemas = componentSchemas(ctx, ctx.lib.OpenApiGeneratorV3, [piped]);
  assert(
    deepEqual(pipeSchemas.ModifierPipe, { type: 'string' }),
    'pipe modifier inheritance changed: ' + JSON.stringify(pipeSchemas.ModifierPipe)
  );

  const lazy = z.lazy(() => z.string()).openapi('ModifierLazy');
  const lazySchemas = componentSchemas(ctx, ctx.lib.OpenApiGeneratorV3, [lazy]);
  assert(
    deepEqual(lazySchemas.ModifierLazy, { type: 'string' }),
    'lazy modifier inheritance changed: ' + JSON.stringify(lazySchemas.ModifierLazy)
  );
}

function consoleErrorScenario(ctx) {
  const z = ctx.z;
  const A = z.object({ kind: z.literal('a') });
  const B = z.object({ kind: z.literal('b') });
  const union = z.discriminatedUnion('kind', [A, B]);
  union._zod.def.discriminator = undefined;
  const schema = union.openapi('NoDiscriminator');
  const original = console.error;
  const calls = [];
  console.error = function (...args) {
    calls.push(args);
  };
  let components;
  try {
    components = ctx.lib.OpenApiGeneratorV3
      ? componentSchemas(ctx, ctx.lib.OpenApiGeneratorV3, [schema])
      : {};
  } finally {
    console.error = original;
  }
  assert(calls.length > 0, 'console.error was not triggered for a discriminated union without a discriminator');
  assert(
    String(calls[0][0]) === 'No discriminator found for discriminated union',
    'console.error message changed: ' + String(calls[0][0])
  );
  assert(components.NoDiscriminator && Array.isArray(components.NoDiscriminator.oneOf), 'oneOf output was not produced');
}

const PROBES = {
  'F2P-005': objectExtensionMetadataScenario,
  'F2P-010': plainSchemaClassificationScenario,
  'F2P-011': peerDependencyScenario,
  'F2P-017': consoleErrorScenario,
  'F2P-020': titleMetadataScenario,
  'F2P-021': modifierInheritanceScenario
};

function buildContext(options) {
  const repoRoot = path.resolve(options.repoRoot);
  const depsDir = path.resolve(options.depsDir || path.join(repoRoot, 'node_modules'));
  const zodRoot = path.join(repoRoot, 'node_modules', 'zod');
  const depsZodRoot = path.join(depsDir, 'zod');
  const zodPackagePath = fs.existsSync(path.join(zodRoot, 'package.json'))
    ? path.join(zodRoot, 'package.json')
    : path.join(depsZodRoot, 'package.json');
  const zodVersion = fs.existsSync(zodPackagePath) ? readJson(zodPackagePath).version : 'unknown';
  const ctx = { repoRoot, depsDir, zodVersion, technology: options.technology };
  if (!options.technology || !options.technology.satisfied) {
    return ctx;
  }
  ensureBuilt(ctx);
  const cjs = path.join(repoRoot, 'dist', 'index.cjs');
  const mjs = path.join(repoRoot, 'dist', 'index.mjs');
  const required = require('node:module').createRequire(path.join(repoRoot, 'package.json'));
  ctx.z = required(fs.existsSync(path.join(zodRoot, 'package.json')) ? zodRoot : depsZodRoot);
  ctx.lib = fs.existsSync(cjs) ? required(cjs) : null;
  if (!ctx.lib) {
    throw new Error('built CommonJS entry point dist/index.cjs was not produced');
  }
  ctx.lib.extendZodWithOpenApi(ctx.z);
  return ctx;
}

function evaluateObservation(ctx, id, jest, tsd) {
  const requirement = TEST_REQUIREMENTS[id];
  if (PROBES[id]) {
    try {
      PROBES[id](ctx);
      return { status: 'PASS', value: { probe: id, requirement_ids: REQUIREMENTS[id] } };
    } catch (error) {
      return {
        status: 'FAIL',
        value: {
          probe: id,
          requirement_ids: REQUIREMENTS[id],
          error: String((error && error.message) || error)
        }
      };
    }
  }
  if (!requirement) {
    return { status: 'FAIL', value: { probe: id, error: 'no probe registered for observation' } };
  }
  if (!jest.ok) {
    return {
      status: 'FAIL',
      value: { probe: id, requirement_ids: REQUIREMENTS[id], error: jest.error }
    };
  }
  if (!tsd.ok) {
    return {
      status: 'FAIL',
      value: { probe: id, requirement_ids: REQUIREMENTS[id], error: tsd.error }
    };
  }
  if (requirement.whole) {
    const prefix = requirement.file + '::';
    const results = [...jest.tests.entries()].filter(([key]) => key.startsWith(prefix));
    const failed = results.filter(([, status]) => status === 'failed');
    if (results.length === 0) {
      return {
        status: 'FAIL',
        value: { probe: id, requirement_ids: REQUIREMENTS[id], error: 'no tests found for ' + requirement.file }
      };
    }
    if (failed.length > 0) {
      return {
        status: 'FAIL',
        value: {
          probe: id,
          requirement_ids: REQUIREMENTS[id],
          error: 'failing tests: ' + failed.map(([key]) => key.slice(prefix.length)).join('; ')
        }
      };
    }
    return {
      status: 'PASS',
      value: { probe: id, requirement_ids: REQUIREMENTS[id], spec: requirement.file, tests: results.length }
    };
  }
  const matches = [...jest.tests.entries()].filter(([key]) => {
    if (!key.startsWith(requirement.file + '::')) {
      return false;
    }
    const fullName = key.slice((requirement.file + '::').length);
    return fullName === requirement.title || fullName.endsWith(' ' + requirement.title);
  });
  if (matches.length === 0) {
    return {
      status: 'FAIL',
      value: {
        probe: id,
        requirement_ids: REQUIREMENTS[id],
        error: 'test not found: ' + requirement.file + ' :: ' + requirement.title
      }
    };
  }
  const failed = matches.filter(([, status]) => status === 'failed');
  if (failed.length > 0) {
    return {
      status: 'FAIL',
      value: {
        probe: id,
        requirement_ids: REQUIREMENTS[id],
        error: 'test failed: ' + requirement.file + ' :: ' + requirement.title
      }
    };
  }
  return {
    status: 'PASS',
    value: {
      probe: id,
      requirement_ids: REQUIREMENTS[id],
      spec: requirement.file,
      test: requirement.title
    }
  };
}

function run(options) {
  const observations = [];
  let ctx;
  try {
    ctx = buildContext(options);
  } catch (error) {
    for (const id of OBSERVATION_IDS) {
      observations.push({
        id,
        status: 'INFRA_ERROR',
        value: { probe: id, requirement_ids: REQUIREMENTS[id], error: String((error && error.message) || error) }
      });
    }
    return observations;
  }

  if (!options.technology || !options.technology.satisfied) {
    const reason =
      'required target technology Zod 4.1.0 is not loaded; loaded Zod ' + ctx.zodVersion;
    for (const id of OBSERVATION_IDS) {
      observations.push({
        id,
        status: 'FAIL',
        value: { probe: id, requirement_ids: REQUIREMENTS[id], error: reason }
      });
    }
    return observations;
  }

  let jest = { ok: false, error: 'not run', tests: new Map() };
  let tsd = { ok: false, error: 'not run' };
  try {
    resetRegistries(ctx);
    jest = runJest(ctx);
    tsd = runTsd(ctx);
  } catch (error) {
    jest = { ok: false, error: String((error && error.message) || error), tests: new Map() };
    tsd = { ok: false, error: String((error && error.message) || error) };
  }

  for (const id of OBSERVATION_IDS) {
    resetRegistries(ctx);
    const result = evaluateObservation(ctx, id, jest, tsd);
    observations.push({ id, status: result.status, value: result.value });
  }
  return observations;
}

module.exports = {
  BATCH_ID,
  OBSERVATION_IDS,
  REQUIREMENTS,
  run
};
