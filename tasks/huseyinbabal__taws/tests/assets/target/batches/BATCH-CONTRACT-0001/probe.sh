#!/bin/sh
# Executable public-contract probe entrypoint for the Go target.
#
# Usage:
#   probe.sh [repo-dir]
#
# The probe prints a JSON object keyed by CONTRACT observation id and never
# blocks: every build/test command it issues is wrapped in `timeout`.
# target/runner (created only by the INTEGRATION batch) is expected to invoke
# this script and merge its output.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR="${1:-${CONTRACT_REPO:-/workspace/repo}}"

exec python3 "${SCRIPT_DIR}/probe.py" "${REPO_DIR}"
