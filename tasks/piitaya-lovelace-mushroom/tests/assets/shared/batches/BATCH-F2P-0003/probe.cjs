"use strict";

// Shared probe engine for BATCH-F2P-0003.
//
// Runs the same semantic predicate for every F2P observation against a
// mushroom-cards repository root. The engine is side-agnostic: the source
// runner points it at the Superstruct baseline, the target runner points it at
// the migrated repository. It never hardcodes PASS/FAIL by role; it reports the
// raw predicate result for the repository it is given.
//
// The engine loads the repository TypeScript modules directly with a
// transpile-on-require hook and evaluates the exported schemas and
// handleStructError through their public runtime surface.

const fs = require("fs");
const path = require("path");
const Module = require("module");
const { createValidationResult, createHaBarrelStub, unionReportSkipsAlternatives } = require("../../integration/config-validation.cjs");

const DEPS = process.env.MUSHROOM_DEPS || "/opt/migration/dependencies/node_modules";
const ts = require(path.join(DEPS, "typescript"));

const HASS_LOCALIZE = (key, ...args) => `${key}(${args.join("|")})`;

function installTsLoader() {
  Module._extensions[".ts"] = function (module, filename) {
    const source = fs.readFileSync(filename, "utf8");
    const output = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2019,
        esModuleInterop: true,
        moduleResolution: ts.ModuleResolutionKind.Node10,
      },
    }).outputText;
    module._compile(output, filename);
  };
}

function installModuleInterception(repoRoot, loaded) {
  const originalLoad = Module._load;
  let barrelStub = null;

  const litStub = {
    html: () => ({}),
    css: () => ({}),
    svg: () => ({}),
    nothing: Symbol("nothing"),
    noChange: Symbol("noChange"),
    LitElement: class {},
  };

  const makeBarrelStub = () => {
    const actionStructPath = path.join(
      repoRoot,
      "src/ha/panels/lovelace/editor/structs/action-struct.ts"
    );
    return createHaBarrelStub(repoRoot, ts, {
      get actionConfigStruct() {
        return require(actionStructPath).actionConfigStruct;
      },
      get actionConfigStructType() {
        return require(actionStructPath).actionConfigStructType;
      },
      getEntityPicture: () => "",
      isAvailable: () => false,
      isUnknown: () => false,
      computeDomain: (value) => String(value).split(".")[0],
    });
  };

  Module._load = function (request, parent, isMain) {
    loaded.push(request);
    if (request === "lit" || request.startsWith("lit/")) {
      return litStub;
    }
    if (parent && request.startsWith(".")) {
      const candidate = path.resolve(path.dirname(parent.filename), request);
      const barrel = path.join(repoRoot, "src/ha");
      if (
        candidate === barrel ||
        candidate === path.join(barrel, "index") ||
        candidate === path.join(barrel, "index.ts")
      ) {
        if (!barrelStub) {
          barrelStub = makeBarrelStub();
        }
        return barrelStub;
      }
    }
    return originalLoad.apply(this, arguments);
  };
}

function loadModules(repoRoot) {
  const modules = {
    lovelaceCardConfig: "src/shared/config/lovelace-card-config.ts",
    lovelaceBadgeConfig: "src/shared/config/lovelace-badge-config.ts",
    layout: "src/utils/layout.ts",
    entityConfig: "src/shared/config/entity-config.ts",
    appearanceConfig: "src/shared/config/appearance-config.ts",
    actionsConfig: "src/shared/config/actions-config.ts",
    actionStruct: "src/ha/panels/lovelace/editor/structs/action-struct.ts",
    entityCardConfig: "src/cards/entity-card/entity-card-config.ts",
    templateBadgeConfig: "src/badges/template/template-badge-config.ts",
    handleErrors: "src/ha/common/structs/handle-errors.ts",
  };
  const loaded = {};
  for (const [key, relative] of Object.entries(modules)) {
    try {
      loaded[key] = require(path.join(repoRoot, relative));
    } catch (error) {
      loaded[key] = { __loadError: String((error && error.stack) || error) };
    }
  }
  return loaded;
}

function zodError(schema, input, validateConfig) {
  if (!schema || typeof schema.safeParse !== "function") {
    return null;
  }
  const result = validateConfig(schema, input);
  if (result.success) {
    return null;
  }
  const error = result.error;
  if (error && typeof error === "object" && Array.isArray(error.issues)) {
    return error;
  }
  return null;
}

const isArray = (value) => Array.isArray(value);

