#!/bin/sh
set -eu
cd /workspace/repo
node --version
npm --version
npm run build -w partyserver
npm run build -w hono-party
npm run check:test -w partyserver
node_modules/.bin/vitest -r packages/partyserver/src/tests --watch false --reporter=junit --outputFile=/logs/native-report.xml
set +e
node /assets/harness.mjs /logs/result.json
status=$?
exit $status
