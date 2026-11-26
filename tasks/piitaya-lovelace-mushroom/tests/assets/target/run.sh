#!/bin/sh
set -eu
cd /workspace/repo
node --version
npm --version
npm run build
set +e
node /assets/target/runner.js /logs/result.json
status=$?
exit $status
