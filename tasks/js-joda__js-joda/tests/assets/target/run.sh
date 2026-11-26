#!/bin/sh
set -u
cd /workspace/repo || exit 1
failed_status=0
node --version
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
npm --version
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
npm --prefix packages/core run build-dist
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
npm --prefix packages/core run test
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
npm --prefix packages/core run test -- --reporter xunit --reporter-option 'output={report}'
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
set +e
node /assets/harness.js /logs/result.json
status=$?
if [ -s /logs/result.json ]; then exit "$status"; fi
echo "harness produced no report" >&2
if [ "$failed_status" -ne 0 ]; then exit "$failed_status"; fi
if [ "$status" -eq 0 ]; then exit 125; fi
exit "$status"
