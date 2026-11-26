#!/bin/sh
set -eu
exec .venv/bin/python /assets/source/runner.py /logs/verifier/source-result.json
