"use strict";

/*
 * BATCH-F2P-0001 shared semantic predicate core.
 *
 * The source and target probe drivers both import this module and execute the
 * exact same predicates against their respective repository. The predicates
 * describe target postconditions of the Superstruct 2.0.2 -> Zod 3.23.8
 * configuration-validation migration:
 *
 *   - target side: the postcondition must hold (PASS),
 *   - source side: the target postcondition must not hold (FAIL).
 *
 * No predicate checks "which side am I"; the same code runs on both sides and
 * the outcome is whatever the repository actually does.
 */

const fs = require("fs");
const path = require("path");
const { createRepo } = require("./ts-loader");
const { createValidationResult, handleValidationError, unionReportSkipsAlternatives } = require("../../integration/config-validation.cjs");

const MIGRATED_MODULES = [
  "src/shared/config/entity-config.ts",
  "src/shared/config/appearance-config.ts",
  "src/shared/config/actions-config.ts",
  "src/shared/config/lovelace-card-config.ts",
  "src/shared/config/lovelace-badge-config.ts",
  "src/utils/layout.ts",
  "src/ha/common/structs/handle-errors.ts",
  "src/ha/panels/lovelace/editor/structs/action-struct.ts",
];

function makeContext(repoRoot) {
  const repo = createRepo(repoRoot);
  const root = repo.root;

  const readFile = (rel) => fs.readFileSync(path.join(root, rel), "utf8");
  const readJSON = (rel) => JSON.parse(readFile(rel));

  const reset = () => {
    // Clear the repository's own modules (and any source-library residue) so each
    // predicate observes freshly loaded sources, but keep resolved third-party
    // dependencies (zod, typescript, ...) cached: reloading them would create a
    // second module instance and break cross-module identity checks such as
    // `err instanceof z.ZodError` in the migrated `handleStructError`.
    const modulesRoot = path.join(root, "node_modules") + path.sep;
    for (const key of Object.keys(require.cache)) {
      if (key.startsWith(modulesRoot) && !/[\\/]superstruct[\\/]/.test(key)) {
        continue;
      }
      if (key.startsWith(root) || key === repo.haStub || /[\\/]superstruct[\\/]/.test(key)) {
        delete require.cache[key];
      }
    }
  };

  const localize = (key, ...args) =>
    key + " " + args.map((a) => String(a)).join(" ");

  const hass = { localize };

  const zod = () => repo.resolveDep("zod");

  return {
    root,
    readFile,
    readJSON,
    reset,
    ts: repo.ts,
    req: (rel) => repo.req(rel),
    loadSchema: (rel) => {
      const module = repo.req(rel);
      return rel === "src/ha/common/structs/handle-errors.ts" ? {
        ...module,
        handleStructError: (...args) => handleValidationError(module.handleStructError, ...args),
      } : module;
    },
    // Load several modules from one freshly reset registry so a shared
    // helper imported by more than one of them (for example an input-tracking
    // WeakMap or an error-annotation shim) stays a single instance. Loading
    // them through separate loadSchema() calls would reset the registry in
    // between and split that helper into unrelated copies.
    loadSchemas: (rels) => {
      return rels.map((rel) => {
        const module = repo.req(rel);
        return rel === "src/ha/common/structs/handle-errors.ts" ? {
          ...module,
          handleStructError: (...args) => handleValidationError(module.handleStructError, ...args),
        } : module;
      });
    },
    hass,
    validateConfig: (schema, input) => createValidationResult(
      root, repo.req("src/ha/common/structs/handle-errors.ts"), repo.ts
    )(schema, input),
    zod,
    cacheKeys: () => Object.keys(require.cache),
  };
}

function joined(msgs) {
  return (msgs.warnings || []).concat(msgs.errors || []).join(" | ");
}

function issueByPath(result, pathArr) {
  if (!result.success) {
    return result.error.issues.find(
      (i) => JSON.stringify(i.path) === JSON.stringify(pathArr)
    );
  }
  return undefined;
}

