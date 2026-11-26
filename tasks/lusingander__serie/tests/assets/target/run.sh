#!/bin/sh
set -eu
cd /workspace/repo
go version
git --version
go env GOMODCACHE GOCACHE GOFLAGS
go build ./...
go test ./...
gotestsum --junitfile=/logs/native-report.xml --format=standard-verbose -- ./...
set +e
/assets/target/runner /logs/result.json
status=$?
exit $status
