#!/bin/bash
set -euo pipefail

HOME="${HOME:-/root}"
export HOME

# OS dependencies are installed by the Dockerfile; this guard only matters if the
# script is ever run standalone.
if [ "${SKIP_APT_INSTALL:-0}" != "1" ] && command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends curl ca-certificates ripgrep libstdc++6
    rm -rf /var/lib/apt/lists/*
fi

NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}"
CODEX_VERSION="${CODEX_VERSION:-0.154.0}"

export PATH="/opt/node22/bin:${PATH}"

npm config delete proxy 2>/dev/null || true
npm config delete https-proxy 2>/dev/null || true
npm install -g "@openai/codex@${CODEX_VERSION}" --registry="$NPM_REGISTRY" --noproxy="*"

cat > /usr/local/bin/codex <<'EOF'
#!/bin/sh
# Pin the Node 22 toolchain: the upstream entrypoint is a Node >=16 ES module
# while /opt/node14/bin comes first on the candidate PATH.
exec /opt/node22/bin/node /opt/node22/lib/node_modules/@openai/codex/bin/codex.js "$@"
EOF
chmod 755 /usr/local/bin/codex

codex --version
