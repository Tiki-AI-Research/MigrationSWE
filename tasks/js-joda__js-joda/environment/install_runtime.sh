#!/usr/bin/env bash
# Build-time setup only: leave the candidate repository and its Node version intact.
set -euo pipefail

export PATH="/opt/node22/bin:$PATH"
export NPM_CONFIG_OFFLINE=false
export NPM_CONFIG_REGISTRY=https://registry.npmjs.org
export NPM_CONFIG_CACHE=/opt/offline-deps/npm

if ! command -v runuser > /dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends util-linux
fi

# npm install can request only full metadata. npm cache add fetches the tarball
# using abbreviated metadata, then also fetches full metadata. Warm both while
# online so the agent's offline cache check can resolve either request format.
# Fail the image build if the promised offline dependency cannot be prepared.
mkdir -p /opt/offline-deps
npm install --prefix /opt/offline-deps --no-save --no-audit --no-fund @js-joda/core@6.3.2
npm cache add @js-joda/core@6.3.2 --offline=false
chown -R agent:agent /opt/offline-deps
runuser -u agent -- env PATH="$PATH" NPM_CONFIG_OFFLINE=true \
    npm cache add @js-joda/core@6.3.2 --offline
test "$(runuser -u agent -- env PATH="$PATH" NPM_CONFIG_OFFLINE=true \
    npm view @js-joda/core@6.3.2 version --offline)" = 6.3.2

# Bundled browsers work on both Debian and Ubuntu without snap or runtime
# downloads. Keep this tooling outside the repository and its dependency cache.
export NPM_CONFIG_CACHE=/opt/harbor-browsers/npm
export PLAYWRIGHT_BROWSERS_PATH=/opt/harbor-browsers/cache
npm install --prefix /opt/harbor-browsers --no-save --no-audit --no-fund playwright@1.56.1
node /opt/harbor-browsers/node_modules/playwright/cli.js install --with-deps chromium firefox

# Karma keeps both original launchers. Only supply executable paths and flags
# needed inside Docker, where user namespaces and /dev/shm may be restricted.
node <<'NODE'
const fs = require('fs');
const {chromium, firefox} = require('/opt/harbor-browsers/node_modules/playwright');
for (const [name, browser, setup, flags] of [
    ['chromium', chromium, '', '--no-sandbox --disable-dev-shm-usage'],
    ['firefox', firefox, 'export MOZ_DISABLE_CONTENT_SANDBOX=1\n', ''],
]) {
    const binary = browser.executablePath();
    fs.accessSync(binary, fs.constants.X_OK);
    const quotedBinary = "'" + binary.replace(/'/g, "'\\''") + "'";
    fs.writeFileSync('/usr/local/bin/harbor-' + name,
        '#!/bin/sh\n' + setup + 'exec ' + quotedBinary + ' ' + flags + ' "$@"\n',
        {mode: 0o755});
}
NODE
chmod -R a+rX /opt/harbor-browsers
runuser -u agent -- /usr/local/bin/harbor-chromium --version
runuser -u agent -- /usr/local/bin/harbor-firefox --version
rm -rf /var/lib/apt/lists/*
