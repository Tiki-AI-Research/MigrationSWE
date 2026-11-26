#!/bin/sh
set -u
cd /workspace/repo || exit 1
failed_status=0
go version
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
go build ./...
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
go vet ./...
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
go test ./...
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
gotestsum --junitfile=/logs/native-report.xml -- ./...
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
set +e
/assets/target/runner /logs/result.json
status=$?
if [ -s /logs/result.json ]; then exit "$status"; fi
echo "harness produced no report" >&2
if [ "$failed_status" -ne 0 ]; then exit "$failed_status"; fi
if [ "$status" -eq 0 ]; then exit 125; fi
exit "$status"
