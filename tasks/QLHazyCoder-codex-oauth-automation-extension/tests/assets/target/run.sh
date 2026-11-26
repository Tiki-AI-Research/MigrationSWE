#!/bin/sh
set -eu
cd /workspace/repo
node --version
npm --version
python3 --version
python3 -c 'import flask; print(flask.__version__)'
python3 -m compileall -q scripts
npm test
node --test --test-reporter=junit --test-reporter-destination=/logs/native-report.xml 'tests/*.test.js'
set +e
/assets/target/runner /logs/result.json
status=$?
exit $status
