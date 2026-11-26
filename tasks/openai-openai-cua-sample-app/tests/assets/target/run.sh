#!/bin/sh
set -eu
cd /workspace/repo
node --version
pnpm --version
python3 --version
pnpm --filter @cua-sample/runner build
pnpm --filter @cua-sample/runner typecheck
pnpm --filter @cua-sample/runner test --testTimeout=15000
pnpm --filter @cua-sample/runner exec vitest run --testTimeout=15000 --reporter=junit --outputFile=/logs/native-report.xml
set +e
node /assets/harness.mjs /logs/result.json
status=$?
exit $status
