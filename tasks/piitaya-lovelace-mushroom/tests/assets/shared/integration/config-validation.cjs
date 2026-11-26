"use strict";

// Exercise the validation entry used by setConfig when it supplies error data.
// The schema's own decision is still checked independently: an assertion must
// neither accept a rejected input nor reject an accepted input. No error values
// are synthesized by the harness.
const fs = require("fs");
const path = require("path");
const validationErrors = new WeakMap();

function handleValidationError(handler, hass, error, ...args) {
  return handler(hass, validationErrors.get(error) || error, ...args);
}

// The instruction forbids messages for individual union alternatives. It does
// not require an empty report: one summary at the failed union's own path is
// equivalent, provided it uses the preserved localization key and value.
function unionReportSkipsAlternatives(handler, error, input, unionPath = []) {
  const hass = { localize: (key, ...args) => JSON.stringify({ key, args }) };
  const report = handler(hass, error, input);
  if (!report || !Array.isArray(report.warnings) ||
      (report.errors !== undefined && !Array.isArray(report.errors)) ||
      (report.errors || []).length !== 0 || report.warnings.length > 1) return false;
  if (report.warnings.length === 0) return true;
  let message;
  try { message = JSON.parse(report.warnings[0]); } catch { return false; }
  if (message.key !== "ui.errors.config.key_wrong_type" ||
      !Array.isArray(message.args)) return false;
  const argument = (name) => {
    for (let index = 0; index + 1 < message.args.length; index += 2) {
      if (message.args[index] === name) return message.args[index + 1];
    }
    return undefined;
  };
  const value = unionPath.reduce((current, key) => current == null ? undefined : current[key], input);
  const encoded = JSON.stringify(value);
  const received = argument("type_wrong");
  if (encoded === undefined || typeof received !== "string") return false;
  const escaped = encoded.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const containsValue = new RegExp(`(^|[^\\w.-])${escaped}($|[^\\w.])`).test(received) ||
    (value !== null && typeof value === "object" &&
      received.replace(/\s/g, "").includes(encoded));
  const expected = argument("type_correct");
  if (typeof expected !== "string") return false;
  const samePath = (issue) => JSON.stringify(issue.path) === JSON.stringify(unionPath);
  const alternatives = new Set();
  const collect = (issues) => {
    for (const issue of issues || []) {
      if (!samePath(issue)) continue;
      if (issue.code === "invalid_union") {
        for (const branch of issue.unionErrors || []) collect(branch.issues);
      } else if (issue.code === "invalid_enum_value") {
        for (const option of issue.options) alternatives.add(String(option));
      } else if (issue.code === "invalid_type" || issue.code === "invalid_literal") {
        alternatives.add(String(issue.expected));
      }
    }
  };
  collect(error.issues);
  // A summary describes the union or all its alternatives. Selecting one
  // branch and reporting only its expected type is still a branch diagnostic.
  const summarizesUnion = /(?:^|[^a-z])union(?:$|[^a-z])/i.test(expected) ||
    (alternatives.size > 1 && [...alternatives].every((option) => expected.includes(option)));
  return argument("key") === unionPath.join(".") &&
    summarizesUnion && containsValue;
}

// Resolve a requested barrel export without evaluating the unrelated browser
// module graph. Validation helpers re-exported by the candidate must execute
// from their real source, just like the schemas; their names are not fixed.
function createHaBarrelStub(repoRoot, ts, fallbacks = {}) {
  const sources = new Map();
  const exports = new Map();
  const resolveFile = (filename, specifier) => {
    if (!specifier.startsWith(".")) return undefined;
    const base = path.resolve(path.dirname(filename), specifier);
    const root = path.resolve(repoRoot) + path.sep;
    if (!base.startsWith(root)) return undefined;
    return [base, base + ".ts", base + ".js", path.join(base, "index.ts"),
      path.join(base, "index.js")].find((file) =>
      fs.existsSync(file) && fs.statSync(file).isFile()
    );
  };
  const sourceOf = (filename) => {
    if (!sources.has(filename)) sources.set(filename, ts.createSourceFile(
      filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true
    ));
    return sources.get(filename);
  };
  const bindingNames = (name) => ts.isIdentifier(name) ? [name.text] :
    name.elements.flatMap((element) => element.name ? bindingNames(element.name) : []);
  const findExport = (filename, name, seen = new Set()) => {
    const key = filename + "\0" + name;
    if (seen.has(key)) return undefined;
    seen.add(key);
    const stars = [];
    for (const statement of sourceOf(filename).statements) {
      if (ts.isExportDeclaration(statement)) {
        if (statement.isTypeOnly) continue;
        const specifier = statement.moduleSpecifier;
        const target = specifier && ts.isStringLiteral(specifier) ?
          resolveFile(filename, specifier.text) : undefined;
        if (!statement.exportClause) {
          if (target && name !== "default") stars.push(target);
        } else if (ts.isNamedExports(statement.exportClause)) {
          for (const item of statement.exportClause.elements) {
            if (item.isTypeOnly || item.name.text !== name) continue;
            return target ? findExport(target, (item.propertyName || item.name).text, seen) :
              specifier ? undefined : { filename, name };
          }
        } else if (target && statement.exportClause.name.text === name) {
          return { filename: target, namespace: true };
        }
        continue;
      }
      if (!statement.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)) continue;
      if (ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement)) continue;
      const isDefault = statement.modifiers.some((modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword);
      const names = ts.isVariableStatement(statement) ?
        statement.declarationList.declarations.flatMap((declaration) => bindingNames(declaration.name)) :
        statement.name ? [statement.name.text] : [];
      if ((isDefault && name === "default") || (!isDefault && names.includes(name))) {
        return { filename, name };
      }
    }
    for (const target of stars) {
      const found = findExport(target, name, seen);
      if (found) return found;
    }
    return undefined;
  };
  return new Proxy(fallbacks, {
    get(target, name) {
      if (typeof name !== "string" || Object.prototype.hasOwnProperty.call(target, name)) {
        return Reflect.get(target, name);
      }
      if (!exports.has(name)) {
        const found = findExport(path.join(repoRoot, "src/ha/index.ts"), name);
        const module = found ? require(found.filename) : undefined;
        exports.set(name, found?.namespace ? module : module?.[found.name]);
      }
      return exports.get(name);
    },
  });
}

