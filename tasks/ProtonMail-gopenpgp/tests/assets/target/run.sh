#!/bin/sh
set -eu
cd /workspace/repo
go version
go build ./...
go test -v -race ./...
gotestsum --junitfile=/logs/native-report.xml -- -count=1 ./...
set +e
python3 /assets/harness.py /logs/result.json
status=$?
exit $status
