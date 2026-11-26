'use strict';

// Parse source as data; never load candidate modules or compiler options.
const fs = require('node:fs');
const ts = require(require.resolve('typescript', {
  paths: ['/opt/verifier-tools', '/workspace/repo']
}));
const { path, before, after } = JSON.parse(fs.readFileSync(0, 'utf8'));

function signature(text) {
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true);
  if (source.parseDiagnostics.length) throw new Error('invalid test source');
  function tree(node) {
    const children = node.getChildren(source);
    return [node.kind, children.length ? children.map(tree) : node.getText(source)];
  }
  // Comments can contain compiler/coverage directives. Preserve their content
  // and order rather than treating them as disposable whitespace.
  const scanner = ts.createScanner(ts.ScriptTarget.Latest, false, source.languageVariant, text);
  const comments = [];
  let tokenCount = 0;
  for (let kind = scanner.scan(); kind !== ts.SyntaxKind.EndOfFileToken; kind = scanner.scan()) {
    if (kind === ts.SyntaxKind.SingleLineCommentTrivia || kind === ts.SyntaxKind.MultiLineCommentTrivia) {
      comments.push([tokenCount, scanner.getTokenText()]);
    } else if (kind > ts.SyntaxKind.LastTriviaToken) {
      tokenCount++;
    }
  }
  return JSON.stringify([tree(source), comments]);
}

try {
  process.stdout.write(signature(before) === signature(after) ? 'equivalent' : 'different');
} catch (_) {
  process.stdout.write('different');
}
