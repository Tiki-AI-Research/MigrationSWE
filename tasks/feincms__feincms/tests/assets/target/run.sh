#!/bin/sh
set -u
cd /workspace/repo || exit 1
failed_status=0
python --version
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
python -m django --version
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
python tests/manage.py check
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
python -Wd -m coverage run tests/manage.py test -v2 testapp
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
python -m pytest -m 'not e2e' --junitxml=/logs/native-report.xml tests/testapp/tests
command_status=$?
if [ "$command_status" -ne 0 ]; then failed_status=$command_status; fi
set +e
python3 /assets/target/runner.py /logs/result.json
status=$?
if [ -s /logs/result.json ]; then exit "$status"; fi
echo "harness produced no report" >&2
if [ "$failed_status" -ne 0 ]; then exit "$failed_status"; fi
if [ "$status" -eq 0 ]; then exit 125; fi
exit "$status"
