#!/bin/sh
set -eu
cd /workspace/repo
node --version
pnpm --version
cargo --version
rustc --version
pnpm install --frozen-lockfile --offline --store-dir /opt/migration/dependencies/pnpm-store
pnpm --filter @stepperize/core build
pnpm --filter @stepperize/core test
pnpm --filter @stepperize/react test
pnpm --filter @stepperize/core exec vitest run --reporter=junit --outputFile=/logs/native-report.xml
set +e
node /assets/harness.mjs /logs/result.json
status=$?
exit $status
