'use strict';

// Parse candidate text only. Never import/evaluate a candidate module or use
// its compiler configuration, plugins, or module-resolution hooks.
const fs = require('fs');
const { createRequire } = require('module');
const ts = createRequire('/opt/migration/dependencies/package.json')('typescript');
const UNKNOWN = Symbol('unknown');
const capability = (kind, disabled = false) => ({ kind, disabled });
const tests = new Set(['it', 'test', 'describe', 'context', 'specify']);
const disabledTests = new Set(['xit', 'xtest', 'xdescribe', 'xcontext', 'xspecify', 'fit', 'fdescribe']);

function analyze(source, filename) {
  const file = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const options = { noLib: true, noResolve: true, target: ts.ScriptTarget.Latest };
  const host = ts.createCompilerHost(options);
  host.getSourceFile = (name) => name === filename ? file : undefined;
  host.fileExists = (name) => name === filename;
  host.readFile = (name) => name === filename ? source : undefined;
  const checker = ts.createProgram([filename], options, host).getTypeChecker();
  const memo = new Map();
  const active = new Set();
  const imports = new Set();
  let disabled = 0;

  function member(base, key) {
    if (base === UNKNOWN || typeof key !== 'string') return UNKNOWN;
    if (base && base.kind === 'test') {
      if (key === 'skip' || key === 'only' || key === 'todo') return capability('test', true);
      if (key === 'each' || key === 'bind' || key === 'call' || key === 'apply') {
        return { kind: 'testMethod', base, key };
      }
    }
    if (base && base.kind === 'testNamespace') {
      if (tests.has(key)) return capability('test');
      if (disabledTests.has(key)) return capability('test', true);
    }
    if (base && base.kind === 'module' && key === 'require') return capability('loader');
    if (base && base.kind === 'loader' && ['bind', 'call', 'apply'].includes(key)) {
      return { kind: 'loaderMethod', base, key };
    }
    if (base && base.kind === 'bufferClass' && key === 'from') return { kind: 'bufferFrom' };
    if (base && base.kind === 'stringClass' && key === 'fromCharCode') return { kind: 'charCodes' };
    if (Array.isArray(base) && ['join', 'reverse', 'concat'].includes(key)) return { kind: 'arrayMethod', base, key };
    if (typeof base === 'string' && ['concat', 'slice', 'substring', 'toString'].includes(key)) return { kind: 'stringMethod', base, key };
    if (base && base.kind === 'buffer' && key === 'toString') return { kind: 'bufferString', base };
    return UNKNOWN;
  }

  function loaderArguments(fn, args) {
    if (fn && fn.kind === 'loader') return (fn.boundArgs || []).concat(args);
    if (fn && fn.kind === 'loaderMethod') {
      if (fn.key === 'call') return (fn.base.boundArgs || []).concat(args.slice(1));
      if (fn.key === 'apply' && Array.isArray(args[1])) {
        return (fn.base.boundArgs || []).concat(args[1]);
      }
    }
    return null;
  }

  function evaluate(node, depth = 0) {
    if (!node || depth > 48) return UNKNOWN;
    if (memo.has(node)) return memo.get(node);
    if (active.has(node)) return UNKNOWN;
    active.add(node);
    let value = UNKNOWN;
    const next = (item) => evaluate(item, depth + 1);
    if (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) {
      value = ts.isNumericLiteral(node) ? Number(node.text) : node.text;
    } else if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node)
      || ts.isTypeAssertionExpression(node) || ts.isNonNullExpression(node)
      || ts.isSatisfiesExpression(node)) {
      value = next(node.expression);
    } else if (ts.isIdentifier(node)) {
      const symbol = checker.getSymbolAtLocation(node);
      const declaration = symbol && symbol.declarations && symbol.declarations[0];
      if (declaration && ts.isVariableDeclaration(declaration)) {
        value = next(declaration.initializer);
      } else if (declaration && ts.isParameter(declaration)) {
        value = next(declaration.initializer);
      } else if (declaration && ts.isBindingElement(declaration)) {
        const key = declaration.propertyName ? declaration.propertyName.text : declaration.name.text;
        const parent = declaration.parent.parent;
        if (ts.isVariableDeclaration(parent)) value = member(next(parent.initializer), key);
      } else if (declaration && ts.isImportSpecifier(declaration)) {
        const mod = declaration.parent.parent.parent.moduleSpecifier.text;
        const name = (declaration.propertyName || declaration.name).text;
        if (['@jest/globals', 'vitest', 'mocha'].includes(mod)) {
          value = tests.has(name) ? capability('test')
            : disabledTests.has(name) ? capability('test', true) : UNKNOWN;
        }
      } else if (declaration && ts.isNamespaceImport(declaration)) {
        const mod = declaration.parent.parent.moduleSpecifier.text;
        if (['@jest/globals', 'vitest', 'mocha'].includes(mod)) value = capability('testNamespace');
      } else if (!declaration) {
        if (node.text === 'require') value = capability('loader');
        else if (node.text === 'module') value = capability('module');
        else if (node.text === 'Buffer') value = capability('bufferClass');
        else if (node.text === 'String') value = capability('stringClass');
        else if (tests.has(node.text)) value = capability('test');
        else if (disabledTests.has(node.text)) value = capability('test', true);
        else if (['atob', 'decodeURIComponent', 'decodeURI'].includes(node.text)) value = capability(node.text);
      }
    } else if (ts.isArrayLiteralExpression(node)) {
      const items = node.elements.map(next);
      if (items.every((item) => item !== UNKNOWN)) value = items;
    } else if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      const left = next(node.left), right = next(node.right);
      if (['string', 'number'].includes(typeof left) && ['string', 'number'].includes(typeof right)) value = left + right;
    } else if (ts.isTemplateExpression(node)) {
      let text = node.head.text;
      for (const span of node.templateSpans) {
        const item = next(span.expression);
        if (!['string', 'number'].includes(typeof item)) { text = UNKNOWN; break; }
        text += String(item) + span.literal.text;
      }
      value = text;
    } else if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      const key = ts.isPropertyAccessExpression(node) ? node.name.text : next(node.argumentExpression);
      value = member(next(node.expression), key);
    } else if (ts.isTaggedTemplateExpression(node)) {
      const fn = next(node.tag);
      if (fn && fn.kind === 'testMethod' && fn.key === 'each') value = fn.base;
    } else if (ts.isCallExpression(node)) {
      const fn = next(node.expression);
      const args = node.arguments.map(next);
      const known = args.every((item) => item !== UNKNOWN);
      const loadedArgs = loaderArguments(fn, args);
      if (loadedArgs && typeof loadedArgs[0] === 'string') {
        if (['@jest/globals', 'vitest', 'mocha'].includes(loadedArgs[0])) value = capability('testNamespace');
      } else if (fn && fn.kind === 'testMethod') {
        value = fn.key === 'each' || fn.key === 'bind' ? fn.base : UNKNOWN;
      } else if (fn && fn.kind === 'loaderMethod' && fn.key === 'bind') {
        value = { kind: 'loader', boundArgs: (fn.base.boundArgs || []).concat(args.slice(1)) };
      } else if (known && fn && fn.kind === 'arrayMethod') {
        if (fn.key === 'join' && (args.length === 0 || typeof args[0] === 'string')) value = fn.base.join(args[0]);
        if (fn.key === 'reverse') value = fn.base.slice().reverse();
        if (fn.key === 'concat') value = fn.base.concat(...args);
      } else if (known && fn && fn.kind === 'stringMethod') {
        if (fn.key === 'concat') value = fn.base.concat(...args);
        if (fn.key === 'slice') value = fn.base.slice(...args);
        if (fn.key === 'substring') value = fn.base.substring(...args);
        if (fn.key === 'toString') value = fn.base;
      } else if (known && fn && fn.kind === 'charCodes' && args.every((arg) => typeof arg === 'number')) {
        value = String.fromCharCode(...args);
      } else if (known && fn && fn.kind === 'bufferFrom' && typeof args[0] === 'string') {
        if (args.length === 1 || ['base64', 'hex', 'utf8', 'utf-8'].includes(args[1])) {
          value = { kind: 'buffer', text: Buffer.from(args[0], args[1]).toString('utf8') };
        }
      } else if (fn && fn.kind === 'bufferString' && (args.length === 0 || ['utf8', 'utf-8'].includes(args[0]))) {
        value = fn.base.text;
      } else if (known && fn && typeof args[0] === 'string') {
        try {
          if (fn.kind === 'atob') value = Buffer.from(args[0], 'base64').toString('binary');
          if (fn.kind === 'decodeURIComponent') value = decodeURIComponent(args[0]);
          if (fn.kind === 'decodeURI') value = decodeURI(args[0]);
        } catch { /* Not a valid constant encoding. */ }
      }
    }
    active.delete(node);
    memo.set(node, value);
    return value;
  }

  function visit(node) {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier) {
      const name = evaluate(node.moduleSpecifier);
      if (typeof name === 'string') imports.add(name);
    }
    if (ts.isCallExpression(node)) {
      const fn = evaluate(node.expression);
      const args = node.arguments.map((arg) => evaluate(arg));
      const loadedArgs = node.expression.kind === ts.SyntaxKind.ImportKeyword ? args : loaderArguments(fn, args);
      if (loadedArgs) {
        const name = loadedArgs[0];
        if (typeof name === 'string') imports.add(name);
      }
      if (fn && fn.kind === 'test' && fn.disabled) disabled += 1;
      if (fn && fn.kind === 'testMethod' && fn.base.disabled && ['call', 'apply'].includes(fn.key)) disabled += 1;
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  return { imports: Array.from(imports).sort(), disabled };
}

const request = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(request.map((row) => analyze(row.source, row.filename))));