/* Minimal semver range check for the single version this Case pins.
 *
 * The runtime contract is "the lock resolves Zod 3.23.8"; the manifest may
 * declare that version exactly or with a range that still covers it (for
 * example a caret range). Only the declaration to version 3.23.8 relation is
 * needed, so a focused comparator avoids pulling a semver dependency into the
 * probe. */
function semverTuple(value) {
  const match = String(value)
    .trim()
    .replace(/^[v=\s]+/, "")
    .match(/^(\d+)\.(\d+)\.(\d+)/);
  return match ? match.slice(1).map(Number) : null;
}

function compareTuple(a, b) {
  for (let i = 0; i < 3; i += 1) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return 0;
}

function comparatorCovers(token, target) {
  const text = token.trim();
  if (!text || text === "*" || text.toLowerCase() === "x") return true;
  const operator = /^(\^|~|>=|<=|>|<|=)/.exec(text);
  const op = operator ? operator[1] : "=";
  const rest = operator ? text.slice(op.length).trim() : text;
  const parts = rest
    .split(".")
    .map((piece) => (/^[0-9]+$/.test(piece) ? Number(piece) : null));
  if (parts.length === 0 || parts[0] === null) return false;
  const lower = [parts[0], parts.length > 1 && parts[1] !== null ? parts[1] : 0, parts.length > 2 && parts[2] !== null ? parts[2] : 0];
  if (op === ">=") return compareTuple(target, lower) >= 0;
  if (op === ">") return compareTuple(target, lower) > 0;
  if (op === "<=") return compareTuple(target, lower) <= 0;
  if (op === "<") return compareTuple(target, lower) < 0;
  if (op === "^") {
    if (lower[0] > 0) {
      return compareTuple(target, lower) >= 0 && target[0] === lower[0];
    }
    if (lower[1] > 0) {
      return compareTuple(target, lower) >= 0 && target[0] === 0 && target[1] === lower[1];
    }
    return compareTuple(target, lower) >= 0 && target[0] === 0 && target[1] === 0 && target[2] === lower[2];
  }
  if (op === "~") {
    return compareTuple(target, lower) >= 0 && target[0] === lower[0] && target[1] === lower[1];
  }
  if (parts[1] === null) return target[0] === parts[0];
  if (parts[2] === null) return target[0] === parts[0] && target[1] === parts[1];
  return compareTuple(target, lower) === 0;
}

function declarationCovers(declaration, version) {
  const target = semverTuple(version);
  if (!target || typeof declaration !== "string") return false;
  const clauses = declaration.split("||").map((part) => part.trim()).filter(Boolean);
  if (clauses.length === 0) return false;
  return clauses.some((clause) =>
    clause
      .split(/\s+/)
      .filter(Boolean)
      .every((token) => comparatorCovers(token, target))
  );
}

