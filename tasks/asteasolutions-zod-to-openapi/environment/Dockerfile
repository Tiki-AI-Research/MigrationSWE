# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:asteasolutions-zod-to-openapi-base-image-20261126@sha256:f609fe36ba3f9b0ede5559b6d7472b536dae729828042c7550dc5fade85b2677
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

# Runtime npm is offline, so cache the full source lockfile as well as the
# migration target. Work on copies to leave the candidate repository unchanged.
# Missing downloads must fail the build instead of producing an unusable cache.
RUN set -eux; \
    mkdir -p /opt/offline-deps; \
    cp /workspace/repo/package.json /workspace/repo/package-lock.json /opt/offline-deps/; \
    cd /opt/offline-deps; \
    export NPM_CONFIG_OFFLINE=false NPM_CONFIG_CACHE=/opt/offline-deps/npm; \
    npm ci --include=dev --ignore-scripts --no-audit --no-fund; \
    npm ci --offline --include=dev --ignore-scripts --no-audit --no-fund; \
    npm install --package-lock-only --ignore-scripts --save-dev --save-exact --no-audit --no-fund zod@4.1.0; \
    npm cache add zod@4.1.0; \
    npm ci --offline --include=dev --ignore-scripts --no-audit --no-fund; \
    rm -f package.json package-lock.json; \
    node --version; \
    npm --version
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
# Agent installers also populate this cache as root; set ownership last so the
# runtime agent can write cache indexes, temporary files and npm logs.
RUN bash /tmp/install_agents.sh \
    && rm -f /tmp/install_agents.sh \
    && chown -R agent:agent /opt/offline-deps
