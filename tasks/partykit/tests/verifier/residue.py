"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import json
import re
import subprocess
from functools import lru_cache
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "dist",
    "build",
    "coverage",
}
_RULE_KINDS = {"literal", "dependency", "import", "dynamic_import", "file"}
_TEST_SOURCE_PATH = re.compile(
    r"(?:^|/)(?:test[^/]*|[^/]*(?:\.test|\.spec)\.[^/]*"
    r"|[^/]*_(?:test|spec)\.[^/]+|[^/]*(?:Test|Tests|Spec)\.[^/]+)$",
    re.IGNORECASE,
)


_IMPORT_ANALYZER = r"""
const ts = require('/opt/migration/dependencies/node_modules/typescript');
const text = require('fs').readFileSync(0, 'utf8');
const source = ts.createSourceFile('candidate.ts', text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const bindings = new Map();
const testAliases = new Map();
const moduleBindings = new Map();
function collect(node) {
  if (ts.isImportSpecifier(node)) testAliases.set(node.name.text, (node.propertyName || node.name).text);
  if (ts.isImportDeclaration(node) && ts.isStringLiteralLike(node.moduleSpecifier) && ['node:module', 'module'].includes(node.moduleSpecifier.text) && node.importClause) {
    const named = node.importClause.namedBindings;
    if (named && ts.isNamespaceImport(named)) moduleBindings.set(named.name.text, 'module');
    if (named && ts.isNamedImports(named)) {
      for (const specifier of named.elements) {
        if ((specifier.propertyName || specifier.name).text === 'createRequire') moduleBindings.set(specifier.name.text, 'createRequire');
      }
    }
  }
  if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) {
    const values = bindings.get(node.name.text) || [];
    values.push(node.initializer);
    bindings.set(node.name.text, values);
  }
  if (ts.isVariableDeclaration(node) && ts.isObjectBindingPattern(node.name) && node.initializer) {
    for (const element of node.name.elements) {
      if (ts.isIdentifier(element.name) && !element.dotDotDotToken) {
        const property = element.propertyName || element.name;
        moduleBindings.set(element.name.text, {from: node.initializer, property});
      }
    }
  }
  ts.forEachChild(node, collect);
}
collect(source);
function unwrap(node) {
  while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) || ts.isSatisfiesExpression(node))) node = node.expression;
  return node;
}
function memberLoader(base, property) {
  if (base && base.fields) return base.fields.get(property);
  if (property === 'createRequire' && base === 'module') return 'createRequire';
  if (property === 'module' && base === 'globalThis') return 'module';
  if (property === 'constructor' && base === 'module') return 'module-constructor';
  if (property === '_load' && base === 'module-constructor') return 'require';
  if (property === 'apply' && base === 'Reflect') return 'reflect-apply';
  if (property === 'process' && base === 'globalThis') return 'process';
  if (property === 'require' && base === 'globalThis') return 'require';
  if (property === 'getBuiltinModule' && base === 'process') return 'builtin';
  if (['call', 'apply', 'bind'].includes(property) && base === 'require') return property;
  return undefined;
}
function loader(node, seen = new Set(), locals = new Map()) {
  node = unwrap(node);
  if (!node) return undefined;
  if (ts.isIdentifier(node)) {
    if (locals.has(node.text)) return locals.loaders?.get(node.text);
    if (seen.has(node.text)) return undefined;
    const next = new Set([...seen, node.text]);
    const values = bindings.get(node.text);
    if (values) return values.length === 1 ? loader(values[0], next, locals) : undefined;
    const imported = moduleBindings.get(node.text);
    if (imported && typeof imported === 'object') {
      const key = imported.property;
      const property = ts.isComputedPropertyName(key) ? evaluate(key.expression, new Set(), locals) : key.text;
      return memberLoader(loader(imported.from, next, locals), property);
    }
    if (imported) return imported;
    return ['require', 'process', 'globalThis', 'module', 'Reflect'].includes(node.text) ? node.text : undefined;
  }
  if (ts.isObjectLiteralExpression(node)) {
    const fields = new Map();
    for (const entry of node.properties) {
      if (!entry.name) continue;
      const key = ts.isComputedPropertyName(entry.name) ? evaluate(entry.name.expression, new Set(), locals) : entry.name.text;
      if (ts.isPropertyAssignment(entry)) fields.set(key, loader(entry.initializer, seen, locals));
      if (ts.isShorthandPropertyAssignment(entry)) fields.set(key, loader(entry.name, seen, locals));
      if (ts.isMethodDeclaration(entry)) fields.set(key, {fn: entry, locals});
    }
    return {fields};
  }
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) return {fn: node, locals};
  if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
    const property = ts.isPropertyAccessExpression(node) ? node.name.text : evaluate(node.argumentExpression, new Set(), locals);
    const base = loader(node.expression, seen, locals);
    return memberLoader(base, property);
  }
  // A comma expression evaluates to its right operand, including loader aliases.
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.CommaToken) return loader(node.right, seen, locals);
  if (ts.isCallExpression(node)) {
    const target = loader(node.expression, seen, locals);
    if (target === 'createRequire' || target === 'bind') return 'require';
    if (target === 'require' && ['node:module', 'module'].includes(evaluate(node.arguments[0], new Set(), locals))) return 'module';
    if (target && target.fn && !seen.has(target.fn)) {
      const parameters = callLocals(target, node.arguments, locals);
      const body = target.fn.body;
      const returned = ts.isBlock(body) ? body.statements.find(ts.isReturnStatement)?.expression : body;
      return loader(returned, new Set([...seen, target.fn]), parameters);
    }
    const constructor = functionConstructor(node.expression);
    if (constructor && !seen.has(constructor.body)) {
      const generated = ts.createSourceFile('returned.ts', `function __returned() { ${constructor.body}\n }`, ts.ScriptTarget.Latest, true);
      const returned = generated.statements[0].body.statements.find(ts.isReturnStatement)?.expression;
      const parameters = parameterLocals(constructor.params, node.arguments, locals);
      return loader(returned, new Set([...seen, constructor.body]), parameters);
    }
  }
  return undefined;
}
function callLocals(target, args, locals) {
  return parameterLocals(target.fn.parameters.map(param => ts.isIdentifier(param.name) ? param.name.text : undefined), args, locals, target.locals);
}
function parameterLocals(names, args, locals, inherited = new Map()) {
  const parameters = new Map(inherited);
  parameters.loaders = new Map(inherited.loaders);
  names.forEach((name, index) => {
    if (name === undefined) return;
    parameters.set(name, evaluate(args[index], new Set(), locals));
    parameters.loaders.set(name, loader(args[index], new Set(), locals));
  });
  return parameters;
}
function evaluate(node, seen = new Set(), locals = new Map()) {
  if (!node) return undefined;
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.CommaToken) return evaluate(node.right, seen, locals);
  if (ts.isNumericLiteral(node)) return Number(node.text);
  if (node.kind === ts.SyntaxKind.TrueKeyword) return true;
  if (node.kind === ts.SyntaxKind.FalseKeyword) return false;
  if (ts.isStringLiteralLike(node)) return node.text;
  if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) || ts.isSatisfiesExpression(node)) return evaluate(node.expression, seen, locals);
  if (ts.isIdentifier(node)) {
    if (locals.has(node.text)) return locals.get(node.text);
    if (seen.has(node.text)) return undefined;
    const values = bindings.get(node.text);
    if (!values || values.length !== 1) return undefined;
    return evaluate(values[0], new Set([...seen, node.text]), locals);
  }
  if (ts.isArrayLiteralExpression(node)) {
    const values = node.elements.map(element => evaluate(element, seen, locals));
    return values.every(value => value !== undefined) ? values : undefined;
  }
  if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
    const left = evaluate(node.left, seen, locals), right = evaluate(node.right, seen, locals);
    return typeof left === 'string' && typeof right === 'string' ? left + right : undefined;
  }
  if (ts.isTemplateExpression(node)) {
    let value = node.head.text;
    for (const span of node.templateSpans) {
      const part = evaluate(span.expression, seen, locals);
      if (typeof part !== 'string') return undefined;
      value += part + span.literal.text;
    }
    return value;
  }
  if (ts.isCallExpression(node)) {
    const args = node.arguments.map(argument => evaluate(argument, seen, locals));
    if (ts.isIdentifier(node.expression) && node.expression.text === 'atob' && args.length === 1 && typeof args[0] === 'string') {
      try { return Buffer.from(args[0], 'base64').toString('latin1'); } catch { return undefined; }
    }
    if (ts.isPropertyAccessExpression(node.expression)) {
      const method = node.expression.name.text;
      const target = node.expression.expression;
      if (ts.isIdentifier(target) && target.text === 'String' && ['fromCharCode', 'fromCodePoint'].includes(method) && args.every(value => Number.isInteger(value) && value >= 0 && value <= 0x10ffff)) {
        try { return String[method](...args); } catch { return undefined; }
      }
      if (method === 'from' && ts.isIdentifier(target) && target.text === 'Buffer' && args.length <= 2 && (typeof args[0] === 'string' || Array.isArray(args[0]))) {
        if (args[1] !== undefined && !['base64', 'hex', 'utf8', 'latin1'].includes(args[1])) return undefined;
        try { return {bytes: Buffer.from(args[0], args[1])}; } catch { return undefined; }
      }
      const receiver = evaluate(target, seen, locals);
      if (method === 'toString' && receiver && receiver.bytes && args.length <= 1 && (args.length === 0 || ['utf8', 'latin1', 'base64', 'hex'].includes(args[0]))) return receiver.bytes.toString(args[0] || 'utf8');
      if (method === 'reverse' && Array.isArray(receiver) && args.length === 0) return [...receiver].reverse();
      if (method === 'join' && Array.isArray(receiver) && (args.length === 0 || typeof args[0] === 'string')) return receiver.join(args.length ? args[0] : ',');
      if (method === 'concat' && typeof receiver === 'string' && args.every(value => typeof value === 'string')) return receiver.concat(...args);
    }
  }
  return undefined;
}
// Follow possible string values at loader arguments without executing code.
// An unknown environment value does not erase a known fallback to a source runtime.
function specifierValues(node, seen = new Set(), locals = new Map()) {
  node = unwrap(node);
  if (!node) return [];
  const exact = evaluate(node, new Set(seen), locals);
  if (typeof exact === 'string') return [exact];
  if (ts.isIdentifier(node) && !seen.has(node.text)) {
    const values = bindings.get(node.text) || [];
    return values.flatMap(value => specifierValues(value, new Set([...seen, node.text]), locals));
  }
  if (ts.isBinaryExpression(node) && [ts.SyntaxKind.QuestionQuestionToken, ts.SyntaxKind.BarBarToken].includes(node.operatorToken.kind)) {
    return [...specifierValues(node.left, seen, locals), ...specifierValues(node.right, seen, locals)];
  }
  if (ts.isConditionalExpression(node)) return [...specifierValues(node.whenTrue, seen, locals), ...specifierValues(node.whenFalse, seen, locals)];
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'String') return specifierValues(node.arguments[0], seen, locals);
  return [];
}
const imports = [];
let disabled = 0;
function functionConstructor(node, seen = new Set()) {
  if (ts.isParenthesizedExpression(node)) return functionConstructor(node.expression, seen);
  if (ts.isIdentifier(node) && node.text !== 'Function' && !seen.has(node.text)) {
    const values = bindings.get(node.text);
    return values && values.length === 1 ? functionConstructor(values[0], new Set([...seen, node.text])) : undefined;
  }
  if ((ts.isNewExpression(node) || ts.isCallExpression(node)) && ts.isIdentifier(node.expression) && node.expression.text === 'Function') {
    const args = node.arguments || [];
    const parts = args.map(argument => evaluate(argument));
    if (!parts.length || !parts.every(part => typeof part === 'string')) return undefined;
    const params = parts.slice(0, -1);
    if (!params.every(param => /^[A-Za-z_$][\w$]*$/.test(param))) return undefined;
    return {params, body: parts.at(-1)};
  }
  return undefined;
}
function scanGenerated(body, locals, depth) {
  if (depth >= 3 || body.length > 100000) return;
  const generated = ts.createSourceFile('generated.ts', `function __generated() { ${body}\n }`, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  visit(generated, locals, depth + 1);
}
function testChain(node, seen = new Set()) {
  if (ts.isIdentifier(node)) {
    const name = testAliases.get(node.text) || node.text;
    if (['describe', 'it', 'test', 'context', 'specify', 'suite'].includes(name)) return [];
    if (['xdescribe', 'xit', 'xtest', 'xcontext', 'xspecify', 'xsuite', 'fdescribe', 'fit'].includes(name)) return ['skip'];
    const values = bindings.get(node.text);
    if (!seen.has(node.text) && values && values.length === 1) return testChain(values[0], new Set([...seen, node.text]));
  }
  if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
    const chain = testChain(node.expression, seen);
    const name = ts.isPropertyAccessExpression(node) ? node.name.text : evaluate(node.argumentExpression);
    if (chain && typeof name === 'string') return [...chain, name];
  }
  if (ts.isCallExpression(node)) {
    const chain = testChain(node.expression, seen);
    if (chain && ['skipIf', 'runIf'].includes(chain.at(-1))) {
      const condition = evaluate(node.arguments[0]);
      return [...chain, (chain.at(-1) === 'skipIf' ? condition !== false : condition !== true) ? 'skip' : ''];
    }
    return chain;
  }
  return undefined;
}
function visit(node, locals = new Map(), depth = 0) {
  if (depth === 0 && ts.isCallExpression(node) && !ts.isCallExpression(node.expression)) {
    const chain = testChain(node);
    if (chain && chain.some(modifier => ['skip', 'todo', 'only'].includes(modifier))) disabled++;
  }
  if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteralLike(node.moduleSpecifier)) imports.push({value: node.moduleSpecifier.text, dynamic: false});
  if (ts.isCallExpression(node)) {
    let kind = node.expression.kind === ts.SyntaxKind.ImportKeyword ? 'import' : loader(node.expression, new Set(), locals);
    let argument = node.arguments[0];
    if (kind === 'reflect-apply') {
      kind = loader(node.arguments[0], new Set(), locals);
      const args = unwrap(node.arguments[2]);
      argument = args && ts.isArrayLiteralExpression(args) ? args.elements[0] : undefined;
    }
    if (kind === 'call' || kind === 'apply') argument = node.arguments[1];
    if (kind === 'apply' && argument && ts.isArrayLiteralExpression(argument)) argument = argument.elements[0];
    if (kind === 'bind') argument = node.arguments[1];
    if (kind === 'import' || ['require', 'call', 'apply', 'bind', 'builtin'].includes(kind)) {
      for (const value of specifierValues(argument, new Set(), locals)) imports.push({value, dynamic: true});
    }
  }
  if (ts.isCallExpression(node)) {
    const target = loader(node.expression, new Set(), locals);
    if (target && target.fn && depth < 3) visit(target.fn.body, callLocals(target, node.arguments, locals), depth + 1);
    const constructor = functionConstructor(node.expression);
    if (constructor) {
      const parameters = parameterLocals(constructor.params, node.arguments, locals);
      scanGenerated(constructor.body, parameters, depth);
    }
    if (ts.isIdentifier(node.expression) && node.expression.text === 'eval') {
      const body = evaluate(node.arguments[0], new Set(), locals);
      if (typeof body === 'string') scanGenerated(body, locals, depth);
    }
  }
  if (ts.isNewExpression(node) || ts.isCallExpression(node)) {
    const constructor = functionConstructor(node);
    if (constructor) scanGenerated(constructor.body, locals, depth);
  }
  ts.forEachChild(node, child => visit(child, locals, depth));
}
visit(source);
process.stdout.write(JSON.stringify({imports, disabled}));
"""


