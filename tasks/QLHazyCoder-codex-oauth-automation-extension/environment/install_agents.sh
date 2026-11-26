#!/usr/bin/env bash
#
#
#   COPY install_agents.sh /tmp/install_agents.sh
#   RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh
#
#   NPM_REGISTRY / CLAUDE_CODE_VERSION / CODEX_VERSION
#   NODE22_VERSION / NODE22_X64_SHA256 / NODE22_ARM64_SHA256
set -euo pipefail

NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}"
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.295}"
CODEX_VERSION="${CODEX_VERSION:-0.154.0}"
NODE22_VERSION="${NODE22_VERSION:-22.22.0}"
NODE22_X64_SHA256="${NODE22_X64_SHA256:-9aa8e9d2298ab68c600bd6fb86a6c13bce11a4eca1ba9b39d79fa021755d7c37}"
NODE22_ARM64_SHA256="${NODE22_ARM64_SHA256:-1bf1eb9ee63ffc4e5d324c0b9b62cf4a289f44332dfef9607cea1a0d9596ba6f}"

if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        ca-certificates curl xz-utils libstdc++6 git procps ripgrep
    rm -rf /var/lib/apt/lists/*
else
    for tool in curl xz tar sha256sum; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "install_agents: missing tool '$tool' and no apt-get available" >&2
            exit 1
        }
    done
fi

if [ ! -x /opt/node22/bin/node ] || \
   [ "$(/opt/node22/bin/node --version 2>/dev/null || true)" != "v${NODE22_VERSION}" ]; then
    case "${TARGETARCH:-amd64}" in
        amd64) node22_arch=x64;   node22_sha="${NODE22_X64_SHA256}" ;;
        arm64) node22_arch=arm64; node22_sha="${NODE22_ARM64_SHA256}" ;;
        *)     node22_arch=x64;   node22_sha="${NODE22_X64_SHA256}" ;;
    esac
    curl -fsSL "https://nodejs.org/dist/v${NODE22_VERSION}/node-v${NODE22_VERSION}-linux-${node22_arch}.tar.xz" \
        -o /tmp/node22.tar.xz
    printf '%s  %s\n' "$node22_sha" /tmp/node22.tar.xz | sha256sum -c -
    mkdir -p /opt/node22
    tar -xJf /tmp/node22.tar.xz -C /opt/node22 --strip-components=1
    test "$(/opt/node22/bin/node --version)" = "v${NODE22_VERSION}"
    rm -f /tmp/node22.tar.xz
fi

export PATH="/opt/node22/bin:${PATH}"
export NPM_CONFIG_OFFLINE=false
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-/opt/npm-cache}"
mkdir -p "$NPM_CONFIG_CACHE"
npm config delete proxy 2>/dev/null || true
npm config delete https-proxy 2>/dev/null || true

npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    --registry="$NPM_REGISTRY" --noproxy="*"
npm install -g "@openai/codex@${CODEX_VERSION}" \
    --registry="$NPM_REGISTRY" --noproxy="*"

CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
[ -n "$CLAUDE_BIN" ] || CLAUDE_BIN="/opt/node22/bin/claude"
[ -x "$CLAUDE_BIN" ] || { echo "install_agents: claude executable not found" >&2; exit 1; }
[ "$CLAUDE_BIN" = /usr/local/bin/claude ] || ln -sf "$CLAUDE_BIN" /usr/local/bin/claude

cat > /usr/local/bin/codex <<'EOF'
#!/bin/sh
exec /opt/node22/bin/node /opt/node22/lib/node_modules/@openai/codex/bin/codex.js "$@"
EOF
chmod 755 /usr/local/bin/codex

# Model, endpoint and credentials are supplied by Harbor at runtime.

claude --version
codex --version
echo "install_agents: claude-code ${CLAUDE_CODE_VERSION} + codex ${CODEX_VERSION} installed (node ${NODE22_VERSION})"
