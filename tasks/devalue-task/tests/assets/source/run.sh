#!/bin/sh
set -eu
cd "${MIGRATION_REPO_ROOT:-/workspace/repo}"
export MIGRATION_REPO_ROOT="$(pwd)"
export MIGRATION_SIDE=source
exec node /assets/harness.mjs "${1:-/logs/result.json}"
