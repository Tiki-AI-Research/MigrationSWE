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

# Claude Code requires Node >=22; /opt/node14/bin comes first on the candidate
# PATH, so pin the Node 22 toolchain for this install.
export PATH="/opt/node22/bin:${PATH}"

npm config delete proxy 2>/dev/null || true
npm config delete https-proxy 2>/dev/null || true
npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" --registry="$NPM_REGISTRY" --noproxy="*"

CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
[ -n "$CLAUDE_BIN" ] || CLAUDE_BIN="/opt/node22/bin/claude"
[ -x "$CLAUDE_BIN" ] || { echo "claude executable not found" >&2; exit 1; }
[ "$CLAUDE_BIN" = /usr/local/bin/claude ] || ln -sf "$CLAUDE_BIN" /usr/local/bin/claude

claude --version
