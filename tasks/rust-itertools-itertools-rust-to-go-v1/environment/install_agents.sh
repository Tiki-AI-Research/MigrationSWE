#!/usr/bin/env bash
# Preinstall Codex and Claude Code as in marwes-combine/tasks/environment.
# Keep their pinned Node 22 runtime separate from the project's Node runtime.
set -euo pipefail

NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}"
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.295}"
CODEX_VERSION="${CODEX_VERSION:-0.154.0}"
NODE22_VERSION="${NODE22_VERSION:-22.22.0}"
NODE22_X64_SHA256="${NODE22_X64_SHA256:-9aa8e9d2298ab68c600bd6fb86a6c13bce11a4eca1ba9b39d79fa021755d7c37}"
NODE22_ARM64_SHA256="${NODE22_ARM64_SHA256:-1bf1eb9ee63ffc4e5d324c0b9b62cf4a289f44332dfef9607cea1a0d9596ba6f}"
AGENT_USER="${AGENT_USER:-agent}"

if [ "$(id -u)" -ne 0 ]; then
    echo 'install_agents: run as root during image construction' >&2
    exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        ca-certificates curl xz-utils tar libstdc++6 git procps ripgrep util-linux
    rm -rf /var/lib/apt/lists/*
else
    for tool in curl xz tar sha256sum runuser; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "install_agents: missing '$tool' and no apt-get available" >&2
            exit 1
        }
    done
fi

# Verify the official archive before extracting it. Detect the build machine
# when Docker's optional TARGETARCH argument has not been declared.
if [ ! -x /opt/node22/bin/node ] || \
   [ "$(/opt/node22/bin/node --version 2>/dev/null || true)" != "v${NODE22_VERSION}" ]; then
    case "${TARGETARCH:-$(uname -m)}" in
        amd64|x86_64) node22_arch=x64; node22_sha="$NODE22_X64_SHA256" ;;
        arm64|aarch64) node22_arch=arm64; node22_sha="$NODE22_ARM64_SHA256" ;;
        *) echo 'install_agents: unsupported architecture' >&2; exit 1 ;;
    esac
    node22_archive=$(mktemp /tmp/harbor-node22.XXXXXX.tar.xz)
    trap 'rm -f "$node22_archive"' EXIT
    curl -fsSL "https://nodejs.org/dist/v${NODE22_VERSION}/node-v${NODE22_VERSION}-linux-${node22_arch}.tar.xz" \
        -o "$node22_archive"
    printf '%s  %s\n' "$node22_sha" "$node22_archive" | sha256sum -c -
    mkdir -p /opt/node22
    tar -xJf "$node22_archive" -C /opt/node22 --strip-components=1
    test "$(/opt/node22/bin/node --version)" = "v${NODE22_VERSION}"
    rm -f "$node22_archive"
    trap - EXIT
fi

export PATH="/opt/node22/bin:${PATH}"

# Override candidate-only offline/prefix/cache settings for these two installs.
# These flags apply only to this build step; the image's runtime ENV is kept.
npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    --prefix=/opt/node22 --cache=/opt/harbor-agent-npm-cache --offline=false \
    --registry="$NPM_REGISTRY" --noproxy="*" --no-audit --no-fund
npm install -g "@openai/codex@${CODEX_VERSION}" \
    --prefix=/opt/node22 --cache=/opt/harbor-agent-npm-cache --offline=false \
    --registry="$NPM_REGISTRY" --noproxy="*" --no-audit --no-fund

# Both CLIs must use Node 22 even when the candidate's node is older.
test -x /opt/node22/bin/claude
test -f /opt/node22/lib/node_modules/@openai/codex/bin/codex.js
# Replace inherited symlinks without overwriting their package entrypoints.
rm -f /usr/local/bin/claude /usr/local/bin/codex
cat > /usr/local/bin/claude <<'EOF'
#!/bin/sh
export PATH="/opt/node22/bin:${PATH}"
exec /opt/node22/bin/claude "$@"
EOF
cat > /usr/local/bin/codex <<'EOF'
#!/bin/sh
exec /opt/node22/bin/node /opt/node22/lib/node_modules/@openai/codex/bin/codex.js "$@"
EOF
chmod 0755 /usr/local/bin/claude /usr/local/bin/codex

# Model, endpoint and credentials are supplied by Harbor at runtime.

# Check actual CLI versions as both root and the rollout user. No API calls.
test "$(/usr/local/bin/claude --version | awk '{print $1}')" = "$CLAUDE_CODE_VERSION"
test "$(/usr/local/bin/codex --version)" = "codex-cli $CODEX_VERSION"
test "$(runuser -u "$AGENT_USER" -- /usr/local/bin/claude --version | awk '{print $1}')" = "$CLAUDE_CODE_VERSION"
test "$(runuser -u "$AGENT_USER" -- /usr/local/bin/codex --version)" = "codex-cli $CODEX_VERSION"
echo "install_agents: Claude Code ${CLAUDE_CODE_VERSION} and Codex ${CODEX_VERSION} ready"
