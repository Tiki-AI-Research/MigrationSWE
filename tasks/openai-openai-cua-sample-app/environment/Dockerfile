# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:openai-openai-cua-sample-app-base-image-20261126@sha256:8a973aea18c1a678d8e416ffe04b5943d4639a3e2249b6ae8bd18e7ce39e89af
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

# Keep both package managers' caches writable by the offline agent.
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'
ENV npm_config_store_dir=/opt/offline-deps/pnpm-store
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/offline-deps/ms-playwright

# Migration eval harness: provision the declared Node package manager for the agent user.
ENV COREPACK_HOME=/opt/offline-deps/corepack
RUN set -eux; mkdir -p /opt/offline-deps/corepack; COREPACK_HOME=/opt/offline-deps/corepack COREPACK_ENABLE_NETWORK=1 COREPACK_DEFAULT_TO_LATEST=0 corepack install --global pnpm@10.26.0; corepack enable --install-directory /usr/local/bin pnpm; chmod -R a+rX /opt/offline-deps/corepack; test -r /opt/offline-deps/corepack
ENV COREPACK_ENABLE_NETWORK=0
ENV COREPACK_DEFAULT_TO_LATEST=0

# Prepare target packages, pnpm metadata/store, and the baseline browser before
# runtime egress is restricted. Missing dependencies must fail the image build.
COPY prepare_offline_deps.sh /tmp/prepare_offline_deps.sh
RUN bash /tmp/prepare_offline_deps.sh && rm -f /tmp/prepare_offline_deps.sh
ENV NPM_CONFIG_OFFLINE='true'

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