const predicates = {
  /* F2P-002: migrated modules use Zod directly or through local helpers. */
  dependency_imports(ctx) {
    const importsOf = (rel) => {
      const source = ctx.ts.createSourceFile(
        rel, ctx.readFile(rel), ctx.ts.ScriptTarget.Latest, true
      );
      const imports = [];
      for (const node of source.statements) {
        if (!(ctx.ts.isImportDeclaration(node) || ctx.ts.isExportDeclaration(node)) ||
            node.isTypeOnly || node.importClause?.isTypeOnly ||
            !node.moduleSpecifier || !ctx.ts.isStringLiteral(node.moduleSpecifier)) continue;
        const bindings = node.importClause?.namedBindings;
        if (bindings && ctx.ts.isNamedImports(bindings) && bindings.elements.length &&
            bindings.elements.every((item) => item.isTypeOnly)) continue;
        imports.push(node.moduleSpecifier.text);
      }
      return imports;
    };
    const reachesZod = (rel, visited = new Set()) => {
      if (visited.has(rel)) return false;
      visited.add(rel);
      return importsOf(rel).some((specifier) => {
        if (specifier === "zod" || specifier.startsWith("zod/")) return true;
        if (!specifier.startsWith(".")) return false;
        const local = path.posix.normalize(path.posix.join(path.posix.dirname(rel), specifier));
        const resolved = [local + ".ts", local + "/index.ts", local].find((candidate) =>
          candidate.startsWith("src/") && fs.existsSync(path.join(ctx.root, candidate)) &&
          fs.statSync(path.join(ctx.root, candidate)).isFile()
        );
        return resolved ? reachesZod(resolved, visited) : false;
      });
    };
    const offenders = MIGRATED_MODULES.filter((rel) => importsOf(rel).some(
      (specifier) => specifier === "superstruct" || specifier.startsWith("superstruct/")
    ));
    const missingZod = MIGRATED_MODULES.filter((rel) => !reachesZod(rel));
    return {
      pass: offenders.length === 0 && missingZod.length === 0,
      detail: `superstruct importers=${JSON.stringify(offenders)}; no Zod dependency=${JSON.stringify(missingZod)}`,
    };
  },

  /* F2P-011: package.json adds zod 3.23.8 and keeps superstruct; lock resolves zod. */
  dependency_manifest(ctx) {
    const pkg = ctx.readJSON("package.json");
    const deps = Object.assign({}, pkg.dependencies, pkg.devDependencies);
    const lock = ctx.readJSON("package-lock.json");
    const lockZod =
      (lock.packages && lock.packages["node_modules/zod"] && lock.packages["node_modules/zod"].version) ||
      (lock.dependencies && lock.dependencies.zod && lock.dependencies.zod.version);
    const pass =
      declarationCovers(deps.zod, "3.23.8") &&
      typeof deps.superstruct === "string" &&
      lockZod === "3.23.8";
    return { pass, detail: `package.json zod=${deps.zod} superstruct=${deps.superstruct}; lock zod=${lockZod}` };
  },

  /* F2P-004: entityCardConfigStruct is strict about unknown keys but accepts known fields. */
  strict_entity_card(ctx) {
    const { entityCardConfigStruct } = ctx.loadSchema(
      "src/cards/entity-card/entity-card-config.ts"
    );
    const bad = ctx.validateConfig(entityCardConfigStruct, {
      type: "custom:mushroom-entity-card",
      bogus: 1,
    });
    const good = ctx.validateConfig(entityCardConfigStruct, {
      type: "custom:mushroom-entity-card",
      entity: "light.kitchen",
    });
    const issue = bad.success
      ? undefined
      : bad.error.issues.find((i) => i.code === "unrecognized_keys");
    const pass =
      bad.success === false &&
      good.success === true &&
      !!issue &&
      (issue.keys || []).includes("bogus");
    return { pass, detail: `bad.success=${bad.success} good.success=${good.success} codes=${bad.success ? "[]" : JSON.stringify(bad.error.issues.map((i) => i.code))}` };
  },

  /* F2P-012 / F2P-014 / F2P-020: strict unknown key reported as key_not_expected. */
  unknown_key_strict(ctx) {
    const { entitySharedConfigStruct } = ctx.loadSchema(
      "src/shared/config/entity-config.ts"
    );
    const { handleStructError } = ctx.loadSchema(
      "src/ha/common/structs/handle-errors.ts"
    );
    const input = { entity: "light.kitchen", bogus: 1 };
    const result = ctx.validateConfig(entitySharedConfigStruct, input);
    const good = ctx.validateConfig(entitySharedConfigStruct, { entity: "light.kitchen" });
    const issue = result.success
      ? undefined
      : result.error.issues.find((i) => i.code === "unrecognized_keys");
    let out = { warnings: [], errors: [] };
    if (!result.success) out = handleStructError(ctx.hass, result.error, input);
    const msg = joined(out);
    const pass =
      result.success === false &&
      good.success === true &&
      !!issue &&
      (issue.keys || []).includes("bogus") &&
      msg.includes("ui.errors.config.key_not_expected") &&
      msg.includes("bogus");
    return { pass, detail: `success=${result.success} messages=${msg}` };
  },

  /* F2P-001 / F2P-003 / F2P-013: offending value recovered from the original input. */
  offending_value(ctx) {
    const { appearanceSharedConfigStruct } = ctx.loadSchema(
      "src/shared/config/appearance-config.ts"
    );
    const { handleStructError } = ctx.loadSchema(
      "src/ha/common/structs/handle-errors.ts"
    );
    const input = { primary_info: 123 };
    const result = ctx.validateConfig(appearanceSharedConfigStruct, input);
    if (result.success) return { pass: false, detail: "schema accepted the invalid input" };
    // The public two-argument handler must retain this error's value even
    // after a different input has been validated. A third argument supplied
    // by the harness must not conceal loss of the original error data.
    ctx.validateConfig(appearanceSharedConfigStruct, { primary_info: 456 });
    const withInput = joined(handleStructError(ctx.hass, result.error, input));
    const withoutInput = joined(handleStructError(ctx.hass, result.error));
    const pass =
      [withInput, withoutInput].every((message) =>
        message.includes("primary_info") && message.includes("123") &&
        !message.includes("undefined") && !message.includes("456")
      );
    return { pass, detail: `withInput="${withInput}" withoutInput="${withoutInput}"` };
  },

  /* F2P-009: enum rejection keeps candidates and received value. */
  enum_reject(ctx) {
    const { appearanceSharedConfigStruct } = ctx.loadSchema(
      "src/shared/config/appearance-config.ts"
    );
    const { handleStructError } = ctx.loadSchema(
      "src/ha/common/structs/handle-errors.ts"
    );
    const ok = ctx.validateConfig(appearanceSharedConfigStruct, { primary_info: "name" });
    const bad = ctx.validateConfig(appearanceSharedConfigStruct, { primary_info: "bogus" });
    const issue = bad.success
      ? undefined
      : bad.error.issues.find((i) => i.code === "invalid_enum_value");
    let msg = "";
    if (!bad.success) msg = joined(handleStructError(ctx.hass, bad.error, { primary_info: "bogus" }));
    const pass =
      ok.success === true &&
      bad.success === false &&
      !!issue &&
      msg.includes("one of") &&
      msg.includes("bogus");
    return { pass, detail: `ok=${ok.success} bad=${bad.success} msg="${msg}"` };
  },

  /* F2P-006: missing required key on the card struct -> key_missing. */
  key_missing(ctx) {
    const { lovelaceCardConfigStruct } = ctx.loadSchema(
      "src/shared/config/lovelace-card-config.ts"
    );
    const { handleStructError } = ctx.loadSchema(
      "src/ha/common/structs/handle-errors.ts"
    );
    const result = ctx.validateConfig(lovelaceCardConfigStruct, {});
    if (result.success) return { pass: false, detail: "schema accepted {}" };
    const issue = issueByPath(result, ["type"]);
    const out = handleStructError(ctx.hass, result.error, {});
    const msg = joined(out);
    const pass =
      !!issue &&
      issue.code === "invalid_type" &&
      issue.received === "undefined" &&
      msg.includes("ui.errors.config.key_missing") &&
      msg.includes("type");
    return { pass, detail: `issue=${JSON.stringify(issue)} msg="${msg}"` };
  },

  /* F2P-017 / F2P-024: a value failing all union branches is skipped, no per-branch entries. */
  union_skip(ctx) {
    const { entityNameStruct } = ctx.loadSchema(
      "src/shared/config/entity-config.ts"
    );
    const { handleStructError } = ctx.loadSchema(
      "src/ha/common/structs/handle-errors.ts"
    );
    const result = ctx.validateConfig(entityNameStruct, 7);
    if (result.success) return { pass: false, detail: "schema accepted 7" };
    const union = result.error.issues.find((i) => i.code === "invalid_union");
    const out = handleStructError(ctx.hass, result.error, 7);
    const pass = unionReportSkipsAlternatives(handleStructError, result.error, 7);
    return { pass, detail: `union=${!!union} warnings=${JSON.stringify(out.warnings)} errors=${JSON.stringify(out.errors)}` };
  },

  /* F2P-015 / F2P-019: actionConfigStruct dispatches on the action discriminant. */
  action_dispatch(ctx) {
    const { actionConfigStruct } = ctx.loadSchema(
      "src/ha/panels/lovelace/editor/structs/action-struct.ts"
    );
    const none = ctx.validateConfig(actionConfigStruct, { action: "none" });
    const url = ctx.validateConfig(actionConfigStruct, { action: "url" });
    const pass = none.success === true && url.success === false;
    return { pass, detail: `none.success=${none.success} url.success=${url.success}` };
  },

  /* F2P-005 / F2P-016: handleStructError recognizes a ZodError from the card struct. */
  zod_error_detection(ctx) {
    const { entityCardConfigStruct } = ctx.loadSchema(
      "src/cards/entity-card/entity-card-config.ts"
    );
    const { handleStructError } = ctx.loadSchema(
      "src/ha/common/structs/handle-errors.ts"
    );
    const input = { type: "custom:mushroom-entity-card", entity: 42 };
    const result = ctx.validateConfig(entityCardConfigStruct, input);
    if (result.success) return { pass: false, detail: "schema accepted entity:42" };
    const z = ctx.zod();
    const isZod = result.error instanceof z.ZodError;
    const out = handleStructError(ctx.hass, result.error, input);
    const pass =
      isZod &&
      Array.isArray(out.warnings) &&
      out.warnings.length > 0 &&
      Array.isArray(out.errors);
    return { pass, detail: `isZodError=${isZod} warnings=${JSON.stringify(out.warnings)} errors=${JSON.stringify(out.errors)}` };
  },

  /* F2P-022: a bound card config parses and a wrong-typed entity yields localized messages. */
  card_parse_zod(ctx) {
    const { entityCardConfigStruct } = ctx.loadSchema(
      "src/cards/entity-card/entity-card-config.ts"
    );
    const { handleStructError } = ctx.loadSchema(
      "src/ha/common/structs/handle-errors.ts"
    );
    const good = ctx.validateConfig(entityCardConfigStruct, {
      type: "custom:mushroom-entity-card",
      entity: "light.kitchen",
    });
    const input = { type: "custom:mushroom-entity-card", entity: 42 };
    const bad = ctx.validateConfig(entityCardConfigStruct, input);
    const z = ctx.zod();
    let pass = good.success === true && bad.success === false && bad.error instanceof z.ZodError;
    let msg = "";
    if (!bad.success) {
      msg = joined(handleStructError(ctx.hass, bad.error, input));
      pass = pass && msg.includes("ui.errors.config.") && msg.includes("42");
    }
    return { pass, detail: `good=${good.success} bad=${bad.success} msg="${msg}"` };
  },

  /* F2P-008 / F2P-010: non-validation errors pass through, ZodErrors do not. */
  non_validation_passthrough(ctx) {
    const { handleStructError } = ctx.loadSchema(
      "src/ha/common/structs/handle-errors.ts"
    );
    const plain = handleStructError(ctx.hass, new TypeError("boom"));
    const plainOk =
      JSON.stringify(plain.warnings) === JSON.stringify(["boom"]) &&
      plain.errors === undefined;
    const { entityCardConfigStruct } = ctx.loadSchema(
      "src/cards/entity-card/entity-card-config.ts"
    );
    const result = ctx.validateConfig(entityCardConfigStruct, {
      type: "custom:mushroom-entity-card",
      entity: 42,
    });
    if (result.success) return { pass: false, detail: "schema accepted entity:42" };
    const zodOut = handleStructError(ctx.hass, result.error, {});
    const zodOk = Array.isArray(zodOut.errors);
    const pass = plainOk && zodOk;
    return { pass, detail: `plainOk=${plainOk} zodHandled=${zodOk}` };
  },

  /* F2P-007 / F2P-021 / F2P-023: calling the migrated schema executes Zod in-process only. */
  dependency_execution(ctx) {
    ctx.reset();
    const { entityCardConfigStruct } = ctx.req(
      "src/cards/entity-card/entity-card-config.ts"
    );
    const result = ctx.validateConfig(entityCardConfigStruct, {
      type: "custom:mushroom-entity-card",
      entity: "light.kitchen",
    });
    const keys = ctx.cacheKeys();
    const superstructLoaded = keys.some((k) => /[\\/]superstruct[\\/]/.test(k));
    const zodLoaded = keys.some((k) => /[\\/]zod[\\/]/.test(k));
    const pass = result.success === true && !superstructLoaded && zodLoaded;
    return { pass, detail: `parsed=${result.success} superstructLoaded=${superstructLoaded} zodLoaded=${zodLoaded}` };
  },

  /* F2P-018: the editor validation path surfaces the offending value.
   *
   * Two mechanisms can carry data along this editor path: the editor call
   * site may forward the original value as a third argument, or the error
   * handler may recover the value from the validation error itself. Only a
   * migration that does neither (the editor drops the value and the handler
   * needs it) fails here. The public two-argument handler is checked separately
   * by offending_value; forwarding editor input does not replace that check. */
  element_editor_input(ctx) {
    const editor = ctx.readFile("src/utils/lovelace/element-editor.ts");
    const forwardsInput = /handleStructError\(\s*this\.hass\s*,\s*[A-Za-z_$][\w$]*\s*,/.test(
      editor
    );
    const [appearance, handler] = ctx.loadSchemas([
      "src/shared/config/appearance-config.ts",
      "src/ha/common/structs/handle-errors.ts",
    ]);
    const { appearanceSharedConfigStruct } = appearance;
    const { handleStructError } = handler;
    const input = { primary_info: 123 };
    const result = ctx.validateConfig(appearanceSharedConfigStruct, input);
    let runtime = false;
    if (!result.success) {
      const args = forwardsInput
        ? [ctx.hass, result.error, input]
        : [ctx.hass, result.error];
      const msg = joined(handleStructError(...args));
      runtime = msg.includes("123") && !msg.includes("undefined");
    }
    const pass = runtime;
    return { pass, detail: `forwardsInput=${forwardsInput} runtimeOffendingValue=${runtime}` };
  },

  /* F2P-001 alias used for the handle-errors offending-value observation family. */
  handle_error_value(ctx) {
    return predicates.offending_value(ctx);
  },
};

const OBSERVATIONS = {
  "F2P-001": "offending_value",
  "F2P-002": "dependency_imports",
  "F2P-003": "offending_value",
  "F2P-004": "strict_entity_card",
  "F2P-005": "zod_error_detection",
  "F2P-006": "key_missing",
  "F2P-007": "dependency_execution",
  "F2P-008": "non_validation_passthrough",
  "F2P-009": "enum_reject",
  "F2P-010": "non_validation_passthrough",
  "F2P-011": "dependency_manifest",
  "F2P-012": "unknown_key_strict",
  "F2P-013": "offending_value",
  "F2P-014": "unknown_key_strict",
  "F2P-015": "action_dispatch",
  "F2P-016": "zod_error_detection",
  "F2P-017": "union_skip",
  "F2P-018": "element_editor_input",
  "F2P-019": "action_dispatch",
  "F2P-020": "unknown_key_strict",
  "F2P-021": "dependency_execution",
  "F2P-022": "card_parse_zod",
  "F2P-023": "dependency_execution",
  "F2P-024": "union_skip",
};

const OBSERVATION_ORDER = [
  "F2P-001", "F2P-002", "F2P-004", "F2P-005", "F2P-006", "F2P-007",
  "F2P-008", "F2P-009", "F2P-011", "F2P-012", "F2P-015", "F2P-017",
  "F2P-022", "F2P-003", "F2P-010", "F2P-014", "F2P-016", "F2P-018",
  "F2P-019", "F2P-020", "F2P-024", "F2P-013", "F2P-021", "F2P-023",
];

function runAll(repoRoot) {
  const ctx = makeContext(repoRoot);
  const results = [];
  for (const id of OBSERVATION_ORDER) {
    const name = OBSERVATIONS[id];
    let status;
    let detail;
    try {
      ctx.reset();
      const outcome = predicates[name](ctx);
      status = outcome.pass ? "PASS" : "FAIL";
      detail = outcome.detail;
    } catch (err) {
      status = "FAIL";
      detail = "capability missing or not executable: " + err.message;
    }
    results.push({ observation_id: id, predicate: name, status, detail });
  }
  return { side: null, results };
}

module.exports = {
  makeContext,
  runAll,
  predicates,
  OBSERVATIONS,
  OBSERVATION_ORDER,
  MIGRATED_MODULES,
};
