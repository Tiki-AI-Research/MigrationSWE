#!/bin/sh
set -eu
cd /workspace/repo
node --version
node run-command.mjs build
set +e
node /assets/target/runner.mjs /logs/result.json
status=$?
exit $status