@lru_cache(maxsize=128)
def _javascript_analysis(text: str) -> dict:
    result = subprocess.run(
        ["node", "-e", _IMPORT_ANALYZER], input=text, text=True,
        capture_output=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def _javascript_imports(text: str) -> tuple[tuple[str, bool], ...]:
    return tuple((row["value"], row["dynamic"]) for row in _javascript_analysis(text)["imports"])


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _EXCLUDED_PARTS for part in relative.parts)
        and not (
            path.suffix.casefold() in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
            and (
                "test" in {part.casefold() for part in relative.parts[:-1]}
                or _TEST_SOURCE_PATH.search(relative.as_posix())
            )
        )
    )


def _paths(root: Path, patterns: Sequence[str]) -> Iterable[Path]:
    if not patterns:
        return ()
    collected: list[Path] = []
    for pattern in patterns:
        if not _safe_relative(pattern):
            continue
        path = root / pattern
        if any(token in pattern for token in "*?["):
            collected.extend(root.glob(pattern))
        elif path.is_dir() and not path.is_symlink():
            collected.extend(path.rglob("*"))
        else:
            collected.append(path)
    candidates = collected
    unique = {path.resolve(): path for path in candidates if _eligible(root, path)}
    return (unique[key] for key in sorted(unique, key=lambda item: item.as_posix()))


