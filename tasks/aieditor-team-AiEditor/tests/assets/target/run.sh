#!/bin/sh
set -eu
cd /workspace/repo
node --version
npm --version
npm run build
npm run test:run
npm run test:run -- --reporter=junit --outputFile=/logs/native-report.xml
set +e
node /assets/harness.mjs /logs/result.json
status=$?
exit $status
