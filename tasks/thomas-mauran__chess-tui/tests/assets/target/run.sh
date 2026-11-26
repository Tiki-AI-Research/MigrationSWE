#!/bin/sh
set -eu
cd /workspace/repo
go version
go env GOVERSION
go build ./...
go test -run=^$ ./...
set +e
/assets/target/runner /logs/result.json
status=$?
exit $status
