#!/bin/sh
set -eu
cd /workspace/repo
node --version
node node_modules/.bin/eslint --version
node node_modules/.bin/tsc --version
node node_modules/.bin/tsc -p .
node node_modules/.bin/babel --extensions .js,.ts src --out-dir lib --copy-files
node node_modules/.bin/rimraf --glob lib/__tests__ 'lib/**/__tests__'
node node_modules/.bin/jest --ci
set +e
node /assets/target/runner.js /logs/result.json
status=$?
exit $status