def _target_matches(value: str, target: str) -> bool:
    actual = value.casefold().strip()
    expected = target.casefold().strip()
    return actual == expected or fnmatchcase(actual, expected)


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = _strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return False
    return target.casefold() in text.casefold()


def _strip_comments(text: str) -> str:
    characters = list(text)
    index = 0
    while index < len(text):
        character = text[index]
        if character in ("'", '"', "`"):
            quote = character
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                elif text[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            continue
        if character == "/" and index + 1 < len(text) and text[index + 1] == "/":
            characters[index] = " "
            characters[index + 1] = " "
            index += 2
            while index < len(text) and text[index] != "\n":
                characters[index] = " "
                index += 1
            continue
        if character == "/" and index + 1 < len(text) and text[index + 1] == "*":
            characters[index] = " "
            characters[index + 1] = " "
            index += 2
            while index < len(text):
                if text[index] == "*" and index + 1 < len(text) and text[index + 1] == "/":
                    characters[index] = " "
                    characters[index + 1] = " "
                    index += 2
                    break
                if text[index] != "\n":
                    characters[index] = " "
                index += 1
            continue
        index += 1
    return "".join(characters)


def _dependency_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        for field in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
            "require",
            "require-dev",
        ):
            entries = payload.get(field)
            if isinstance(entries, Mapping) and any(
                _target_matches(str(name), target) for name in entries
            ):
                return True
    dependency_lines = (
        re.compile(r"^\s*([A-Za-z0-9@._+/-]+)\s*(?:[<>=~!].*)?$"),
        re.compile(r"^\s*(?:gem|implementation|api|compileOnly|runtimeOnly)\s*[( ]\s*['\"]([^'\"]+)"),
        re.compile(r"^\s*([A-Za-z0-9_.+-]+)\s*=\s*['\"]?[0-9*^~<>=]"),
    )
    for line in text.splitlines():
        for pattern in dependency_lines:
            match = pattern.search(line)
            if match and _target_matches(match.group(1), target):
                return True
    return False


