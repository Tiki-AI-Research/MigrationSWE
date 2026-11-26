#!/bin/bash
set -euo pipefail
cd /workspace/repo
test "$(git rev-parse HEAD)" = 7a963d16ae5c17b430b57b307d3f3ba74c0cbb1f
git checkout -b feature/solution
git apply --whitespace=nowarn /solution/solution.patch
grammar_snapshot="$(mktemp /tmp/jssip-reference-grammar.XXXXXX)"
trap 'rm -f "$grammar_snapshot"' EXIT
cp src/Grammar.js "$grammar_snapshot"
node npm-scripts.mjs grammar
cmp "$grammar_snapshot" src/Grammar.js
node npm-scripts.mjs grammar
cmp "$grammar_snapshot" src/Grammar.js
npm test
git add -A
git -c user.name="oracle" -c user.email="oracle@local" commit -q --no-verify -m "Apply reference solution"
