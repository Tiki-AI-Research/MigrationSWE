# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:gre__react-native-view-shot-base-image-20261126@sha256:01359ac81bdf0378a93c58871acc7e23fc2613c8fab9c67127dbf8d68e136a76
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

# Cache the complete baseline and target dependency graphs while build-time
# network is available. Work on manifest copies so the candidate repo stays at
# its base commit; caching React Native alone misses packages needed by npm ci.
RUN set -eux; \
    mkdir -p /opt/offline-deps; \
    cp /workspace/repo/package.json /workspace/repo/package-lock.json /opt/offline-deps/; \
    if [ -f /workspace/repo/.npmrc ]; then cp /workspace/repo/.npmrc /opt/offline-deps/; fi; \
    cd /opt/offline-deps; \
    export NPM_CONFIG_OFFLINE=false NPM_CONFIG_CACHE=/opt/offline-deps/npm; \
    npm ci --include=dev --ignore-scripts --legacy-peer-deps --no-audit --no-fund; \
    npm install --save-dev --save-exact --include=dev --ignore-scripts --legacy-peer-deps --no-audit --no-fund react-native@0.85.0; \
    rm -rf node_modules; \
    node --version; \
    npm --version
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# The installers run as root and also use this cache. Hand it to the runtime
# user afterwards, then prove a clean target install works offline as agent.
RUN chown -R agent:agent /opt/offline-deps \
 && su -s /bin/sh agent -c 'cd /opt/offline-deps && npm ci --offline --include=dev --ignore-scripts --legacy-peer-deps --no-audit --no-fund' \
 && test "$(node -p 'require("/opt/offline-deps/node_modules/react-native/package.json").version')" = 0.85.0
