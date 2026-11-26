#!/bin/sh
set -eu
cd /workspace/repo
go version
go build ./...
go test ./...
set +e
/assets/target/runner /logs/result.json
status=$?
exit $status
