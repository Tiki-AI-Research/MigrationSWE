#!/bin/sh
set -eu
cd /workspace/repo
git --version
npm ls hono @hono/node-server --depth=0
npm run build:ui
npm test
npm test -- --reporter=junit --outputFile=/logs/native-report.xml
set +e
node /assets/harness.js /logs/result.json
status=$?
exit $status
