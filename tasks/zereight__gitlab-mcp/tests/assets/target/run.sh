#!/bin/sh
set -eu
cd /workspace/repo
python --version
/workspace/repo/mcp-server/.venv/bin/python -c 'import mcp'
/workspace/repo/mcp-server/.venv/bin/python -m compileall -q mcp-server/omg_mcp
/workspace/repo/mcp-server/.venv/bin/python -m unittest discover -s mcp-server/tests -t mcp-server -v
set +e
/workspace/repo/mcp-server/.venv/bin/python /assets/run_tests.py /logs/result.json
status=$?
exit $status
