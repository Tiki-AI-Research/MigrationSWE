#!/bin/sh
set -u
cd /workspace/repo || exit 1
failed_status=0
cargo metadata --offline --format-version 1
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
cargo build --offline
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
cargo test --offline
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
set +e
python3 /assets/target/harness.py /logs/result.json
status=$?
if [ -s /logs/result.json ]; then exit "$status"; fi
echo "harness produced no report" >&2
if [ "$failed_status" -ne 0 ]; then exit "$failed_status"; fi
if [ "$status" -eq 0 ]; then exit 125; fi
exit "$status"
