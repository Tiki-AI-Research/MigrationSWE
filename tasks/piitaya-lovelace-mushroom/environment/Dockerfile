# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:piitaya-lovelace-mushroom-base-image-20261126@sha256:520db50dfffadf46bdcec0a0a552286ff69cc8bcb407d54127e0260f406838c3
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

ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# Populate the complete locked dependency cache with npm 10 from Node 22,
# matching the candidate's npm cache format and metadata request headers.
# Keep all dependency changes outside the submitted repository.
COPY prewarm/package.json prewarm/package-lock.json /opt/offline-deps/
RUN set -eux; \
    export PATH="/opt/node22/bin:$PATH"; \
    cd /opt/offline-deps; \
    npm ci --offline=false --no-audit --no-fund; \
    npm install --offline=false --no-audit --no-fund --save-exact zod@3.23.8; \
    npm view zod@3.23.8 version --offline=false; \
    mkdir /opt/offline-deps/check; \
    cp /workspace/repo/package.json /workspace/repo/package-lock.json /opt/offline-deps/check/; \
    chown -R agent:agent /opt/offline-deps

# Fail the image build if an unprivileged candidate cannot reinstall the
# baseline and add the target dependency using only the prewarmed cache.
USER agent
RUN set -eux; \
    export PATH="/opt/node22/bin:$PATH"; \
    cd /opt/offline-deps/check; \
    npm ci --offline --no-audit --no-fund; \
    npm install --offline --no-audit --no-fund --save-exact zod@3.23.8; \
    test "$(node -p 'require("zod/package.json").version')" = 3.23.8; \
    npm view zod@3.23.8 version --offline; \
    cd /opt/offline-deps; \
    rm -rf check
USER root
