#!/bin/sh
set -eu
cd /workspace/repo
node --version
npm --version
npm run build
npm test
set +e
node /assets/harness.mjs /logs/result.json
status=$?
exit $status
