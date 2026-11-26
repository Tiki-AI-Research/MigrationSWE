#!/bin/sh
set -eu
cd /workspace/repo
go version
ruby --version
go build -mod=vendor -buildvcs=false -o rubyfmt ./cmd/rubyfmt
go test ./...
gotestsum --junitfile /logs/native-report.xml -- ./...
set +e
python3 /assets/harness.py /logs/result.json
status=$?
exit $status
