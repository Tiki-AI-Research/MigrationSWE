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
npm run build
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
npm run type-check
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
npm run lint:ci
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
npm test -- --ci
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
env 'JEST_JUNIT_OUTPUT_FILE={report}' npm test -- --ci --reporters=default --reporters=jest-junit
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
set +e
node /assets/harness.cjs /logs/result.json
status=$?
if [ -s /logs/result.json ]; then
  if [ "$failed_status" -ne 0 ]; then exit "$failed_status"; fi
  exit "$status"
fi
echo "harness produced no report" >&2
if [ "$failed_status" -ne 0 ]; then exit "$failed_status"; fi
if [ "$status" -eq 0 ]; then exit 125; fi
exit "$status"
