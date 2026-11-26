// Count emitted primitive values in parsed uneval expressions. Comments and
// string contents that merely contain the marker cannot satisfy deduplication.
import compiler from './typescript.cjs';

const cache = new Map();
const MAX_CONSTANT_LENGTH = 65536;

function emissions(source) {
  const ts = compiler.loadTypeScript();
  const filename = '/__evaluator_uneval__.js';
  const file = ts.createSourceFile(filename, `(${source})`, ts.ScriptTarget.ESNext, true, ts.ScriptKind.JS);
  if (file.parseDiagnostics.length) return null;
  const host = {
    getSourceFile: name => name === filename ? file : undefined,
    getDefaultLibFileName: () => '', writeFile() {}, getCurrentDirectory: () => '/',
    getDirectories: () => [], fileExists: name => name === filename,
    readFile: name => name === filename ? file.text : undefined,
    getCanonicalFileName: name => name, useCaseSensitiveFileNames: () => true,
    getNewLine: () => '\n',
  };
  const program = ts.createProgram([filename], { allowJs: true, noLib: true, noResolve: true }, host);
  const checker = program.getTypeChecker();
  const memo = new Map();

  function unwrap(node) {
    while (node && ts.isParenthesizedExpression(node)) node = node.expression;
    return node;
  }

  function builtin(node, name, seen = new Set()) {
    if (ts.isParenthesizedExpression(node)) return builtin(node.expression, name, seen);
    if (ts.isIdentifier(node)) {
      const symbol = checker.getSymbolAtLocation(node);
      if (!symbol?.declarations?.length) return node.text === name;
      if (seen.has(symbol)) return false;
      const declaration = symbol.valueDeclaration;
      const next = new Set(seen).add(symbol);
      if (declaration && ts.isBindingElement(declaration) &&
          ts.isObjectBindingPattern(declaration.parent) && !declaration.dotDotDotToken) {
        const binding = declaration.parent.parent;
        const key = declaration.propertyName ?? declaration.name;
        const property = ts.isIdentifier(key) || ts.isStringLiteral(key) ? key.text
          : ts.isComputedPropertyName(key) ? constant(key.expression) : undefined;
        return ts.isVariableDeclaration(binding) && binding.initializer &&
          binding.parent.flags & ts.NodeFlags.Const && property === name &&
          builtin(binding.initializer, 'globalThis', next);
      }
      return declaration && ts.isVariableDeclaration(declaration) && declaration.initializer &&
        declaration.parent.flags & ts.NodeFlags.Const &&
        builtin(declaration.initializer, name, next);
    }
    if (ts.isPropertyAccessExpression(node) && node.name.text === name) {
      return builtin(node.expression, 'globalThis', seen);
    }
    if (ts.isElementAccessExpression(node) && constant(node.argumentExpression) === name) {
      return builtin(node.expression, 'globalThis', seen);
    }
    return false;
  }

  function constant(node, depth = 0) {
    if (!node || depth > 40) return undefined;
    if (memo.has(node)) return memo.get(node);
    memo.set(node, undefined); // Cyclic initializers are not constants.
    const value = () => {
      if (node.kind === ts.SyntaxKind.TrueKeyword) return true;
      if (node.kind === ts.SyntaxKind.FalseKeyword) return false;
      if (node.kind === ts.SyntaxKind.NullKeyword) return null;
      if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
      if (ts.isBigIntLiteral(node)) return node.text.length <= MAX_CONSTANT_LENGTH
        ? BigInt(node.text.replace(/_/g, '').replace(/n$/, '')) : undefined;
      if (ts.isNumericLiteral(node)) return Number(node.text.replace(/_/g, ''));
      if (ts.isParenthesizedExpression(node)) return constant(node.expression, depth + 1);
      if (ts.isIdentifier(node)) {
        const declaration = checker.getSymbolAtLocation(node)?.valueDeclaration;
        // Only immutable aliases can supply a constant constructor argument.
        if (declaration && ts.isVariableDeclaration(declaration) &&
            declaration.parent.flags & ts.NodeFlags.Const) return constant(declaration.initializer, depth + 1);
      }
      if (ts.isPrefixUnaryExpression(node)) {
        const operand = constant(node.operand, depth + 1);
        if (!['number', 'bigint'].includes(typeof operand)) return undefined;
        if (node.operator === ts.SyntaxKind.MinusToken) return -operand;
        if (node.operator === ts.SyntaxKind.PlusToken && typeof operand === 'number') return +operand;
        if (node.operator === ts.SyntaxKind.TildeToken) return ~operand;
      }
      if (ts.isBinaryExpression(node)) {
        const left = constant(node.left, depth + 1), right = constant(node.right, depth + 1);
        if (left === undefined || right === undefined) return undefined;
        // Bound folding of candidate expressions before allocating their
        // result. Generated doubling chains must not exhaust evaluator RAM.
        if ((typeof left === 'string' || typeof right === 'string') &&
            String(left).length + String(right).length > MAX_CONSTANT_LENGTH) return undefined;
        if (typeof left === 'bigint' && typeof right === 'bigint' &&
            left.toString(16).length + right.toString(16).length > MAX_CONSTANT_LENGTH) return undefined;
        switch (node.operatorToken.kind) {
          case ts.SyntaxKind.PlusToken: return left + right;
          case ts.SyntaxKind.MinusToken: return left - right;
          case ts.SyntaxKind.AsteriskToken: return left * right;
          case ts.SyntaxKind.SlashToken: return left / right;
          case ts.SyntaxKind.PercentToken: return left % right;
        }
      }
      if (ts.isTemplateExpression(node)) {
        let result = node.head.text;
        for (const span of node.templateSpans) {
          const item = constant(span.expression, depth + 1);
          if (item === undefined) return undefined;
          if (result.length + String(item).length + span.literal.text.length > MAX_CONSTANT_LENGTH) return undefined;
          result += String(item) + span.literal.text;
        }
        return result;
      }
      if (ts.isCallExpression(node) && node.arguments.length === 1) {
        const argument = constant(node.arguments[0], depth + 1);
        if (argument === undefined) return undefined;
        if (typeof argument === 'string' && argument.length > MAX_CONSTANT_LENGTH) return undefined;
        if (builtin(node.expression, 'BigInt')) return BigInt(argument);
        if (builtin(node.expression, 'String')) return String(argument);
      }
      return undefined;
    };
    let result;
    try { result = value(); } catch { /* Invalid constant expression. */ }
    memo.set(node, result);
    return result;
  }

  // Follow the returned expression's value dependencies. A standalone literal,
  // unused initializer, unused parameter or uncalled function must not supply
  // the one apparent emission while the returned value is built elsewhere.
  const writes = new Map(), aliases = new Map(), parameterValues = new Map();
  const valueFunctions = new Set(), requiredBindings = new Set(), executedNodes = new Set();
  const activeFunctions = new Set([null]), requiredSymbols = new Set();
  const live = new Set(), pending = [];
  const mutators = new Set(['set', 'add', 'push', 'unshift', 'splice', 'fill', 'copyWithin']);
  function baseSymbol(node) {
    node = unwrap(node);
    while (node && (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node))) node = unwrap(node.expression);
    return node && ts.isIdentifier(node) ? checker.getSymbolAtLocation(node) : undefined;
  }
  function recordWrite(symbol, expression, owner) {
    if (!symbol) return;
    if (!writes.has(symbol)) writes.set(symbol, []);
    writes.get(symbol).push({ expression, owner });
  }
  function index(node, owner = null) {
    if (ts.isFunctionLike(node)) owner = node;
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) &&
        node.initializer && ts.isIdentifier(unwrap(node.initializer)) && node.parent.flags & ts.NodeFlags.Const) {
      const source = baseSymbol(node.initializer), target = checker.getSymbolAtLocation(node.name);
      if (source && target) {
        if (!aliases.has(source)) aliases.set(source, new Set());
        aliases.get(source).add(target);
      }
    }
    if (ts.isBinaryExpression(node) &&
        node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment &&
        node.operatorToken.kind <= ts.SyntaxKind.LastAssignment) {
      recordWrite(baseSymbol(node.left), node, owner);
    }
    if (ts.isCallExpression(node)) {
      const method = unwrap(node.expression);
      const member = ts.isPropertyAccessExpression(method) ? method.name.text
        : ts.isElementAccessExpression(method) ? constant(method.argumentExpression) : undefined;
      if (mutators.has(member)) {
        recordWrite(baseSymbol(method.expression), node, owner);
      }
      if (['defineProperty', 'defineProperties', 'assign'].includes(member) &&
          builtin(method.expression, 'Object')) {
        recordWrite(baseSymbol(node.arguments[0]), node, owner);
      }
    }
    ts.forEachChild(node, child => index(child, owner));
  }
  index(file);

  function mark(node) {
    if (node && !live.has(node)) { live.add(node); pending.push(node); }
  }
  function followWrites(symbol, seen = new Set()) {
    if (seen.has(symbol)) return;
    seen.add(symbol);
    for (const write of writes.get(symbol) ?? []) {
      if (activeFunctions.has(write.owner) && executedNodes.has(write.expression)) mark(write.expression);
    }
    for (const alias of aliases.get(symbol) ?? []) followWrites(alias, seen);
  }
  function resolveFunction(node, seen = new Set()) {
    node = unwrap(node);
    if (!node) return null;
    if (ts.isFunctionExpression(node) || ts.isArrowFunction(node) || ts.isFunctionDeclaration(node)) return node;
    if ((ts.isPropertyAccessExpression(node) && ['call', 'apply'].includes(node.name.text))) {
      return resolveFunction(node.expression, seen);
    }
    if (!ts.isIdentifier(node)) return null;
    const symbol = checker.getSymbolAtLocation(node);
    if (!symbol || seen.has(symbol)) return null;
    seen.add(symbol);
    const declaration = symbol.valueDeclaration;
    return declaration && ts.isVariableDeclaration(declaration)
      ? resolveFunction(declaration.initializer, seen)
      : declaration && ts.isFunctionDeclaration(declaration) ? declaration : null;
  }
  // A function can execute only for its writes. Its unused return value must
  // not provide a decoy emission. Discover those calls in executable bodies,
  // while keeping return-value liveness separate from execution reachability.
  function execution(node) {
    if (!node || ts.isFunctionLike(node)) return;
    executedNodes.add(node);
    if (ts.isBlock(node)) {
      for (const statement of node.statements) {
        if (execution(statement)) return true;
      }
      return;
    }
    if (ts.isIfStatement(node) || ts.isConditionalExpression(node)) {
      const condition = constant(node.expression ?? node.condition);
      const yes = node.thenStatement ?? node.whenTrue;
      const no = node.elseStatement ?? node.whenFalse;
      execution(node.expression ?? node.condition);
      if (condition !== undefined) return execution(condition ? yes : no);
      const yesStops = execution(yes), noStops = execution(no);
      return yesStops && noStops;
    }
    if (ts.isBinaryExpression(node)) {
      const left = constant(node.left);
      execution(node.left);
      if (node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken && left !== undefined && !left ||
          node.operatorToken.kind === ts.SyntaxKind.BarBarToken && left !== undefined && left ||
          node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken && left != null) return;
      execution(node.right);
      return;
    }
    if (ts.isCallExpression(node)) {
      const fn = resolveFunction(node.expression);
      if (fn) activate(fn, callArguments(node), false);
    }
    ts.forEachChild(node, execution);
    return ts.isReturnStatement(node) || ts.isThrowStatement(node);
  }
  function callArguments(node) {
    const method = unwrap(node.expression);
    if (ts.isPropertyAccessExpression(method) && method.name.text === 'call') return [...node.arguments].slice(1);
    if (ts.isPropertyAccessExpression(method) && method.name.text === 'apply') {
      const list = unwrap(node.arguments[1]);
      if (list && ts.isArrayLiteralExpression(list)) return [...list.elements];
    }
    return [...(node.arguments ?? [])];
  }
  function bindingSource(declaration) {
    const selectors = [];
    let current = declaration;
    while (current && ts.isBindingElement(current)) {
      const pattern = current.parent;
      const key = ts.isArrayBindingPattern(pattern) ? pattern.elements.indexOf(current)
        : current.propertyName ? ts.isComputedPropertyName(current.propertyName)
          ? constant(current.propertyName.expression) : current.propertyName.text
        : ts.isIdentifier(current.name) ? current.name.text : undefined;
      selectors.unshift({ key, rest: !!current.dotDotDotToken, fallback: current.initializer });
      current = pattern.parent;
    }
    return { owner: current, selectors };
  }
  function projection(node, selectors, seen = new Set()) {
    node = unwrap(node);
    if (!node || seen.has(node)) return;
    if (!selectors.length) { mark(node); return; }
    const next = new Set(seen).add(node);
    if (ts.isIdentifier(node)) {
      const symbol = checker.getSymbolAtLocation(node);
      const declaration = symbol?.valueDeclaration;
      if (declaration && ts.isVariableDeclaration(declaration) && declaration.initializer) {
        projection(declaration.initializer, selectors, next);
        return;
      }
      for (const value of parameterValues.get(symbol) ?? []) projection(value, selectors, next);
      if (parameterValues.has(symbol)) return;
    }
    const [selector, ...remaining] = selectors;
    let selected;
    if (ts.isArrayLiteralExpression(node) && typeof selector.key === 'number') {
      if (selector.rest) {
        for (const element of node.elements.slice(selector.key)) projection(element, remaining, next);
        return;
      }
      selected = node.elements[selector.key];
      if (selected && ts.isOmittedExpression(selected)) selected = undefined;
    } else if (ts.isObjectLiteralExpression(node)) {
      for (const property of node.properties) {
        const key = property.name && (ts.isComputedPropertyName(property.name)
          ? constant(property.name.expression) : property.name.text);
        if (String(key) === String(selector.key)) {
          selected = ts.isPropertyAssignment(property) ? property.initializer
            : ts.isShorthandPropertyAssignment(property) ? property.name : undefined;
        }
      }
      if (selector.rest) { mark(node); return; }
    } else {
      // Unknown expressions remain accountable; do not replace their actual
      // dependencies with just a destructuring default.
      mark(node);
      if (selector.fallback) mark(selector.fallback);
      return;
    }
    if (!selected || ts.isIdentifier(selected) && selected.text === 'undefined' &&
        !checker.getSymbolAtLocation(selected)?.declarations?.length) selected = selector.fallback;
    projection(selected, remaining, next);
  }
  function followBinding(declaration) {
    requiredBindings.add(declaration);
    const { owner, selectors } = bindingSource(declaration);
    if (owner && ts.isVariableDeclaration(owner)) projection(owner.initializer, selectors);
    else if (owner && ts.isParameter(owner)) {
      for (const value of parameterValues.get(owner) ?? []) projection(value, selectors);
      if (!parameterValues.has(owner)) projection(owner.initializer, selectors);
    }
  }
  function activate(fn, args, returned = true) {
    for (let i = 0; i < fn.parameters.length; i++) {
      const parameter = fn.parameters[i];
      {
        const symbol = ts.isIdentifier(parameter.name) ? checker.getSymbolAtLocation(parameter.name) : parameter;
        if (!parameterValues.has(symbol)) parameterValues.set(symbol, new Set());
        const values = parameter.dotDotDotToken ? args.slice(i) : [args[i] ?? parameter.initializer];
        for (const value of values) {
          if (value) parameterValues.get(symbol).add(value);
          const source = baseSymbol(value);
          if (source) {
            if (!aliases.has(source)) aliases.set(source, new Set());
            aliases.get(source).add(symbol);
          }
          if (requiredSymbols.has(symbol)) mark(value);
        }
        if (!ts.isIdentifier(parameter.name)) {
          for (const binding of requiredBindings) {
            if (bindingSource(binding).owner === parameter) followBinding(binding);
          }
        }
      }
    }
    if (!activeFunctions.has(fn)) {
      activeFunctions.add(fn);
      execution(fn.body);
      for (const symbol of requiredSymbols) followWrites(symbol);
    }
    if (!returned || valueFunctions.has(fn)) return;
    valueFunctions.add(fn);
    function returns(node) {
      if (node !== fn.body && ts.isFunctionLike(node)) return;
      if (ts.isReturnStatement(node)) mark(node.expression);
      else ts.forEachChild(node, returns);
    }
    if (fn.body && ts.isBlock(fn.body)) returns(fn.body);
    else mark(fn.body);
  }
  execution(file.statements[0]?.expression);
  mark(file.statements[0]?.expression);
  while (pending.length) {
    const node = pending.pop();
    if (ts.isIdentifier(node)) {
      const symbol = ts.isShorthandPropertyAssignment(node.parent)
        ? checker.getShorthandAssignmentValueSymbol(node.parent) : checker.getSymbolAtLocation(node);
      if (!symbol) continue;
      requiredSymbols.add(symbol);
      const declaration = symbol.valueDeclaration;
      if (declaration && ts.isVariableDeclaration(declaration)) mark(declaration.initializer);
      if (declaration && ts.isBindingElement(declaration)) followBinding(declaration);
      for (const value of parameterValues.get(symbol) ?? []) mark(value);
      followWrites(symbol);
    } else if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
      const fn = resolveFunction(node.expression);
      if (fn) activate(fn, callArguments(node));
      else {
        mark(node.expression);
        for (const argument of node.arguments ?? []) mark(argument);
      }
    } else if (ts.isFunctionLike(node)) {
      // Function bodies become live when called, not merely when declared.
    } else if (ts.isConditionalExpression(node)) {
      const condition = constant(node.condition);
      if (condition !== undefined) mark(condition ? node.whenTrue : node.whenFalse);
      else { mark(node.whenTrue); mark(node.whenFalse); }
    } else if (ts.isBinaryExpression(node) && [ts.SyntaxKind.AmpersandAmpersandToken,
        ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken].includes(node.operatorToken.kind)) {
      const left = constant(node.left);
      if (left !== undefined) mark(node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
        ? left ? node.right : node.left : node.operatorToken.kind === ts.SyntaxKind.BarBarToken
          ? left ? node.left : node.right : left === null ? node.right : node.left);
      else { mark(node.left); mark(node.right); }
    } else if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.CommaToken) {
      mark(node.right);
    } else if (ts.isBinaryExpression(node) &&
        node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment &&
        node.operatorToken.kind <= ts.SyntaxKind.LastAssignment) {
      mark(node.right);
      if (!ts.isIdentifier(unwrap(node.left))) mark(node.left);
    } else if (ts.isPropertyAccessExpression(node)) {
      mark(node.expression);
    } else {
      ts.forEachChild(node, mark);
    }
  }

  const counts = new Map();
  for (const node of live) {
    // References reuse an existing value; declarations and literal/constructor
    // expressions emit it. Count a folded expression once rather than counting
    // each intermediate operand as another emitted output value.
    if (!ts.isIdentifier(node)) {
      const value = constant(node);
      if (['string', 'bigint'].includes(typeof value) &&
          (!live.has(node.parent) || constant(node.parent) === undefined)) {
        const key = `${typeof value}:${value}`;
        counts.set(key, (counts.get(key) ?? 0) + 1);
      }
    }
  }
  return counts;
}

export function primitiveEmissionCount(source, value) {
  if (typeof source !== 'string') return -1;
  try {
    if (!cache.has(source)) {
      if (cache.size >= 64) cache.delete(cache.keys().next().value);
      cache.set(source, emissions(source));
    }
    const counts = cache.get(source);
    return counts ? counts.get(`${typeof value}:${value}`) ?? 0 : -1;
  } catch {
    return -1;
  }
}
