#!/usr/bin/env bash
# Provision dependencies without changing tracked files in the task repository.
set -euo pipefail

repo=/workspace/repo
offline_root=/opt/offline-deps
export NPM_CONFIG_OFFLINE=false
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

mkdir -p "$offline_root/npm" "$offline_root/pnpm-store" "$PLAYWRIGHT_BROWSERS_PATH"
chown agent:agent "$offline_root"
chown -R agent:agent "$offline_root/npm" "$offline_root/pnpm-store"

seed_root=$(mktemp -d "$offline_root/prewarm.XXXXXX")
trap 'rm -rf "$seed_root"' EXIT
baseline_seed="$seed_root/baseline"
mkdir -p "$baseline_seed"
restore_manifests() {
    cp "$repo/package.json" "$repo/pnpm-lock.yaml" "$repo/pnpm-workspace.yaml" "$baseline_seed/"
    for workspace in console contracts javascript-app labs; do
        mkdir -p "$baseline_seed/$workspace"
        cp "$repo/$workspace/package.json" "$baseline_seed/$workspace/"
    done
    chown -R agent:agent "$seed_root"
}
restore_manifests

# Match the verifier's Express declaration package and reuse baseline Node types.
node_types_version=$(node -p "require('$repo/node_modules/@types/node/package.json').version")

as_agent() {
    runuser -u agent -- "$@"
}

# npm's cache is not interchangeable with pnpm's metadata or content store.
# Retain the original /opt/offline-deps/node_modules convenience location too.
as_agent npm install --prefix "$offline_root" --no-save --no-audit --no-fund \
    express@5.1.0 @types/express@5.0.6 "@types/node@$node_types_version" light-my-request@6.6.0
as_agent env CI=true pnpm --dir "$baseline_seed" install --frozen-lockfile --ignore-scripts
# pnpm remove requires config overrides for these install options.
as_agent pnpm --dir "$baseline_seed" --filter @cua-sample/runner remove --config.ignore-scripts=true fastify
as_agent pnpm --dir "$baseline_seed" --filter @cua-sample/runner add --ignore-scripts --save-exact express@5.1.0
as_agent pnpm --dir "$baseline_seed" --filter @cua-sample/runner add --ignore-scripts --save-dev --save-exact @types/express@5.0.6
as_agent pnpm --dir "$baseline_seed" --filter @cua-sample/runner add --ignore-scripts --save-exact light-my-request@6.6.0

# Exercise the actual offline workspace dependency change against disposable
# manifests. All repository source files and its lockfile remain at the baseline.
restore_manifests
as_agent env CI=true pnpm --dir "$baseline_seed" install --offline --frozen-lockfile --ignore-scripts
as_agent pnpm --dir "$baseline_seed" --filter @cua-sample/runner remove --config.offline=true --config.ignore-scripts=true fastify
as_agent pnpm --dir "$baseline_seed" --filter @cua-sample/runner add --offline --ignore-scripts --save-exact express@5.1.0
as_agent pnpm --dir "$baseline_seed" --filter @cua-sample/runner add --offline --ignore-scripts --save-dev --save-exact @types/express@5.0.6
as_agent pnpm --dir "$baseline_seed" --filter @cua-sample/runner add --offline --ignore-scripts --save-exact light-my-request@6.6.0

# Supply the same target runtime/types that are materialized in the verifier.
# These links are ignored node_modules entries, not changes to dependency metadata.
mkdir -p "$repo/javascript-app/node_modules/@types"
ln -s "$offline_root/node_modules/express" "$repo/javascript-app/node_modules/express"
ln -s "$offline_root/node_modules/@types/express" "$repo/javascript-app/node_modules/@types/express"
ln -s "$offline_root/node_modules/light-my-request" "$repo/javascript-app/node_modules/light-my-request"
chown -R agent:agent "$repo/javascript-app/node_modules/@types"
chown -h agent:agent "$repo/javascript-app/node_modules/express"
chown -h agent:agent "$repo/javascript-app/node_modules/light-my-request"

# Use the repository's locked Playwright version, including its OS dependencies.
pnpm --dir "$repo" --filter @cua-sample/runner playwright:install:with-deps
chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"
as_agent node <<'NODE'
const { createRequire } = require('node:module');
const fromRunner = createRequire('/workspace/repo/javascript-app/package.json');
if (fromRunner('express/package.json').version !== '5.1.0') {
  throw new Error('Offline Express version mismatch');
}
if (fromRunner('@types/express/package.json').version !== '5.0.6') {
  throw new Error('Offline Express declaration version mismatch');
}
(async () => {
  const browser = await fromRunner('playwright').chromium.launch({ headless: true });
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
NODE
