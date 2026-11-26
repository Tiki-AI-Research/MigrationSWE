#!/bin/sh
set -eu
cd /workspace/repo
node --version
node --check server.js
npm run build
npm test
set +e
node /assets/target/runner.js /logs/result.json
status=$?
exit $status