function editorAssertionBinding(repoRoot, ts) {
  const filename = path.join(repoRoot, "src/cards/entity-card/entity-card-editor.ts");
  const source = ts.createSourceFile(
    filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true
  );
  const imports = new Map();
  for (const statement of source.statements) {
    if (!ts.isImportDeclaration(statement) || statement.importClause?.isTypeOnly) continue;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const item of bindings.elements) {
      if (!item.isTypeOnly) imports.set(item.name.text, {
        name: (item.propertyName || item.name).text,
        source: statement.moduleSpecifier.text,
      });
    }
  }
  let binding;
  const visit = (node) => {
    if (ts.isMethodDeclaration(node) && node.name.getText(source) === "setConfig") {
      const parameter = node.parameters[0]?.name.getText(source);
      const expression = node.body?.statements[0]?.expression;
      if (expression && ts.isCallExpression(expression) &&
          ts.isIdentifier(expression.expression) && expression.arguments.length === 2 &&
          expression.arguments[0].getText(source) === parameter &&
          expression.arguments[1].getText(source) === "entityCardConfigStruct") {
        binding = imports.get(expression.expression.text);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return binding;
}

function editorAssertion(repoRoot, ts) {
  const binding = editorAssertionBinding(repoRoot, ts);
  if (!binding) return undefined;
  const filename = path.join(repoRoot, "src/cards/entity-card/entity-card-editor.ts");
  const modulePath = path.resolve(path.dirname(filename), binding.source);
  const barrel = path.join(repoRoot, "src/ha");
  const module = [barrel, path.join(barrel, "index"), path.join(barrel, "index.ts")].includes(modulePath) ?
    createHaBarrelStub(repoRoot, ts) :
    binding.source.startsWith(".") ? require(modulePath) : undefined;
  return typeof module?.[binding.name] === "function" ? module[binding.name] : undefined;
}

function createValidationResult(repoRoot, handler, ts, assertionOverride) {
  // Bundled probes supply the assertion from the same bundle as the schemas
  // and handler. Loading a second raw copy would split wrapper class identity.
  const assertion = assertionOverride === undefined ? editorAssertion(repoRoot, ts) : assertionOverride;
  const validate = (schema, input) => {
    const parsed = schema.safeParse(input);
    if (!assertion) return parsed;
    let thrown;
    try {
      assertion(input, schema);
    } catch (error) {
      thrown = error;
    }
    if (parsed.success) {
      if (thrown) throw thrown;
      return parsed;
    }
    // An editor may wrap the ZodError to carry the original input. Inspect the
    // real framework rejection, and hand the actual editor error to its actual
    // localization function. Do not require a particular wrapper class or name.
    const seen = new Set();
    const containsFrameworkError = (error) => {
      if (!error || typeof error !== "object" || seen.has(error)) return false;
      seen.add(error);
      if (error instanceof parsed.error.constructor) return true;
      return Object.getOwnPropertyNames(error).some((key) => {
        const property = Object.getOwnPropertyDescriptor(error, key);
        return property && "value" in property && containsFrameworkError(property.value);
      });
    };
    if (!containsFrameworkError(thrown)) {
      throw new Error("editor assertion did not throw a validation error for rejected input");
    }
    // Keep native ZodErrors intact, including candidate-provided issue data.
    if (thrown instanceof parsed.error.constructor) return { success: false, error: thrown };
    validationErrors.set(parsed.error, thrown);
    return parsed;
  };
  validate.handleStructError = (...args) => handleValidationError(handler.handleStructError, ...args);
  return validate;
}

module.exports = { createValidationResult, createHaBarrelStub, handleValidationError, editorAssertionBinding, unionReportSkipsAlternatives };
