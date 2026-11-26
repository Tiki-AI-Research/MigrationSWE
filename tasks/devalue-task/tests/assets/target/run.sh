#!/bin/sh
set -eu
cd "${MIGRATION_REPO_ROOT:-/workspace/repo}"
export MIGRATION_REPO_ROOT="$(pwd)"
export MIGRATION_SIDE=target
node --version
pnpm --version
pnpm run build
pnpm test
pnpm run test:types
exec node /assets/harness.mjs "${1:-/logs/result.json}"