function evaluate(repoRoot) {
  const loadedRequests = [];
  installTsLoader();
  installModuleInterception(repoRoot, loadedRequests);
  const modules = loadModules(repoRoot);
  const hass = { localize: HASS_LOCALIZE };
  const validateConfig = createValidationResult(repoRoot, modules.handleErrors, ts);
  modules.handleErrors = { ...modules.handleErrors, handleStructError: validateConfig.handleStructError };
  const configError = (schema, input) => zodError(schema, input, validateConfig);

  const entityCardStruct = () => modules.entityCardConfig.entityCardConfigStruct;
  const boundCard = {
    type: "custom:mushroom-entity-card",
    entity: "light.kitchen",
  };

  const checks = {};
  const define = (id, predicate) => {
    try {
      checks[id] = predicate() === true ? "PASS" : "FAIL";
    } catch (error) {
      checks[id] = "FAIL";
      checks[`${id}__error`] = String((error && error.message) || error);
    }
  };

  const missingTypeIssue = () => {
    const error = configError(modules.lovelaceCardConfig.lovelaceCardConfigStruct, {});
    if (!error) return false;
    return error.issues.some(
      (issue) =>
        issue.code === "invalid_type" &&
        issue.received === "undefined" &&
        issue.path.join(".") === "type"
    );
  };

  const enumSurface = () => {
    const schema = modules.appearanceConfig.appearanceSharedConfigStruct;
    if (!schema || typeof schema.safeParse !== "function") return false;
    const positive = validateConfig(schema, { primary_info: "name" });
    const negative = validateConfig(schema, { primary_info: "bogus" });
    if (positive.success !== true || negative.success) return false;
    const error = negative.error;
    if (!error || !isArray(error.issues)) return false;
    if (!error.issues.some((issue) => issue.code === "invalid_enum_value")) {
      return false;
    }
    const output = modules.handleErrors.handleStructError(hass, error, {
      primary_info: "bogus",
    });
    return (
      isArray(output.warnings) &&
      output.warnings.some(
        (message) =>
          message.includes("ui.errors.config.key_wrong_type") &&
          message.includes("primary_info") && message.includes("bogus") &&
          ["name", "state", "last-changed", "last-updated", "none"].every(
            (option) => message.includes(option)
          )
      )
    );
  };

  const unionSkipSurface = () => {
    const error = configError(modules.entityConfig.entityNameStruct, 7);
    if (!error) return false;
    return unionReportSkipsAlternatives(modules.handleErrors.handleStructError, error, 7);
  };

  // The instruction preserves decisions for the same configuration input; it
  // does not restrict inputs to JSON-parsed plain objects. Superstruct's
  // object()/type() accept non-array objects, read declared inherited fields,
  // and check only own enumerable keys for unexpected fields. Native Zod
  // objects differ for Date/Map/Set and inherited extra keys.
  const objectInputSemantics = () => {
    const schemas = [
      [modules.entityConfig.entitySharedConfigStruct, {}],
      [modules.appearanceConfig.appearanceSharedConfigStruct, {}],
      [modules.actionsConfig.actionsSharedConfigStruct, {}],
      [modules.lovelaceCardConfig.lovelaceCardConfigStruct, { type: "x" }],
      [modules.lovelaceBadgeConfig.lovelaceBadgeConfigStruct, { type: "x" }],
      [entityCardStruct(), boundCard],
      [modules.templateBadgeConfig.templateBadgeConfigStruct, { type: "x" }],
    ];
    const factories = [
      () => ({}),
      () => Object.create(null),
      () => new Date(0),
      () => new Map(),
      () => new Set(),
      () => Promise.resolve(),
      () => Object.create({ inherited_extra: true }),
    ];
    return schemas.every(([schema, seed]) => {
      if (!schema || typeof schema.safeParse !== "function") return false;
      if (!factories.every((factory) =>
        validateConfig(schema, Object.assign(factory(), seed)).success
      )) return false;
      // Required and optional declared fields may themselves be inherited.
      if (!validateConfig(schema, Object.create(seed)).success) return false;
      return [null, [], 7, { ...seed, own_extra: true }].every(
        (input) => !validateConfig(schema, input).success
      );
    });
  };

  const actionObjectSemantics = () => {
    const schema = modules.actionStruct.actionConfigStruct;
    if (!schema || typeof schema.safeParse !== "function") return false;
    for (const action of ["call-service", "perform-action"]) {
      for (const value of [
        {}, new Date(0), new Map(), new Set(), Promise.resolve(),
        { then() {}, catch() {} },
      ]) {
        if (!validateConfig(schema, {
          action, service_data: value, data: value,
        }).success) return false;
      }
      for (const value of [null, [], 7, "data"]) {
        if (validateConfig(schema, { action, service_data: value }).success ||
            validateConfig(schema, { action, data: value }).success) return false;
      }
    }
    return true;
  };

  const extraKeySurface = () => {
    const schema = entityCardStruct();
    if (!schema || typeof schema.safeParse !== "function") return false;
    const bad = validateConfig(schema, {
      type: "custom:mushroom-entity-card",
      bogus: 1,
    });
    const good = validateConfig(schema, {
      type: "custom:mushroom-entity-card",
      entity: "light.kitchen",
      layout: "horizontal",
    });
    return (
      bad.success === false &&
      bad.error &&
      isArray(bad.error.issues) &&
      bad.error.issues.some(
        (issue) =>
          issue.code === "unrecognized_keys" &&
          isArray(issue.keys) &&
          issue.keys.includes("bogus")
      ) &&
      good.success === true &&
      objectInputSemantics()
    );
  };

  const zodErrorMessageSurface = () => {
    const schema = entityCardStruct();
    if (!schema || typeof schema.safeParse !== "function") return false;
    const result = validateConfig(schema,
      Object.assign({}, boundCard, { entity: 42 })
    );
    if (result.success || !result.error || !isArray(result.error.issues)) {
      return false;
    }
    const output = modules.handleErrors.handleStructError(
      hass,
      result.error,
      Object.assign({}, boundCard, { entity: 42 })
    );
    return (
      isArray(output.warnings) &&
      output.warnings.some((message) => message.includes("ui.errors.config."))
    );
  };

  // F2P-049 / F2P-066 / F2P-071: missing required key -> Invalid_type(undefined).
  define("F2P-049", missingTypeIssue);
  define("F2P-066", missingTypeIssue);
  define("F2P-071", missingTypeIssue);

  // F2P-053 / F2P-051 / F2P-052: ZodError detected and localized.
  define("F2P-053", zodErrorMessageSurface);
  define("F2P-051", zodErrorMessageSurface);
  define("F2P-052", () => {
    const schema = entityCardStruct();
    if (!schema || typeof schema.safeParse !== "function") return false;
    const good = validateConfig(schema, boundCard);
    if (good.success !== true) return false;
    return zodErrorMessageSurface();
  });

  // F2P-054: offending value recovered from the original input.
  define("F2P-054", () => {
    const input = { primary_info: 123 };
    const error = configError(
      modules.appearanceConfig.appearanceSharedConfigStruct,
      input
    );
    if (!error) return false;
    const output = modules.handleErrors.handleStructError(hass, error, input);
    return (
      isArray(output.warnings) &&
      output.warnings.some(
        (message) => message.includes("primary_info") && message.includes("123")
      )
    );
  });

  // F2P-063: unexpected key -> key_not_expected.
  define("F2P-063", () => {
    const input = { entity: "light.kitchen", bogus: 1 };
    const error = configError(modules.entityConfig.entitySharedConfigStruct, input);
    if (!error) return false;
    const output = modules.handleErrors.handleStructError(hass, error, input);
    return (
      isArray(output.warnings) &&
      output.warnings.some(
        (message) =>
          message.includes("key_not_expected") && message.includes("bogus")
      )
    );
  });

  // F2P-058: missing key -> key_missing.
  define("F2P-058", () => {
    const error = configError(modules.lovelaceCardConfig.lovelaceCardConfigStruct, {});
    if (!error) return false;
    const output = modules.handleErrors.handleStructError(hass, error, {});
    return (
      isArray(output.errors) &&
      output.errors.some(
        (message) => message.includes("key_missing") && message.includes("type")
      )
    );
  });

  // F2P-056: wrong type warning includes the offending value.
  define("F2P-056", () => {
    const input = Object.assign({}, boundCard, { entity: 42 });
    const error = configError(entityCardStruct(), input);
    if (!error) return false;
    const output = modules.handleErrors.handleStructError(hass, error, input);
    return (
      isArray(output.warnings) &&
      output.warnings.some(
        (message) =>
          message.includes("key_wrong_type") && message.includes("42")
      )
    );
  });

  // F2P-050 / F2P-059 / F2P-062: non-validation error passthrough AND the
  // migrated validation error type is still localized (the discriminative
  // conjunction: the source guard passes the plain error but fails the
  // validation error).
  const nonValidationEvent = () => {
    const output = modules.handleErrors.handleStructError(
      hass,
      new TypeError("boom")
    );
    return (
      output &&
      output.errors === undefined &&
      isArray(output.warnings) &&
      output.warnings.length === 1 &&
      output.warnings[0] === "boom"
    );
  };
  const nonValidationAndZod = () => {
    if (!nonValidationEvent()) return false;
    return zodErrorMessageSurface();
  };
  define("F2P-050", nonValidationAndZod);
  define("F2P-059", nonValidationAndZod);
  define("F2P-062", nonValidationAndZod);

  // F2P-057 / F2P-061: union alternatives are skipped.
  define("F2P-057", unionSkipSurface);
  define("F2P-061", unionSkipSurface);

  // F2P-060 / F2P-072: enum rejection formatted with candidate options.
  define("F2P-060", enumSurface);
  define("F2P-072", enumSurface);

  // F2P-065 / F2P-067 / F2P-069 / F2P-070: strict composing schema.
  define("F2P-065", extraKeySurface);
  define("F2P-067", extraKeySurface);
  define("F2P-069", extraKeySurface);
  define("F2P-070", extraKeySurface);

  // F2P-068: dynamic action dispatch keeps per-action requirements.
  define("F2P-068", () => {
    const schema = modules.actionStruct.actionConfigStruct;
    if (!schema || typeof schema.safeParse !== "function") return false;
    const url = validateConfig(schema, { action: "url" });
    const none = validateConfig(schema, { action: "none" });
    return url.success === false && none.success === true && actionObjectSemantics();
  });

  // F2P-055 / F2P-064: target technology executes in-process and the source
  // technology is not loaded by the migrated modules.
  const dependencyPolicySurface = () => {
    const schema = entityCardStruct();
    if (!schema || typeof schema.safeParse !== "function") return false;
    const result = validateConfig(schema, boundCard);
    if (result.success !== true) return false;
    return !loadedRequests.includes("superstruct");
  };
  define("F2P-055", dependencyPolicySurface);
  define("F2P-064", dependencyPolicySurface);

  // ---------------------------------------------------------------------
  // F2P-073..F2P-090: hardening observations.
  //
  // Each predicate reads only the repository's public runtime surface
  // (`safeParse` + `handleStructError`) and is side-agnostic; it never
  // hardcodes PASS/FAIL by role. Two families sit squarely inside the declared
  // behavior contract:
  //   * F2P-073..080  union-skip fidelity (Behavior 37): no alternative
  //     diagnostics; an optional summary may describe the union itself.
  //   * F2P-081..088  offending-value fidelity (Behavior 36): a
  //     `key_wrong_type` warning must carry the offending value exactly.
  // One family preserves the localized report the schemas produce today:
  //   * F2P-089..090  expected-type / candidate rendering preserved as-is.
  // ---------------------------------------------------------------------

  const unionSkipped = (schema, input, unionPath = []) => {
    const error = configError(schema, input);
    if (!error) return false;
    return unionReportSkipsAlternatives(
      modules.handleErrors.handleStructError, error, input, unionPath
    );
  };

  const wrongValueReported = (output, key, value) => {
    if (!isArray(output.warnings)) return false;
    const encoded = JSON.stringify(value);
    const escaped = encoded.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    // Preserve the value, while accepting harmless display additions such as
    // "42 (number)". A boundary check still rejects a different value (142).
    const pattern = new RegExp(`(^|[^\\w.-])${escaped}($|[^\\w.])`);
    return output.warnings.some((message) => {
      const text = String(message);
      if (!text.includes("ui.errors.config.key_wrong_type") ||
          !text.includes(`key|${key}|`)) return false;
      const marker = text.lastIndexOf("type_wrong|");
      if (marker < 0) return false;
      const reported = text.slice(marker + "type_wrong|".length, -1);
      return pattern.test(reported) || (value && typeof value === "object" &&
        reported.replace(/\s/g, "").includes(encoded));
    });
  };

  const offendingValueReported = (schema, input, key, value) => {
    const error = configError(schema, input);
    if (!error) return false;
    const output = modules.handleErrors.handleStructError(hass, error, input);
    const publicOutput = modules.handleErrors.handleStructError(hass, error);
    return wrongValueReported(output, key, value) &&
      wrongValueReported(publicOutput, key, value);
  };

  const expectedTypeReported = (schema, input, key, expectedType) => {
    const error = configError(schema, input);
    if (!error) return false;
    const output = modules.handleErrors.handleStructError(hass, error, input);
    if (!isArray(output.warnings)) return false;
    return output.warnings.some((message) =>
      String(message).includes(`key|${key}|type_correct|${expectedType}|`)
    );
  };

  const enumCandidatesReported = (schema, input, options) => {
    const error = configError(schema, input);
    if (!error) return false;
    const output = modules.handleErrors.handleStructError(hass, error, input);
    if (!isArray(output.warnings)) return false;
    return output.warnings.some((message) =>
      String(message).includes("ui.errors.config.key_wrong_type") &&
      String(message).includes("primary_info") && String(message).includes("bogus") &&
      options.every((option) => String(message).includes(option))
    );
  };

  const entityNameStruct = () => modules.entityConfig.entityNameStruct;
  const entitySharedStruct = () => modules.entityConfig.entitySharedConfigStruct;
  const layoutStruct = () => modules.layout.layoutStruct;
  const appearanceStruct = () => modules.appearanceConfig.appearanceSharedConfigStruct;
  const cardStruct = () => modules.lovelaceCardConfig.lovelaceCardConfigStruct;
  const actionStruct = () => modules.actionStruct.actionConfigStruct;

  // F2P-073..080: union-skip fidelity (Behavior 37).
  define("F2P-073", () => unionSkipped(entityNameStruct(), {}));
  define("F2P-074", () => unionSkipped(entityNameStruct(), { type: "x" }));
  define("F2P-075", () => unionSkipped(entityNameStruct(), { type: "text" }));
  define("F2P-076", () => unionSkipped(entityNameStruct(), [{ type: "x" }]));
  define("F2P-077", () => unionSkipped(entitySharedStruct(), { name: {} }, ["name"]));
  define("F2P-078", () =>
    unionSkipped(entitySharedStruct(), { name: { type: "text" } }, ["name"])
  );
  define("F2P-079", () => unionSkipped(layoutStruct(), "bogus"));
  define("F2P-080", () => unionSkipped(appearanceStruct(), { layout: "bogus" }, ["layout"]));

  // F2P-081..088: offending-value fidelity (Behavior 36).
  define("F2P-081", () =>
    offendingValueReported(entitySharedStruct(), { entity: 42 }, "entity", 42)
  );
  define("F2P-082", () =>
    offendingValueReported(entitySharedStruct(), { entity: null }, "entity", null)
  );
  define("F2P-083", () =>
    offendingValueReported(entitySharedStruct(), { icon: true }, "icon", true)
  );
  define("F2P-084", () =>
    offendingValueReported(
      entitySharedStruct(),
      { entity: { a: 1 } },
      "entity",
      { a: 1 }
    )
  );
  define("F2P-085", () =>
    offendingValueReported(cardStruct(), { type: 5 }, "type", 5)
  );
  define("F2P-086", () =>
    offendingValueReported(cardStruct(), { type: "x", index: "5" }, "index", "5")
  );
  define("F2P-087", () =>
    offendingValueReported(
      actionStruct(),
      { action: "url", url_path: 5 },
      "url_path",
      5
    )
  );
  define("F2P-088", () => {
    const input = {
      type: 5, entity: 42, bogus: 1,
      tap_action: { action: "url", url_path: 9 },
    };
    const error = configError(entityCardStruct(), input);
    if (!error) return false;
    return [
      modules.handleErrors.handleStructError(hass, error, input),
      modules.handleErrors.handleStructError(hass, error),
    ].every((output) => {
      if (!isArray(output.warnings)) return false;
      const joined = output.warnings.map((message) => String(message)).join("\n");
      return wrongValueReported(output, "entity", 42) &&
        wrongValueReported(output, "type", 5) &&
        wrongValueReported(output, "tap_action.url_path", 9) &&
        joined.includes("key_not_expected") && joined.includes("bogus");
    });
  });

  // F2P-089..090: localized report preserved from the schemas today.
  define("F2P-089", () =>
    expectedTypeReported(
      actionStruct(),
      { action: "call-service", service_data: 5 },
      "service_data",
      "object"
    )
  );
  define("F2P-090", () =>
    enumCandidatesReported(
      appearanceStruct(),
      { primary_info: "bogus" },
      ["name", "state", "last-changed", "last-updated", "none"]
    )
  );

  return {
    results: checks,
    loaded: loadedRequests.filter(
      (name) => name === "superstruct" || name === "zod"
    ),
    loadErrors: Object.entries(modules)
      .filter(([, value]) => value && value.__loadError)
      .map(([key]) => key),
  };
}

module.exports = { evaluate };

if (require.main === module) {
  const repoRoot = process.argv[2];
  if (!repoRoot) {
    process.stderr.write("usage: node probe.cjs <repoRoot>\n");
    process.exit(2);
  }
  process.stdout.write(JSON.stringify(evaluate(repoRoot), null, 2) + "\n");
}
