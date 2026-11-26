#!/bin/bash
set -euo pipefail

mode="${1:?expected online or offline}"
case "$mode" in
  online) offline=false ;;
  offline) offline=true ;;
  *) echo "unknown mode: $mode" >&2; exit 2 ;;
esac

repo="${AGENT_NPM_BASE_REPO:-/workspace/repo}"
node_roots="${AGENT_NPM_NODE_ROOTS:-/opt/node14/bin:/opt/node22/bin}"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
IFS=: read -r -a node_directories <<< "$node_roots"

for node_directory in "${node_directories[@]}"; do
  for scenario in minimal migration; do
    work="$scratch/$(basename "$(dirname "$node_directory")")-$scenario"
    mkdir -p "$work"
    if [ "$scenario" = minimal ]; then
      printf '%s\n' '{"name":"npm-cache-check","version":"1.0.0","dependencies":{"fastify":"4.28.1"}}' > "$work/package.json"
    else
      cp "$repo/package.json" "$repo/package-lock.json" "$work/"
      "$node_directory/node" - "$work/package.json" <<'NODE'
const fs = require('fs');
const file = process.argv[2];
const pkg = JSON.parse(fs.readFileSync(file, 'utf8'));
for (const name of ['express', 'express-basic-auth', 'express-fileupload']) delete pkg.dependencies[name];
pkg.dependencies.fastify = '4.28.1';
pkg.dependencies['@fastify/multipart'] = '8.3.1';
fs.writeFileSync(file, JSON.stringify(pkg, null, 2) + '\n');
NODE
    fi
    (
      cd "$work"
      export PATH="$node_directory:$PATH"
      export NPM_CONFIG_OFFLINE="$offline"
      npm install --ignore-scripts --no-audit --no-fund
      node -e "require.resolve('fastify'); if (process.argv[1] === 'migration') require.resolve('@fastify/multipart');" "$scenario"
    )
  done
done
