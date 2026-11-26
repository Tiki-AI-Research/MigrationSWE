// Evaluator-owned compiler access. Never execute a candidate entrypoint,
// tsconfig plugin, or a compiler from its installed dependency graph.
const path = require('node:path');
let compiler;

function loadTypeScript() {
  if (compiler) return compiler;
  for (const directory of [
    process.env.MIGRATION_EVALUATOR_NODE_MODULES,
    '/opt/migration/dependencies/node_modules',
    '/app/node_modules',
  ]) {
    if (!directory) continue;
    try { return (compiler = require(path.join(directory, 'typescript'))); }
    catch { /* Try the next evaluator-owned layout. */ }
  }
  throw Error('evaluator-owned TypeScript compiler is unavailable');
}

function importedModule(ts, program, source, name = 'devalue') {
  const checker = program.getTypeChecker();
  const declaration = source?.statements.find(statement =>
    ts.isImportDeclaration(statement) && statement.moduleSpecifier.text === name);
  return declaration && checker.getSymbolAtLocation(declaration.moduleSpecifier);
}

function declarationExports(root, entry) {
  const ts = loadTypeScript();
  const filename = path.resolve(root, entry);
  // Resolve the supplied declaration entry from a real type-only import. This
  // supports both external-module barrels and dts-buddy's ambient module form.
  // A paths mapping isolates this entry from unrelated installed packages and
  // keeps each manifest-declared entry independently accountable.
  const consumer = path.join(path.resolve(root), '__evaluator_type_consumer__.mts');
  const text = 'import type * as api from "devalue"; export type Surface = typeof api;\n';
  const options = {
    strict: true, noEmit: true, types: [], target: ts.ScriptTarget.ESNext,
    module: ts.ModuleKind.NodeNext, moduleResolution: ts.ModuleResolutionKind.NodeNext,
    paths: { devalue: [filename] },
  };
  const host = ts.createCompilerHost(options);
  const getSourceFile = host.getSourceFile.bind(host);
  host.getSourceFile = (file, ...args) => file === consumer
    ? ts.createSourceFile(file, text, options.target, true) : getSourceFile(file, ...args);
  const program = ts.createProgram([filename, consumer], options, host);
  const checker = program.getTypeChecker();
  const symbol = importedModule(ts, program, program.getSourceFile(consumer));
  const diagnostics = ts.getPreEmitDiagnostics(program).map(diagnostic => ({
    code: diagnostic.code,
    message: ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n'),
  }));
  return { names: symbol ? checker.getExportsOfModule(symbol).map(value => value.name) : [], diagnostics };
}

