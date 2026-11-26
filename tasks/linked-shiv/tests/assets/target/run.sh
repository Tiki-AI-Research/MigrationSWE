#!/bin/sh
set -eu
exec .venv/bin/python /assets/target/runner.py /logs/verifier/result.json
