#!/bin/sh
set -eu
cd /workspace/repo
node --version
pnpm --version
node -e 'require.resolve('"'"'markdown-it'"'"')'
pnpm run stub
pnpm run gen:version
pnpm exec vitest run
pnpm exec vitest run --reporter=junit --outputFile=/logs/native-report.xml
set +e
node /assets/harness.mjs /logs/result.json
status=$?
exit $status
