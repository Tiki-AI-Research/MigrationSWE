#!/bin/sh
set -eu
cd /workspace/repo
go version
go build ./...
go test ./...
gotestsum --junitfile /logs/native-report.xml -- ./...
set +e
go run /assets/harness.go /logs/result.json
status=$?
exit $status
