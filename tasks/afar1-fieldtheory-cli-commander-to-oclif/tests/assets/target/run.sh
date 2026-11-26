#!/bin/sh
set -eu
cd /workspace/repo
node --version
npm --version
git --version
npm run build
npm test
npm test -- --test-reporter=junit --test-reporter-destination=/logs/native-report.xml
set +e
node /assets/harness.mjs /logs/result.json
status=$?
exit $status
