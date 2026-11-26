# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:partykit-base-image-20261126@sha256:64fcb8d307ee1e1d87ed4245bbaa1d49a7844d1973d9146274758e706c59bf8c
WORKDIR /workspace/repo

# Migration eval harness: preinstall mini-swe-agent for the agent user so
# external `harbor run` agent evaluations work without build-time egress.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
 && HOME=/home/agent uv python install 3.12 \
 && HOME=/home/agent uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi \
 && chown -R agent:agent /home/agent

# Offline dependency prewarm (contract-declared target deps; build-time network).
RUN set -eux; \
    mkdir -p /opt/offline-deps; \
    cd /opt/offline-deps; \
    npm install --no-save --no-audit --no-fund --registry=https://registry.npmjs.org --cache /opt/offline-deps/npm elysia@1.3.0; \
    npm view --registry=https://registry.npmjs.org --cache /opt/offline-deps/npm elysia@1.3.0 version
# npm install caches abbreviated metadata; npm view also needs the full metadata.
# Make the target package resolvable without changing source manifests or locks.
RUN set -eux; \
    mkdir -p /workspace/repo/node_modules; \
    if [ ! -e /workspace/repo/node_modules/elysia ]; then \
        ln -s /opt/offline-deps/node_modules/elysia /workspace/repo/node_modules/elysia; \
    fi; \
    node --version; \
    npm --version
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# Agent installation also writes to the shared npm cache. Fix ownership last,
# then verify both package metadata and a fresh installation as the runtime user.
RUN chown -R agent:agent /opt/offline-deps/npm
USER agent
RUN set -eux; \
    test "$(npm view --offline elysia@1.3.0 version)" = "1.3.0"; \
    node -e 'if (require("elysia/package.json").version !== "1.3.0") throw new Error("Unexpected Elysia version"); import("elysia").then(({ Elysia }) => { if (typeof Elysia !== "function") throw new Error("Elysia import failed"); });'; \
    offline_check_dir="$(mktemp -d)"; \
    trap 'rm -rf "$offline_check_dir"' EXIT; \
    cd "$offline_check_dir"; \
    printf '%s\n' '{"private":true}' > package.json; \
    npm install --offline --ignore-scripts --no-save --no-audit --no-fund --package-lock=false elysia@1.3.0; \
    node -e 'if (require("elysia/package.json").version !== "1.3.0") throw new Error("Offline Elysia installation failed");'
USER root
