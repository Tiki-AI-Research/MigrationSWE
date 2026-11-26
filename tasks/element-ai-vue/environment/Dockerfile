# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:element-ai-vue-base-image-20261126@sha256:b7dd5251bf9bf5a5e572845325b6f1aa847eff82539b02b5b029580fac54ef51
WORKDIR /workspace/repo

# Migration eval harness: preinstall mini-swe-agent for the agent user so
# external `harbor run` agent evaluations work without build-time egress.
RUN (command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; install -m 0755 /root/.local/bin/uv /usr/local/bin/uv; }) \
 && HOME=/home/agent uv python install 3.12 \
 && HOME=/home/agent uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi --with mcp \
 && /home/agent/.local/share/uv/tools/mini-swe-agent/bin/python -c 'import importlib; importlib.import_module("litellm.responses.mcp.litellm_proxy_mcp_handler")' \
 && chown -R agent:agent /home/agent

# Harbor installs mini-swe-agent again during agent setup. Its install command
# omits mcp, so uv may remove the package preinstalled above. Keep uv's normal
# behavior for every other command, and add mcp to this one tool install.
RUN set -eux; \
    mkdir -p /usr/local/libexec /home/agent/.local/bin; \
    install -m 0755 "$(command -v uv)" /usr/local/libexec/uv-real; \
    printf '%s\n' \
      '#!/bin/sh' \
      'if [ "$1" = tool ] && [ "$2" = install ]; then' \
      '  for arg do' \
      '    case "$arg" in' \
      '      mini-swe-agent*)' \
      '        /usr/local/libexec/uv-real "$@" --with mcp || exit $?' \
      '        tool_dir="$(/usr/local/libexec/uv-real tool dir)"' \
      '        "$tool_dir/mini-swe-agent/bin/python" -c "import mcp.types, litellm.responses.mcp.litellm_proxy_mcp_handler"' \
      '        exit $?' \
      '        ;;' \
      '    esac' \
      '  done' \
      'fi' \
      'exec /usr/local/libexec/uv-real "$@"' \
      > /home/agent/.local/bin/uv; \
    chmod 0755 /home/agent/.local/bin/uv; \
    HOME=/home/agent /home/agent/.local/bin/uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi; \
    chown -R agent:agent /home/agent

# Install the repository's pnpm version during the image build, when network
# access is available. Agent runs reuse Corepack's cache without downloading it.
ENV COREPACK_HOME=/opt/corepack
RUN set -eux; \
    mkdir -p "$COREPACK_HOME" /opt/pnpm-store; \
    corepack enable; \
    corepack install -g pnpm@10.18.2; \
    test "$(pnpm --version)" = "10.18.2"

# Warm pnpm's store with the migration's target packages and the workspace's
# Vue peer dependency in a separate project. Share the registry metadata too:
# offline lockfile updates need it even when package contents are installed.
# Reinstalling that project offline checks that the cache works without touching
# the provided repository's existing node_modules directory.
RUN set -eux; \
    mkdir -p /opt/pnpm-seed /opt/pnpm-cache; \
    if [ -d /root/.cache/pnpm ]; then cp -a /root/.cache/pnpm/. /opt/pnpm-cache/; fi; \
    printf '%s\n' '{"private":true,"dependencies":{"markdown-it":"14.1.0","vue":"^3.4.0"},"devDependencies":{"@types/markdown-it":"14.1.2"}}' > /opt/pnpm-seed/package.json; \
    cd /opt/pnpm-seed; \
    pnpm install --ignore-scripts --store-dir /opt/pnpm-store --cache-dir /opt/pnpm-cache; \
    rm -rf node_modules pnpm-lock.yaml; \
    pnpm install --offline --ignore-scripts --store-dir /opt/pnpm-store --cache-dir /opt/pnpm-cache; \
    printf 'store-dir=/opt/pnpm-store\ncache-dir=/opt/pnpm-cache\n' >> /home/agent/.npmrc; \
    chown -R agent:agent "$COREPACK_HOME" /opt/pnpm-store /opt/pnpm-cache /home/agent/.npmrc

# The base image's modules refer to root's original store. Retain any cached
# contents and point their metadata at the shared store, so pnpm reuses the
# installed dependencies instead of trying to remove them without a TTY.
RUN set -eux; \
    modules_metadata=/workspace/repo/node_modules/.modules.yaml; \
    test -f "$modules_metadata"; \
    original_store="$(sed -n 's/^storeDir: //p' "$modules_metadata")"; \
    shared_store="$(pnpm store path --store-dir /opt/pnpm-store)"; \
    mkdir -p "$shared_store"; \
    if [ -d "$original_store" ] && [ "$original_store" != "$shared_store" ]; then \
        cp -a "$original_store/." "$shared_store/"; \
    fi; \
    sed -i "s|^storeDir: .*|storeDir: $shared_store|" "$modules_metadata"; \
    grep -Fqx "storeDir: $shared_store" "$modules_metadata"; \
    chown agent:agent "$modules_metadata"; \
    chown -R agent:agent /opt/pnpm-store

ENV NPM_CONFIG_OFFLINE=true

# Preinstall Codex and Claude Code for agent runs restricted to the model gateway.
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# The public test:coverage script uses Vitest 4.0.16, but the supplied
# repository does not declare its optional coverage provider. Install the
# matching provider outside the checkout so the test can run without changing
# the candidate's package.json or lockfile.
RUN set -eux; \
    NPM_CONFIG_OFFLINE=false npm install --prefix /opt/vitest-coverage \
      --no-save --ignore-scripts --no-audit --no-fund \
      @vitest/coverage-v8@4.0.16; \
    mkdir -p /workspace/repo/node_modules/@vitest; \
    ln -s /opt/vitest-coverage/node_modules/@vitest/coverage-v8 \
      /workspace/repo/node_modules/@vitest/coverage-v8