function fragmentOpacity(ts, checker, moduleSymbol) {
  if (!moduleSymbol) return { opaque: false, reason: 'package-module-unresolved' };
  const exports = checker.getExportsOfModule(moduleSymbol);
  const unalias = symbol => symbol && symbol.flags & ts.SymbolFlags.Alias
    ? checker.getAliasedSymbol(symbol) : symbol;
  const fragment = exports.find(symbol => symbol.name === 'JavaScriptFragment');
  if (!fragment) return { opaque: false, reason: 'fragment-type-unresolved' };
  const type = checker.getDeclaredTypeOfSymbol(unalias(fragment));
  if (type.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown | ts.TypeFlags.Never)) {
    return { opaque: false, reason: 'fragment-is-not-an-opaque-value-type' };
  }
  const publicValues = new Set();
  const publicTypes = new Set();
  const libDirectory = path.dirname(ts.getDefaultLibFilePath({}));
  const publicSymbol = symbol => publicValues.has(unalias(symbol)) ||
    (symbol?.declarations ?? []).some(declaration =>
      path.dirname(declaration.getSourceFile().fileName) === libDirectory);
  function accessible(property) {
    return !(property.declarations ?? []).some(declaration =>
      ts.getCombinedModifierFlags(declaration) & (ts.ModifierFlags.Private | ts.ModifierFlags.Protected) ||
      declaration.name && ts.isPrivateIdentifier(declaration.name) ||
      declaration.name && ts.isComputedPropertyName(declaration.name) && (() => {
        const key = checker.getTypeAtLocation(declaration.name.expression);
        return key.flags & ts.TypeFlags.UniqueESSymbol && !publicSymbol(key.symbol);
      })());
  }
  function collectPublicType(value) {
    if (!value || publicTypes.has(value)) return;
    publicTypes.add(value);
    if (value.flags & ts.TypeFlags.UniqueESSymbol) {
      if (value.symbol) publicValues.add(unalias(value.symbol));
      return;
    }
    if (value.isUnion() || value.isIntersection()) {
      value.types.forEach(collectPublicType);
      return;
    }
    if (!(value.flags & ts.TypeFlags.Object)) return;
    const origin = value.target?.symbol ?? value.symbol;
    if (origin && ['Map', 'ReadonlyMap', 'Set', 'ReadonlySet', 'Promise', 'PromiseLike',
        'Iterable', 'Iterator', 'IterableIterator', 'AsyncIterable', 'AsyncIterator'].includes(origin.name) &&
        origin.declarations?.some(declaration => path.dirname(declaration.getSourceFile().fileName) === libDirectory)) {
      // Public collection iteration and promise resolution expose their value
      // types even though the accessors themselves are declared in lib.d.ts.
      checker.getTypeArguments(value).forEach(collectPublicType);
    }
    for (const index of checker.getIndexInfosOfType(value)) collectPublicType(index.type);
    for (const property of checker.getPropertiesOfType(value)) {
      if (!accessible(property)) continue;
      // Array/string library methods can recursively instantiate unbounded
      // generic result types. Numeric index types above carry array values;
      // library method signatures do not expose a package's private brand.
      if (property.declarations?.length && property.declarations.every(declaration =>
          path.dirname(declaration.getSourceFile().fileName) === libDirectory)) continue;
      const declaration = property.valueDeclaration ?? property.declarations?.[0];
      collectPublicType(declaration ? checker.getTypeOfSymbolAtLocation(property, declaration)
        : checker.getTypeOfPropertyOfType(value, property.name));
    }
    // A public zero-argument factory or constructor also exposes its result's
    // readable properties. Never traverse private constructors or callbacks
    // that cannot be called without supplying a required argument.
    for (const kind of [ts.SignatureKind.Call, ts.SignatureKind.Construct]) {
      for (const signature of checker.getSignaturesOfType(value, kind)) {
        if (signature.minArgumentCount || signature.declaration &&
            ts.getCombinedModifierFlags(signature.declaration) & (ts.ModifierFlags.Private | ts.ModifierFlags.Protected)) continue;
        if (signature.declaration && path.dirname(signature.declaration.getSourceFile().fileName) === libDirectory) continue;
        collectPublicType(checker.getReturnTypeOfSignature(signature));
      }
    }
  }
  function collectPublicValue(symbol) {
    const declarations = symbol.declarations ?? [];
    if (declarations.some(declaration => ts.isExportSpecifier(declaration) &&
      (declaration.isTypeOnly || declaration.parent.parent.isTypeOnly)) ||
      declarations.some(declaration => ts.isNamespaceExport(declaration) &&
        declaration.parent.isTypeOnly)) return;
    symbol = unalias(symbol);
    if (!symbol || !(symbol.flags & ts.SymbolFlags.Value) || publicValues.has(symbol)) return;
    publicValues.add(symbol);
    // A namespace's exported values are accessible through the public value
    // path (including nested namespaces and export aliases). They cannot serve
    // as inaccessible brands. Track symbols to terminate cyclic re-exports.
    if (symbol.flags & ts.SymbolFlags.Module) {
      for (const member of checker.getExportsOfModule(symbol)) collectPublicValue(member);
    }
    const declaration = symbol.valueDeclaration ?? symbol.declarations?.[0];
    if (declaration) collectPublicType(checker.getTypeOfSymbolAtLocation(symbol, declaration));
  }
  exports.forEach(collectPublicValue);

  // A public structural shape can be reproduced under any property name. A
  // required inaccessible symbol, private/protected member, never value or
  // private unique-symbol value prevents literal construction. Optional
  // branding alone cannot prevent forging {}. Unions must protect every arm.
  function constructible(value, seen = new Set()) {
    if (seen.has(value)) return true;
    const nested = new Set(seen).add(value);
    if (value.flags & ts.TypeFlags.Never) return false;
    if (value.flags & ts.TypeFlags.UniqueESSymbol) return publicSymbol(value.symbol);
    if (value.isUnion()) return value.types.some(item => constructible(item, nested));
    if (value.isIntersection()) return value.types.every(item => constructible(item, nested));
    if (!(value.flags & (ts.TypeFlags.Object | ts.TypeFlags.TypeParameter))) return true;
    for (const property of checker.getPropertiesOfType(value)) {
      if (property.flags & ts.SymbolFlags.Optional) continue;
      for (const declaration of property.declarations ?? []) {
        if (ts.getCombinedModifierFlags(declaration) & (ts.ModifierFlags.Private | ts.ModifierFlags.Protected)) return false;
        if (declaration.name && ts.isPrivateIdentifier(declaration.name)) return false;
        if (declaration.name && ts.isComputedPropertyName(declaration.name)) {
          const key = checker.getTypeAtLocation(declaration.name.expression);
          if (key.flags & ts.TypeFlags.UniqueESSymbol && !publicSymbol(key.symbol)) return false;
        }
      }
      const declaration = property.valueDeclaration ?? property.declarations?.[0];
      if (declaration && !constructible(checker.getTypeOfSymbolAtLocation(property, declaration), nested)) return false;
    }
    return true;
  }
  const opaque = !constructible(type);
  return { opaque, reason: opaque ? 'literal-construction-is-blocked' : 'fragment-has-a-forgeable-public-shape' };
}

module.exports = { loadTypeScript, importedModule, declarationExports, fragmentOpacity };
