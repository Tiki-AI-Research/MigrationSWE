"use strict";

// Parse declarations and calls with the evaluator's compiler, never a compiler
// supplied by the candidate. Equivalent numeric aliases and optional chaining
// preserve the native/public contract.
const ts = require("/opt/migration/dependencies/node_modules/typescript");
const fs = require("fs");
const path = require("path");

function nodes(root) {
  const out = [];
  function visit(node) { out.push(node); ts.forEachChild(node, visit); }
  visit(root);
  return out;
}

function parse(text) {
  return ts.createSourceFile("candidate.tsx", text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}

function numeric(type) {
  if (!type) return false;
  if (ts.isParenthesizedTypeNode(type)) return numeric(type.type);
  if (ts.isUnionTypeNode(type)) {
    const significant = type.types.filter(t => t.kind !== ts.SyntaxKind.UndefinedKeyword &&
      !(ts.isLiteralTypeNode(t) && t.literal.kind === ts.SyntaxKind.NullKeyword));
    return significant.length === 1 && numeric(significant[0]);
  }
  if (type.kind === ts.SyntaxKind.NumberKeyword) return true;
  if (!ts.isTypeReferenceNode(type)) return false;
  const name = type.typeName.getText();
  if (name === "Double" || name === "CodegenTypes.Double") return true;
  return (name === "WithDefault" || name === "CodegenTypes.WithDefault") &&
    type.typeArguments?.length === 2 && numeric(type.typeArguments[0]) &&
    type.typeArguments[1].getText().replace(/\s/g, "") === "-1";
}

function captureRefSignature(text) {
  const source = parse(text);
  if (source.parseDiagnostics.length) return false;
  const spec = source.statements.find(s => ts.isInterfaceDeclaration(s) &&
    s.name.text === "Spec" && s.heritageClauses?.some(h =>
      h.types.some(t => t.expression.getText() === "TurboModule")));
  const member = spec?.members.find(m => m.name?.getText() === "captureRef");
  const fn = member && (ts.isPropertySignature(member) ? member.type : member);
  if (!fn || !(ts.isFunctionTypeNode(fn) || ts.isMethodSignature(fn))) return false;
  const args = fn.parameters;
  return args.length === 2 && numeric(args[0].type) &&
    ["Object", "object"].includes(args[1].type?.getText()) &&
    ts.isTypeReferenceNode(fn.type) && fn.type.typeName.getText() === "Promise" &&
    fn.type.typeArguments?.length === 1 &&
    fn.type.typeArguments[0].kind === ts.SyntaxKind.StringKeyword;
}

function releaseCaptureForwarded(text) {
  const source = parse(text);
  const fn = source.statements.find(s => ts.isFunctionDeclaration(s) && s.name?.text === "releaseCapture");
  if (!fn?.body || fn.parameters.length !== 1) return false;
  const parameter = fn.parameters[0].name.getText();
  const body = nodes(fn.body);
  const forwarded = body.some(n => ts.isCallExpression(n) &&
    ts.isPropertyAccessExpression(n.expression) &&
    n.expression.name.text === "releaseCapture" &&
    n.expression.expression.getText() === "RNViewShot" &&
    n.arguments[0]?.getText() === parameter);
  const checked = body.some(n => ts.isBinaryExpression(n) &&
    [ts.SyntaxKind.ExclamationEqualsEqualsToken, ts.SyntaxKind.EqualsEqualsEqualsToken].includes(n.operatorToken.kind) &&
    ts.isTypeOfExpression(n.left) && n.left.expression.getText() === parameter &&
    ts.isStringLiteral(n.right) && n.right.text === "string");
  return forwarded && checked;
}

function windowsErrorNormalized(text) {
  const source = parse(text);
  const all = nodes(source);
  const constants = new Set(all.filter(n => ts.isVariableDeclaration(n) &&
    n.initializer && ts.isStringLiteral(n.initializer) &&
    n.initializer.text === "E_UNABLE_TO_SNAPSHOT").map(n => n.name.getText()));
  const isCode = n => (ts.isStringLiteral(n) && n.text === "E_UNABLE_TO_SNAPSHOT") ||
    (ts.isIdentifier(n) && constants.has(n.text));
  const functions = all.filter(n => (ts.isFunctionDeclaration(n) && n.name) ||
    (ts.isPropertyAssignment(n) && (ts.isArrowFunction(n.initializer) || ts.isFunctionExpression(n.initializer))));
  const nameOf = n => n.name.getText();
  const bodies = new Map();
  for (const n of functions) {
    const name = nameOf(n);
    bodies.set(name, [...(bodies.get(name) || []), ts.isPropertyAssignment(n) ? n.initializer : n]);
  }
  function reachesCode(name, seen = new Set()) {
    if (seen.has(name)) return false;
    seen.add(name);
    return (bodies.get(name) || []).some(body => {
      const descendants = nodes(body);
      const caught = descendants.filter(n => ts.isCallExpression(n) &&
        ts.isPropertyAccessExpression(n.expression) && n.expression.name.text === "catch");
      if (caught.some(n => {
        const callback = n.arguments[0];
        if (!callback) return false;
        const parts = nodes(callback);
        const code = parts.some(p => ts.isPropertyAssignment(p) && p.name.getText() === "code" &&
          isCode(p.initializer)) ||
          parts.some(p => ts.isBinaryExpression(p) && p.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
            ts.isPropertyAccessExpression(p.left) && p.left.name.text === "code" &&
            isCode(p.right));
        return code && parts.some(p => ts.isThrowStatement(p));
      })) return true;
      return descendants.some(n => ts.isCallExpression(n) && ts.isIdentifier(n.expression) &&
        reachesCode(n.expression.text, new Set(seen)));
    });
  }
  return reachesCode("captureRef") && reachesCode("captureScreen");
}

function bridgeFacts(root) {
  const read = file => {
    try { return fs.readFileSync(path.join(root, file), "utf8"); } catch (_) { return ""; }
  };
  return {
    captureRefSignature: captureRefSignature(read("src/specs/NativeRNViewShot.ts")),
    releaseCaptureForwarded: releaseCaptureForwarded(read("src/index.tsx")),
    windowsErrorNormalized: windowsErrorNormalized(read("src/RNViewShot.ts")) ||
      windowsErrorNormalized(read("src/index.tsx")),
  };
}

module.exports = {bridgeFacts, captureRefSignature, releaseCaptureForwarded, windowsErrorNormalized};
