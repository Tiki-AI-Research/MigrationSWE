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
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.295}"

# The harbor bundle ships Node 22 beside the candidate runtime. Candidate PATH
# starts at /opt/envpipe/runtime/bin, so install Claude against the bundle Node
# and expose only the claude wrapper on the default PATH.
HARBOR_NODE="${HARBOR_NODE:-/opt/harbor-agent/node}"
export PATH="${HARBOR_NODE}/bin:${PATH}"

command -v npm >/dev/null 2>&1 || { echo "npm not found under ${HARBOR_NODE}/bin" >&2; exit 1; }

npm config delete proxy 2>/dev/null || true
npm config delete https-proxy 2>/dev/null || true
npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    --prefix /opt/harbor-agent/claude \
    --registry="$NPM_REGISTRY" \
    --noproxy="*"

CLAUDE_BIN="$(find /opt/harbor-agent/claude -type f -path '*/@anthropic-ai/claude-code/*' -name claude -perm -111 | head -n 1)"
[ -n "$CLAUDE_BIN" ] || CLAUDE_BIN="$(find /opt/harbor-agent/claude -type f -name 'claude.exe' -perm -111 | head -n 1)"
[ -n "$CLAUDE_BIN" ] || { echo "claude executable not found under /opt/harbor-agent/claude" >&2; exit 1; }
ln -sf "$CLAUDE_BIN" /usr/local/bin/claude

claude --version