def _import_contains(path: Path, target: str, *, dynamic_only: bool = False) -> bool:
    try:
        text = _strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return False
    candidates: list[str] = []
    if path.suffix.casefold() in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}:
        candidates.extend(
            value for value, dynamic in _javascript_imports(text)
            if dynamic or not dynamic_only
        )
        if dynamic_only:
            return any(_target_matches(value, target) for value in candidates)
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bfrom\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\brequire\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"\b(?:use|using)\s+([A-Za-z0-9_.\\]+)"),
    )
    for pattern in patterns:
        candidates.extend(match.group(1) for match in pattern.finditer(text))
    return any(
        _target_matches(candidate, target)
        or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
        for candidate in candidates
    )


def _matches(root: Path, path: Path, *, kind: str, target: str) -> bool:
    if kind in {"literal", "import", "dynamic_import"} and path.suffix.casefold() not in {
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    }:
        return False
    if kind == "literal":
        return _literal_contains(path, target)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    if kind == "dynamic_import":
        return _import_contains(path, target, dynamic_only=True)
    relative = path.relative_to(root).as_posix()
    return fnmatchcase(relative.casefold(), target.casefold()) or fnmatchcase(
        path.name.casefold(), target.casefold()
    )


def scan_residue_rules(
    root: Path, rules: object
) -> list[dict[str, str]]:
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    hits: list[dict[str, str]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        target = row.get("target")
        patterns = row.get("paths", [])
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(kind, str)
            or not isinstance(target, str)
            or not target
            or not isinstance(patterns, Sequence)
            or isinstance(patterns, (str, bytes))
            or not patterns
            or any(
                not isinstance(item, str) or not _safe_relative(item)
                for item in patterns
            )
        ):
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind not in _RULE_KINDS:
            continue
        for path in _paths(root, tuple(patterns)):
            matched = _matches(root, path, kind=str(kind), target=target)
            if matched:
                hits.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "token": target,
                        "kind": str(kind),
                        "rule_id": identifier,
                    }
                )
    return hits


def classify_residue_rules(
    root: Path, rules: object
) -> list[dict[str, object]]:
    """Classify residue rules without treating unsupported syntax as a failure."""

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    diagnostics: list[dict[str, object]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        if kind not in _RULE_KINDS:
            diagnostics.append(
                {
                    "rule_id": str(identifier or "<unknown>"),
                    "kind": str(kind or ""),
                    "status": "UNSUPPORTED",
                    "hits": [],
                    "reason": "residue rule syntax is not supported by the generic scanner",
                }
            )
            continue
        hits = scan_residue_rules(root, [row])
        diagnostics.append(
            {
                "rule_id": str(identifier),
                "kind": str(kind),
                "status": "FAIL" if hits else "PASS",
                "hits": hits,
            }
        )
    return diagnostics


__all__ = ["classify_residue_rules", "scan_residue_rules"]
