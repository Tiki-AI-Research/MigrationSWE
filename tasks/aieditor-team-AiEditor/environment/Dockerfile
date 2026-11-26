# Generated public source environment pinned to the Task-Pool image.
# The image already contains the repository at the claimed base commit.
FROM docker.io/tikiaiteam/migration:aieditor-team-aieditor-base-image-20261126@sha256:8ff027a383c5776dcdebb172957424aa139d0f1321cb2a91b0343c56bad71140
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

# Healthcheck contract: the repo pins npm@10.9.8 (package.json) and the runtime
# healthcheck requires `npm --version` to equal that pin while offline. Provision
# exactly that version globally (same pattern as sibling tasks' corepack pins).
RUN npm install -g --no-audit --no-fund npm@10.9.8 && test "$(npm --version)" = "10.9.8"

# Cache both the baseline lockfile and the target dependency while build-time
# networking is available. Use a disposable workspace to leave the source tree
# and its installed dependencies unchanged. A failed prewarm must fail the build.
# Also resolve the production tree without a lockfile: package consumers need
# registry metadata as well as the tarballs already cached by npm ci.
RUN set -eux; \
    mkdir -p /opt/offline-deps/npm; \
    prewarm_dir="$(mktemp -d)"; \
    cp /workspace/repo/package.json /workspace/repo/package-lock.json "$prewarm_dir/"; \
    cd "$prewarm_dir"; \
    npm ci --ignore-scripts --no-audit --no-fund --cache /opt/offline-deps/npm; \
    npm install --ignore-scripts --no-audit --no-fund --cache /opt/offline-deps/npm --save-exact @openai/agents@0.1.0; \
    node -e 'const fs=require("fs");const p=JSON.parse(fs.readFileSync("package.json","utf8"));delete p.devDependencies;delete p.scripts;fs.writeFileSync("package.json",JSON.stringify(p))'; \
    rm -rf node_modules package-lock.json; \
    npm install --ignore-scripts --no-audit --no-fund --cache /opt/offline-deps/npm; \
    cd /opt/offline-deps; \
    npm install --no-save --no-audit --no-fund --cache /opt/offline-deps/npm @openai/agents@0.1.0; \
    npm pack --ignore-scripts --cache /opt/offline-deps/npm --pack-destination "$prewarm_dir" \
      @openai/agents@0.1.0 @openai/agents-core@0.1.0 \
      @openai/agents-openai@0.1.0 @openai/agents-realtime@0.1.0; \
    rm -rf "$prewarm_dir"
RUN node --version && npm --version
ENV NPM_CONFIG_OFFLINE='true'
ENV NPM_CONFIG_CACHE='/opt/offline-deps/npm'


# --- migration eval agents (claude-code / codex): generic installer, see repair skill assets/agent-install/ ---
COPY install_agents.sh /tmp/install_agents.sh
RUN bash /tmp/install_agents.sh && rm -f /tmp/install_agents.sh

# The installers above run as root and may add cache entries. Transfer the cache
# only afterwards: npm needs write access even to read cached package metadata.
# Check real offline installs as the configured agent user in a disposable tree.
# npm pack uses abbreviated registry metadata while npm install/view use full
# metadata. Prewarm both variants above and verify SDK inspection offline too.
RUN set -eux; \
    chown -R agent:agent /opt/offline-deps/npm; \
    smoke_dir="$(mktemp -d)"; \
    cp /workspace/repo/package.json /workspace/repo/package-lock.json "$smoke_dir/"; \
    chown -R agent:agent "$smoke_dir"; \
    runuser -u agent -- sh -ec \
      'cd "$1"; npm ci --ignore-scripts --no-audit --no-fund; npm install --ignore-scripts --no-audit --no-fund --save-exact @openai/agents@0.1.0' \
      sh "$smoke_dir"; \
    cd "$smoke_dir"; \
    node -e 'const fs=require("fs");const p=JSON.parse(fs.readFileSync("package.json","utf8"));delete p.devDependencies;delete p.scripts;fs.writeFileSync("package.json",JSON.stringify(p))'; \
    rm -rf node_modules package-lock.json; \
    runuser -u agent -- sh -ec \
      'cd "$1"; npm install --offline --ignore-scripts --no-audit --no-fund' \
      sh "$smoke_dir"; \
    runuser -u agent -- sh -ec \
      'cd "$1"; npm pack --offline --ignore-scripts @openai/agents@0.1.0 @openai/agents-core@0.1.0 @openai/agents-openai@0.1.0 @openai/agents-realtime@0.1.0' \
      sh "$smoke_dir"; \
    cd /workspace/repo; \
    rm -rf "$smoke_dir"
