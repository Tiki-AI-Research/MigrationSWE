#!/bin/sh
set -eu
cd /workspace/repo
node --version
npm --version
npm run build
npm run test
set +e
node /assets/target/harness.mjs /logs/result.json
status=$?
exit $status
