import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { relative } from "node:path";
import { trustedTypeScript } from "./compiler.mjs";
import { productionSourceFiles } from "./native-layout.mjs";

// Supplement the dynamic delegation probes with local value provenance. This
// is not a proof for arbitrary programs. In particular, an opaque call's result
// must not inherit the facts marshalled into its arguments: Rust may decide it.
export function rustResponsibilityViolations(root) {
	const ts = trustedTypeScript(), violations = [];
	for (const file of productionSourceFiles(root).filter(file => /\.[cm]?[jt]sx?$/.test(file) && !/\.d\.[cm]?ts$/.test(file))) {
		const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);
		if (source.isDeclarationFile) continue;
		const options = { allowJs: true, noLib: true, noResolve: true, module: ts.ModuleKind.ESNext };
		const host = ts.createCompilerHost(options);
		host.getSourceFile = name => name === file ? source : undefined;
		const checker = ts.createProgram([file], options, host).getTypeChecker();
		const symbol = node => checker.getSymbolAtLocation(node) ?? node;
		const isFunction = node => ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node) || ts.isMethodDeclaration(node);
		const unwrap = node => {
			while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) || ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node))) node = node.expression;
			return node;
		};
		const walk = (node, visit, nestedFunctions = true, first = true) => {
			if (!node || ts.isTypeNode(node) || ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node) || !first && !nestedFunctions && isFunction(node)) return;
			visit(node);
			ts.forEachChild(node, child => walk(child, visit, nestedFunctions, false));
		};
		const has = (node, predicate) => {
			let found = false;
			walk(node, child => { if (predicate(child)) found = true; }, false);
			return found;
		};
		const bindings = new Map(), functions = new Map(), mutated = new Set();
		const bind = (name, expression, path = []) => {
			if (ts.isIdentifier(name)) {
				const key = symbol(name);
				if (!bindings.has(key)) bindings.set(key, []);
				bindings.get(key).push({ expression, path });
			} else if (ts.isObjectBindingPattern(name) || ts.isArrayBindingPattern(name)) {
				name.elements.forEach((element, index) => {
					if (ts.isOmittedExpression(element) || element.dotDotDotToken) return;
					const key = ts.isArrayBindingPattern(name) ? index : element.propertyName ?? (ts.isIdentifier(element.name) ? element.name.text : undefined);
					if (key !== undefined) bind(element.name, expression, [...path, key]);
				});
			}
		};
		walk(source, node => {
			if (ts.isVariableDeclaration(node) && node.initializer) bind(node.name, node.initializer);
			if (ts.isFunctionDeclaration(node) && node.name) functions.set(symbol(node.name), node);
			if (ts.isBinaryExpression(node) && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment && ts.isIdentifier(node.left)) {
				mutated.add(symbol(node.left));
				bind(node.left, node.right);
			}
			if ((ts.isPrefixUnaryExpression(node) || ts.isPostfixUnaryExpression(node)) && [ts.SyntaxKind.PlusPlusToken, ts.SyntaxKind.MinusMinusToken].includes(node.operator) && ts.isIdentifier(node.operand)) mutated.add(symbol(node.operand));
		});
		const UNKNOWN = Symbol("unknown"), EMPTY = new Map();
		const ID = 1, SCHEMA = 2, ISSUES = 4, INDEXED = 8, IDS = 16;
		// Objects here are analysis records, not the candidate's JS objects.
		// Only fold coercions whose primitive/array value is fully known.
		const coercible = value => value !== UNKNOWN && (value === null || typeof value !== "object" || Array.isArray(value) && value.every(coercible));
		const propertyKey = value => coercible(value) ? String(value) : UNKNOWN;
		const keyOf = (name, scope = EMPTY, seen = new Set()) => typeof name === "string" || typeof name === "number" ? String(name) :
			ts.isComputedPropertyName(name) ? propertyKey(scalar(name.expression, scope, seen)) : name.text ?? UNKNOWN;
		const constantMember = (target, key) => target !== UNKNOWN && target != null && key !== UNKNOWN && Object.hasOwn(Object(target), key) ? target[key] : UNKNOWN;
		const scalar = (value, scope = EMPTY, seen = new Set()) => {
			const node = unwrap(value);
			if (!node || seen.has(node)) return UNKNOWN;
			seen = new Set(seen).add(node);
			if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
			if (ts.isNumericLiteral(node)) return Number(node.text);
			if (node.kind === ts.SyntaxKind.TrueKeyword) return true;
			if (node.kind === ts.SyntaxKind.FalseKeyword) return false;
			if (node.kind === ts.SyntaxKind.NullKeyword) return null;
			if (ts.isIdentifier(node)) {
				const key = symbol(node);
				if (scope.has(key)) return scope.get(key).constant;
				const definitions = bindings.get(key);
				if (definitions?.length === 1 && !mutated.has(key)) {
					let result = scalar(definitions[0].expression, scope, seen);
					for (const part of definitions[0].path) result = constantMember(result, keyOf(part, scope, seen));
					return result;
				}
				return node.text === "undefined" && !checker.getSymbolAtLocation(node) ? undefined : UNKNOWN;
			}
			if (ts.isPrefixUnaryExpression(node)) {
				const operand = scalar(node.operand, scope, seen);
				if (operand === UNKNOWN) return UNKNOWN;
				if (node.operator === ts.SyntaxKind.ExclamationToken) return !operand;
				if (!coercible(operand)) return UNKNOWN;
				if (node.operator === ts.SyntaxKind.MinusToken) return -operand;
				if (node.operator === ts.SyntaxKind.PlusToken) return +operand;
			}
			if (ts.isArrayLiteralExpression(node)) return node.elements.map(element => scalar(element, scope, seen));
			if (ts.isObjectLiteralExpression(node)) {
				const result = Object.create(null);
				for (const property of node.properties) {
					if (!ts.isPropertyAssignment(property)) return UNKNOWN;
					const key = keyOf(property.name, scope, seen);
					if (key === UNKNOWN) return UNKNOWN;
					result[key] = scalar(property.initializer, scope, seen);
				}
				return result;
			}
			if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
				const constant = checker.getConstantValue(node);
				if (constant !== undefined) return constant;
				const target = scalar(node.expression, scope, seen);
				const key = ts.isPropertyAccessExpression(node) ? node.name.text : propertyKey(scalar(node.argumentExpression, scope, seen));
				return constantMember(target, key);
			}
			if (ts.isBinaryExpression(node)) {
				const left = scalar(node.left, scope, seen), right = scalar(node.right, scope, seen);
				if (left === UNKNOWN || right === UNKNOWN) return UNKNOWN;
				if (node.operatorToken.kind === ts.SyntaxKind.CommaToken) return right;
				if (!coercible(left) || !coercible(right)) return UNKNOWN;
				if ([ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken].includes(node.operatorToken.kind) &&
					(left !== null && typeof left === "object" || right !== null && typeof right === "object")) return UNKNOWN;
				switch (node.operatorToken.kind) {
					case ts.SyntaxKind.PlusToken: return left + right;
					case ts.SyntaxKind.MinusToken: return left - right;
					case ts.SyntaxKind.AsteriskToken: return left * right;
					case ts.SyntaxKind.EqualsEqualsEqualsToken: return left === right;
					case ts.SyntaxKind.ExclamationEqualsEqualsToken: return left !== right;
					case ts.SyntaxKind.CommaToken: return right;
				}
			}
			if (ts.isConditionalExpression(node)) {
				const condition = scalar(node.condition, scope, seen);
				if (condition !== UNKNOWN) return scalar(condition ? node.whenTrue : node.whenFalse, scope, seen);
			}
			if (ts.isCallExpression(node)) {
				const fn = functionOf(node.expression);
				if (fn) {
					const bound = new Map(scope);
					fn.parameters.forEach((parameter, index) => {
						if (ts.isIdentifier(parameter.name)) bound.set(symbol(parameter.name), { origin: 0, constant: scalar(node.arguments[index] ?? parameter.initializer, scope, seen) });
					});
					const values = returns(fn).map(expression => scalar(expression, bound, seen));
					if (values.length && values[0] !== UNKNOWN && values.every(value => Object.is(value, values[0]))) return values[0];
				}
				const path = builtin(node.expression, new Set(), scope);
				if (path === "Object.freeze") return scalar(node.arguments[0], scope, seen);
				const member = rawAccess(node.expression, scope);
				if (member?.key === "join") {
					const array = scalar(member.target, scope, seen), separator = node.arguments.length ? scalar(node.arguments[0], scope, seen) : undefined;
					if (Array.isArray(array) && coercible(array) && (separator === undefined || typeof separator === "string")) return array.join(separator);
				}
			}
			return UNKNOWN;
		};
		const builtin = (value, seen = new Set(), scope = EMPTY) => {
			const node = unwrap(value);
			if (!node || seen.has(node)) return undefined;
			seen = new Set(seen).add(node);
			if (ts.isIdentifier(node)) {
				if (!checker.getSymbolAtLocation(node) && ["Reflect", "Object", "Array", "Boolean", "Number", "String", "JSON", "Math"].includes(node.text)) return node.text;
				const definitions = bindings.get(symbol(node));
				if (definitions?.length === 1) {
					const base = builtin(definitions[0].expression, seen, scope);
					const path = definitions[0].path.map(part => keyOf(part, scope));
					if (base && !path.includes(UNKNOWN)) return [base, ...path].join(".");
				}
			}
			const member = rawAccess(node, scope);
			const base = member && builtin(member.target, seen, scope);
			return base && member.key !== UNKNOWN ? base + "." + member.key : undefined;
		};
		const rawAccess = (value, scope = EMPTY) => {
			const node = unwrap(value);
			if (!node) return undefined;
			if (ts.isPropertyAccessExpression(node)) return { target: node.expression, key: node.name.text };
			if (ts.isElementAccessExpression(node)) return { target: node.expression, key: propertyKey(scalar(node.argumentExpression, scope)), keyExpression: node.argumentExpression };
		};
		const access = (value, scope = EMPTY) => {
			const node = unwrap(value), member = rawAccess(node, scope);
			if (member) return member;
			if (node && ts.isCallExpression(node) && builtin(node.expression, new Set(), scope) === "Reflect.get" && node.arguments.length >= 2) {
				return { target: node.arguments[0], key: propertyKey(scalar(node.arguments[1], scope)), keyExpression: node.arguments[1] };
			}
		};
		const functionOf = (value, seen = new Set()) => {
			const node = unwrap(value);
			if (!node || seen.has(node)) return undefined;
			seen = new Set(seen).add(node);
			if (isFunction(node)) return node;
			if (ts.isIdentifier(node)) {
				if (functions.has(symbol(node))) return functions.get(symbol(node));
				const definitions = bindings.get(symbol(node));
				if (definitions?.length === 1 && !definitions[0].path.length) return functionOf(definitions[0].expression, seen);
			}
		};
		const returns = fn => {
			if (!fn.body) return [];
			if (!ts.isBlock(fn.body)) return [fn.body];
			const expressions = [];
			walk(fn.body, node => { if (ts.isReturnStatement(node) && node.expression) expressions.push(node.expression); }, false);
			return expressions;
		};
		const resultDependencies = fn => {
			const expressions = returns(fn);
			if (fn.body) walk(fn.body, node => {
				if (ts.isIfStatement(node) && [node.thenStatement, node.elseStatement].some(branch => has(branch, ts.isReturnStatement))) expressions.push(node.expression);
			}, false);
			return expressions;
		};
		const scopeFor = (fn, args, scope, seen) => {
			const bound = new Map(scope);
			fn.parameters.forEach((parameter, index) => {
				if (ts.isIdentifier(parameter.name)) bound.set(symbol(parameter.name), { origin: origins(args[index], scope, seen), constant: scalar(args[index], scope) });
			});
			return bound;
		};
		const readOrigin = (member, scope, seen) => {
			const own = member.key === "id" ? ID : member.key === "schema" ? SCHEMA : member.key === "issues" ? ISSUES : member.key === UNKNOWN ? INDEXED : 0;
			return own | origins(member.target, scope, seen);
		};
		const origins = (value, scope = EMPTY, seen = new Set()) => {
			const node = unwrap(value);
			if (!node || seen.has(node)) return 0;
			seen = new Set(seen).add(node);
			if (ts.isIdentifier(node)) {
				const key = symbol(node);
				if (scope.has(key)) return scope.get(key).origin;
				return (bindings.get(key) ?? []).reduce((flags, definition) => {
					let result = origins(definition.expression, scope, seen);
					for (const part of definition.path) {
						const key = keyOf(part, scope);
						result |= key === "id" ? ID : key === "schema" ? SCHEMA : key === "issues" ? ISSUES : 0;
					}
					return flags | result;
				}, 0);
			}
			const member = access(node, scope);
			if (member) return readOrigin(member, scope, seen);
			if (isFunction(node)) return 0;
			if (ts.isCallExpression(node)) {
				const fn = functionOf(node.expression);
				if (fn) {
					const bound = scopeFor(fn, node.arguments, scope, seen);
					return resultDependencies(fn).reduce((flags, expression) => flags | origins(expression, bound, seen), 0);
				}
				const path = builtin(node.expression, new Set(), scope);
				if (["Boolean", "Number", "String", "Object.keys", "Object.values", "Object.entries", "Object.hasOwn", "Array.isArray", "JSON.stringify"].includes(path) || path?.startsWith("Math.")) {
					return node.arguments.reduce((flags, argument) => flags | origins(argument, scope, seen), 0);
				}
				const method = rawAccess(node.expression, scope);
				if (method?.key === "map") {
					const callback = functionOf(node.arguments[0]);
					const flags = callback ? returns(callback).reduce((flags, expression) => flags | origins(expression, scope, seen), 0) : 0;
					return origins(method.target, scope, seen) | (flags & ID ? IDS : 0);
				}
				if (["indexOf", "lastIndexOf", "includes", "slice", "concat", "filter", "split", "join", "trim", "reverse"].includes(method?.key)) return origins(method.target, scope, seen);
				// An unknown/native result is not a copy of its argument origins.
				return 0;
			}
			let flags = 0;
			ts.forEachChild(node, child => { if (!ts.isTypeNode(child)) flags |= origins(child, scope, seen); });
			return flags;
		};
		const statusNames = new Set(["active", "previous", "upcoming"]);
		const statusOptions = (node, seen = new Set()) => {
			node = unwrap(node);
			if (!node || seen.has(node)) return new Set();
			seen = new Set(seen).add(node);
			const value = scalar(node), found = new Set();
			const add = value => {
				if (typeof value === "string" && statusNames.has(value)) found.add(value);
				else if (Array.isArray(value)) value.forEach(add);
				else if (value && typeof value === "object") Object.values(value).forEach(add);
			};
			add(value);
			if (found.size) return found;
			if (ts.isConditionalExpression(node)) for (const branch of [node.whenTrue, node.whenFalse]) for (const name of statusOptions(branch, seen)) found.add(name);
			const member = access(node);
			if (member) for (const name of statusOptions(member.target, seen)) found.add(name);
			if (ts.isIdentifier(node)) for (const definition of bindings.get(symbol(node)) ?? []) for (const name of statusOptions(definition.expression, seen)) found.add(name);
			return found;
		};
		const positional = (value, scope = EMPTY, seen = new Set()) => {
			const node = unwrap(value);
			if (!node || seen.has(node)) return false;
			seen = new Set(seen).add(node);
			if (ts.isIdentifier(node)) return (bindings.get(symbol(node)) ?? []).some(definition => positional(definition.expression, scope, seen));
			if (ts.isCallExpression(node)) {
				const fn = functionOf(node.expression);
				if (fn) {
					const bound = scopeFor(fn, node.arguments, scope, seen);
					return resultDependencies(fn).some(expression => positional(expression, bound, seen));
				}
				return builtin(node.expression)?.startsWith("Math.") && node.arguments.some(argument => positional(argument, scope, seen));
			}
			if (ts.isBinaryExpression(node) && [ts.SyntaxKind.LessThanToken, ts.SyntaxKind.LessThanEqualsToken, ts.SyntaxKind.GreaterThanToken, ts.SyntaxKind.GreaterThanEqualsToken, ts.SyntaxKind.MinusToken].includes(node.operatorToken.kind) &&
				scalar(node.left, scope) === UNKNOWN && scalar(node.right, scope) === UNKNOWN) return true;
			let found = false;
			ts.forEachChild(node, child => { if (!isFunction(child) && !ts.isTypeNode(child) && positional(child, scope, seen)) found = true; });
			return found;
		};
		const successProperty = property => ts.isPropertyAssignment(property) && keyOf(property.name) === "success";
		const validationShape = node => ts.isObjectLiteralExpression(node) && node.properties.some(property => successProperty(property) && typeof scalar(property.initializer) === "boolean");
		const producesValidation = (node, seen = new Set()) => {
			if (!node || seen.has(node) || isFunction(node) || ts.isTypeNode(node)) return false;
			seen = new Set(seen).add(node);
			if (validationShape(node)) return true;
			if (ts.isCallExpression(node)) {
				const fn = functionOf(node.expression);
				if (fn && returns(fn).some(expression => producesValidation(expression, seen))) return true;
			}
			let found = false;
			ts.forEachChild(node, child => { if (producesValidation(child, seen)) found = true; });
			return found;
		};
		const report = (node, responsibility) => violations.push({ file: relative(root, file), line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1, responsibility });
		walk(source, node => {
			if (ts.isCallExpression(node)) {
				const method = rawAccess(node.expression);
				if (["indexOf", "lastIndexOf", "includes"].includes(method?.key) && origins(method.target) & IDS) report(node, "step lookup is implemented in JavaScript/TypeScript");
			}
			if (ts.isIfStatement(node) && origins(node.expression) & INDEXED && has(node.thenStatement, child => ts.isThrowStatement(child) && child.getText(source).includes("No match handler"))) report(node, "matching handler selection is implemented in JavaScript/TypeScript");
			if (ts.isBinaryExpression(node) && [ts.SyntaxKind.EqualsEqualsToken, ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.ExclamationEqualsToken, ts.SyntaxKind.ExclamationEqualsEqualsToken].includes(node.operatorToken.kind) && (origins(node.left) | origins(node.right)) & ID) {
				for (let parent = node.parent; parent; parent = parent.parent) {
					if (ts.isForStatement(parent) || ts.isForOfStatement(parent) || ts.isForInStatement(parent) || ts.isWhileStatement(parent) || ts.isDoStatement(parent) ||
						ts.isCallExpression(parent) && ["find", "findIndex", "some", "filter", "every", "reduce"].includes(rawAccess(parent.expression)?.key)) {
						report(node, "step lookup is implemented in JavaScript/TypeScript"); break;
					}
				}
			}
			if (successProperty(node) && origins(node.initializer) & (SCHEMA | ISSUES)) report(node, "validation dispatch/result decisions are implemented in JavaScript/TypeScript");
			if (!isFunction(node) || !node.body) return;
			const names = new Set(), decisions = [];
			const collect = expression => { for (const name of statusOptions(expression)) names.add(name); };
			walk(node.body, child => {
				if (ts.isReturnStatement(child)) collect(child.expression);
				if (ts.isConditionalExpression(child)) {
					const choices = new Set([...statusOptions(child.whenTrue), ...statusOptions(child.whenFalse)]);
					if (choices.size) { choices.forEach(name => names.add(name)); decisions.push(child.condition); }
				}
				const member = access(child);
				if (member?.keyExpression && statusOptions(member.target).size) { collect(member.target); decisions.push(member.keyExpression); }
				if (ts.isIfStatement(child) && [child.thenStatement, child.elseStatement].some(branch => has(branch, value => ts.isReturnStatement(value) && statusOptions(value.expression).size > 0))) decisions.push(child.expression);
			}, false);
			collect(node.body);
			if (names.size === 3 && decisions.some(expression => positional(expression))) report(node, "positional status derivation is implemented in JavaScript/TypeScript");
			if (has(node.body, child => {
				if (!ts.isIfStatement(child) && !ts.isConditionalExpression(child)) return false;
				const condition = ts.isIfStatement(child) ? child.expression : child.condition;
				const branches = ts.isIfStatement(child) ? [child.thenStatement, child.elseStatement] : [child.whenTrue, child.whenFalse];
				return origins(condition) & (SCHEMA | ISSUES) && branches.some(branch => producesValidation(branch));
			})) report(node, "validation dispatch/result decisions are implemented in JavaScript/TypeScript");
		});
	}
	return violations;
}

export function assertRustResponsibilities(root) {
	const violations = rustResponsibilityViolations(root);
	assert.equal(violations.length, 0, "migrated responsibilities must execute in Rust:\n" +
		violations.map(value => value.file + ":" + value.line + ": " + value.responsibility).join("\n"));
}
