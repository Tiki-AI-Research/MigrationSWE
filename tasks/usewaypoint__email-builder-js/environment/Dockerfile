# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:usewaypoint__email-builder-js-base-image-20261126@sha256:942aa970cb17861cfe281957c3a210db8d3dccf0f1cd59c6fa6115f9357af1c3
WORKDIR /workspace/repo

# Migration eval harness: preinstall mini-swe-agent for the agent user so
# external `harbor run` agent evaluations work without build-time egress.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
 && HOME=/home/agent uv python install 3.12 \
 && HOME=/home/agent uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi --with mcp \
 && chown -R agent:agent /home/agent

# Migration eval harness: keep the mcp extra when Harbor reinstalls mini-swe-agent.
RUN set -eux; \
    if ! command -v uv >/dev/null 2>&1; then \
      curl -LsSf https://astral.sh/uv/install.sh | sh; \
      install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; \
    fi; \
    if [ ! -x /usr/local/bin/uv.real ]; then \
      uv_path="$(command -v uv)"; \
      if [ "$uv_path" = "/usr/local/bin/uv" ] && grep -q 'Migration eval harness minimal uv wrapper' "$uv_path"; then \
        echo 'uv wrapper exists without uv.real' >&2; exit 1; \
      fi; \
      install -m 0755 "$uv_path" /usr/local/bin/uv.real; \
    fi; \
    printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      '# Migration eval harness minimal uv wrapper' \
      real_uv=/usr/local/bin/uv.real \
      saw_tool=0 \
      saw_install=0 \
      saw_agent=0 \
      saw_mcp=0 \
      previous= \
      'for argument in "$@"; do' \
      '    case "$argument" in' \
      '        tool) saw_tool=1 ;;' \
      '        install) if [ "$saw_tool" -eq 1 ]; then saw_install=1; fi ;;' \
      '        mini-swe-agent|mini-swe-agent==*|mini-swe-agent@*) saw_agent=1 ;;' \
      '        --with=mcp) saw_mcp=1 ;;' \
      '        mcp) if [ "$previous" = "--with" ]; then saw_mcp=1; fi ;;' \
      '    esac' \
      '    previous=$argument' \
      done \
      'if [ "$saw_tool" -eq 1 ] && [ "$saw_install" -eq 1 ] && [ "$saw_agent" -eq 1 ] && [ "$saw_mcp" -eq 0 ]; then' \
      '    exec "$real_uv" "$@" --with mcp' \
      fi \
      'exec "$real_uv" "$@"' \
      > /usr/local/bin/uv; \
    chmod 0755 /usr/local/bin/uv

# Offline dependency prewarm, including declarations for the TypeScript build.
RUN set -eux; \
    mkdir -p /opt/offline-deps; \
    cd /opt/offline-deps; \
    npm install --offline=false --no-save --no-audit --no-fund --cache /opt/offline-deps/npm markdown-it@14.1.0 @types/markdown-it@14.1.1; \
    node --version; \
    npm --version
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# npm compacts cache indexes even on offline reads. Hand over the cache only
# after the last root-owned npm install, including the agent CLI installation.
RUN chown -R agent:agent /opt/offline-deps/npm

# Fail the build if the non-root agent cannot install and type-check offline.
# Use the public Node toolchain without depending on login-shell PATH setup.
USER agent
RUN set -eux; \
    offline_probe="$(mktemp -d)"; \
    trap 'rm -rf "$offline_probe"' EXIT; \
    cd "$offline_probe"; \
    /opt/node22/bin/node /opt/node22/lib/node_modules/npm/bin/npm-cli.js install \
      --offline --ignore-scripts --no-audit --no-fund --package-lock=false markdown-it@14.1.0 @types/markdown-it@14.1.1; \
    /opt/node22/bin/node -e "const assert = require('node:assert/strict'); assert.equal(require('markdown-it/package.json').version, '14.1.0'); assert.equal(require('@types/markdown-it/package.json').version, '14.1.1'); assert.match(require('markdown-it')().render('offline'), /<p>offline<\/p>/)"; \
    printf '%s\n' 'import MarkdownIt from "markdown-it";' 'const html: string = new MarkdownIt().render("offline");' > offline-probe.ts; \
    /opt/node22/bin/node /workspace/repo/node_modules/typescript/bin/tsc \
      --strict --esModuleInterop --skipLibCheck --noEmit --moduleResolution node \
      --module commonjs --target ES2020 offline-probe.ts
USER root
